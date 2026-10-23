// src/compare/useComparison.ts — the browser side of a backend-owned comparison run.
// The backend owns planning, the compare→rank chain and the result; this hook only plans, confirms one immutable plan
// revision, polls the existing job, reads the run detail and reconnects to server runs after a reload. Stale answers
// are dropped by generation, polling stops at done/error/cancelled/gone, and nothing is stored in the browser.
import { useEffect, useRef, useState } from "react";
import { cancelJob, errorSentence, SuiteApiError } from "../suite/api";
import { backoffMs, classifyPollError, POLL_MS } from "../suite/useJob";
import { BAD_JOB_RESPONSE_TEXT } from "../suite/messages";
import { cancelRun, confirmRun, getRun, getRunJob, listRuns, planRun } from "./api";
import { ACTIVE_STAGES } from "./types";
import type { RunDetail, RunJob, RunRequest, RunResult, RunSnapshot } from "./types";

export interface ComparisonJobView {
  status: RunJob["status"] | "gone";
  current: string;
  events: RunJob["events"];
  error: string | null;
  errorKind: string | null;
  unreachable: boolean;
}

export interface ComparisonState {
  run: RunSnapshot | null;
  result: RunResult | null;
  job: ComparisonJobView | null;
  busy: boolean;
  error: string | null;
  gone: boolean;
  cancelRequested: boolean;
  /** Older plans on the server that were never started and are not the run shown (found on reconnect). */
  leftoverPlans: RunSnapshot[];
}

export interface UseComparison extends ComparisonState {
  plan: (request: RunRequest) => void;
  start: () => void;
  cancel: () => void;
  reconnect: () => void;
  reset: () => void;
  /** Hides a shown error (the owner changed the inputs). */
  dismissError: () => void;
  /** Cancels the leftover never-started plans. */
  discardLeftoverPlans: () => void;
}

const IDLE: ComparisonState = {
  run: null, result: null, job: null, busy: false, error: null, gone: false, cancelRequested: false, leftoverPlans: [],
};
const PENDING_STAGES = ["awaiting_confirmation", "selection_required"];

type Setter = (fn: (s: ComparisonState) => ComparisonState) => void;
interface Controller {
  gen: number;
  mounted: boolean;
  ctrl: AbortController | null;
  timer: ReturnType<typeof setTimeout> | undefined;
  confirming: Set<string>;
}

/** Starts a new generation: older answers are ignored from now on. Only disposal aborts requests, so a finished
 *  response is never aborted at an ordinary step (no stray AbortError in the packaged browser). */
function begin(c: Controller, abort = false): number {
  if (c.timer !== undefined) clearTimeout(c.timer);
  c.timer = undefined;
  if (abort) c.ctrl?.abort();
  c.ctrl = new AbortController();
  c.gen += 1;
  return c.gen;
}

const live = (c: Controller, gen: number) => c.mounted && c.gen === gen;
const sentence = (err: unknown) =>
  err instanceof SuiteApiError && err.kind === "bad_response" ? BAD_JOB_RESPONSE_TEXT : errorSentence(err);
const isAbort = (err: unknown) => err instanceof DOMException && err.name === "AbortError";

function jobView(job: RunJob): ComparisonJobView {
  return { status: job.status, current: job.current, events: job.events, error: job.error, errorKind: job.error_kind, unreachable: false };
}

/** A run that stopped with a problem before it had a result (a failed check): its job holds the backend's message.
 *  Read once; a purged job just leaves the plain fallback text. */
async function readFailedJob(c: Controller, set: Setter, gen: number, runId: string, jobId: string): Promise<void> {
  try {
    const job = await getRunJob(jobId, c.ctrl?.signal);
    if (!live(c, gen) || job.status !== "error" || (job.run !== null && job.run.run_id !== runId)) return;
    set((s) => (s.run?.run_id === runId ? { ...s, job: jobView(job) } : s));
  } catch {
    // purged or unreachable: the screen says the details are no longer available
  }
}

async function refresh(c: Controller, set: Setter, gen: number, runId: string, follow: boolean): Promise<RunDetail | null> {
  try {
    const detail = await getRun(runId, c.ctrl?.signal);
    if (!live(c, gen)) return null;
    set((s) => ({ ...s, run: detail.run, result: detail.result, busy: false, gone: false }));
    if (follow && ACTIVE_STAGES.includes(detail.run.stage) && detail.run.job_id) watch(c, set, gen, runId, detail.run.job_id);
    if (follow && detail.run.stage === "error" && detail.result === null && detail.run.job_id) {
      await readFailedJob(c, set, gen, runId, detail.run.job_id);
    }
    return detail;
  } catch (err) {
    if (!live(c, gen) || isAbort(err)) return null;
    if (err instanceof SuiteApiError && err.status === 404) {
      set((s) => ({ ...s, busy: false, gone: true, job: s.job ? { ...s.job, status: "gone" } : null }));
      return null;
    }
    set((s) => ({ ...s, busy: false, error: sentence(err) }));
    return null;
  }
}

/** Polls GET /job/{id} until a terminal state, then reads the run detail once. */
function watch(c: Controller, set: Setter, gen: number, runId: string, jobId: string): void {
  let failures = 0;
  const tick = async () => {
    c.timer = undefined;
    if (!live(c, gen)) return;
    try {
      const job = await getRunJob(jobId, c.ctrl?.signal);
      if (!live(c, gen)) return;
      failures = 0;
      set((s) => ({
        ...s, job: jobView(job), error: null,
        run: job.run !== null && job.run.run_id === runId ? job.run : s.run,
        result: job.results !== null && job.results.run_id === runId ? job.results : s.result,
      }));
      if (job.status === "running") c.timer = setTimeout(tick, POLL_MS);
      else await refresh(c, set, gen, runId, false);
    } catch (err) {
      if (!live(c, gen) || isAbort(err)) return;
      const kind = classifyPollError(err);
      if (kind === "gone") { await refresh(c, set, gen, runId, false); return; }
      if (kind === "retry") {
        failures += 1;
        set((s) => ({ ...s, job: s.job ? { ...s.job, unreachable: true } : s.job }));
        c.timer = setTimeout(tick, backoffMs(failures));
        return;
      }
      set((s) => ({ ...s, busy: false, error: sentence(err) }));
    }
  };
  c.timer = setTimeout(tick, 0);
}

async function reconnectRuns(c: Controller, set: Setter, gen: number): Promise<void> {
  try {
    const { runs } = await listRuns(c.ctrl?.signal);
    if (!live(c, gen)) return;
    // The backend lists newest first: a working run wins, otherwise the newest run of any stage. A never-started plan
    // never hides a newer finished result; older pending plans are offered for discarding instead.
    const chosen = runs.find((r) => ACTIVE_STAGES.includes(r.stage)) ?? runs[0] ?? null;
    if (chosen === null) { set(() => IDLE); return; }
    const leftoverPlans = runs.filter((r) => PENDING_STAGES.includes(r.stage) && r.run_id !== chosen.run_id);
    set((s) => ({ ...s, leftoverPlans }));
    await refresh(c, set, gen, chosen.run_id, true);
  } catch (err) {
    if (!live(c, gen) || isAbort(err)) return;
    set((s) => ({ ...s, busy: false, error: sentence(err) }));
  }
}

export function useComparison(): UseComparison {
  const [state, setState] = useState<ComparisonState>(IDLE);
  const ref = useRef<Controller>({ gen: 0, mounted: false, ctrl: null, timer: undefined, confirming: new Set() });

  useEffect(() => {
    const c = ref.current;
    c.mounted = true;
    const gen = begin(c);
    const t = setTimeout(() => { void reconnectRuns(c, setState, gen); }, 0);
    return () => {
      clearTimeout(t);
      c.mounted = false;
      begin(c, true);
    };
  }, []);

  const plan = (request: RunRequest) => {
    const c = ref.current;
    const gen = begin(c);
    // This screen's own earlier plan that was never started is cancelled first (awaited), so a re-check never leaves
    // an orphan plan behind. Only the plan this hook shows is touched, never another tab's.
    const own = state.run !== null && PENDING_STAGES.includes(state.run.stage) ? state.run.run_id : null;
    setState((s) => ({ ...IDLE, leftoverPlans: s.leftoverPlans, busy: true }));
    const cancelled = own === null ? Promise.resolve() : cancelRun(own, c.ctrl?.signal).then(() => undefined, (err: unknown) => {
      if (isAbort(err)) throw err;                     // anything else (already started, gone, busy): plan anyway
    });
    cancelled.then(() => (live(c, gen) ? planRun(request, c.ctrl?.signal) : null)).then((started) => {
      if (started === null || !live(c, gen)) return;
      setState((s) => ({ ...s, job: { status: "running", current: "", events: [], error: null, errorKind: null, unreachable: false } }));
      watch(c, setState, gen, started.run_id, started.job_id);
    }).catch((err: unknown) => {
      if (!live(c, gen) || isAbort(err)) return;
      setState((s) => ({ ...s, busy: false, error: sentence(err) }));
    });
  };

  const start = () => {
    const c = ref.current;
    const run = state.run;
    if (run === null || run.stage !== "awaiting_confirmation") return;
    const key = `${run.run_id}:${run.plan_revision}`;
    if (c.confirming.has(key)) return;                 // one confirmation in flight per immutable plan revision
    c.confirming.add(key);
    const gen = begin(c);
    setState((s) => ({ ...s, busy: true, error: null, cancelRequested: false }));
    confirmRun(run.run_id, run.plan_revision, c.ctrl?.signal).then((started) => {
      if (!live(c, gen)) return;
      watch(c, setState, gen, started.run_id, started.job_id);
    }).catch(async (err: unknown) => {
      if (err instanceof SuiteApiError && err.status === 0) c.confirming.delete(key);   // never reached the backend
      if (!live(c, gen) || isAbort(err)) return;
      setState((s) => ({ ...s, busy: false, error: sentence(err) }));
      // Reconcile with the backend: a plan still waiting for this exact revision was refused (busy), so Start may be
      // pressed again; an already accepted run (an ambiguous answer) is followed instead.
      const detail = await refresh(c, setState, gen, run.run_id, true);
      if (detail !== null && detail.run.stage === "awaiting_confirmation" && detail.run.plan_revision === run.plan_revision) {
        c.confirming.delete(key);
      }
    });
  };

  const cancel = () => {
    const c = ref.current;
    const run = state.run;
    if (run === null || state.cancelRequested) return;
    const gen = c.gen;
    setState((s) => ({ ...s, cancelRequested: true }));
    const pending = PENDING_STAGES.includes(run.stage);
    const request = pending || run.job_id === null ? cancelRun(run.run_id, c.ctrl?.signal) : cancelJob(run.job_id, c.ctrl?.signal);
    request.then(() => {
      if (!live(c, gen)) return;
      if (pending || c.timer === undefined) void refresh(c, setState, gen, run.run_id, false);
    }).catch((err: unknown) => {
      if (!live(c, gen) || isAbort(err)) return;
      setState((s) => ({ ...s, cancelRequested: false, error: sentence(err) }));
    });
  };

  const reconnect = () => {
    const c = ref.current;
    const gen = begin(c);
    setState((s) => ({ ...s, busy: true, error: null, job: null }));
    void reconnectRuns(c, setState, gen);
  };

  const reset = () => {
    begin(ref.current);
    setState((s) => ({ ...IDLE, leftoverPlans: s.leftoverPlans }));
  };

  const dismissError = () => {
    setState((s) => (s.error === null ? s : { ...s, error: null }));
  };

  const discardLeftoverPlans = () => {
    const c = ref.current;
    for (const old of state.leftoverPlans) {
      cancelRun(old.run_id).then(() => true, (err: unknown) => err instanceof SuiteApiError && err.status === 404)
        .then((done) => {
          if (done && c.mounted) setState((s) => ({ ...s, leftoverPlans: s.leftoverPlans.filter((r) => r.run_id !== old.run_id) }));
        });
    }
  };

  return { ...state, plan, start, cancel, reconnect, reset, dismissError, discardLeftoverPlans };
}
