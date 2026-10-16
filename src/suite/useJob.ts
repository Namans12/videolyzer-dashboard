// src/suite/useJob.ts — poll GET /job/{id} until a terminal state; one request in flight; one session per job id.
import { useEffect, useRef, useState } from "react";
import { cancelJob, errorSentence, getJob, SuiteApiError } from "./api";
import { isTerminalStatus } from "../jobStatus";
import { BAD_JOB_RESPONSE_TEXT } from "./messages";
import type { Job, JobEvent } from "./types";

export const POLL_MS = 1500;
export const MAX_BACKOFF_MS = 15000;

export function backoffMs(failures: number): number {
  return Math.min(POLL_MS * 2 ** failures, MAX_BACKOFF_MS);
}

export type PollErrorClass = "gone" | "retry" | "contract" | "fatal";

/** 404 = purged; network (status 0) or 5xx = retry; unusable 2xx = contract; any other HTTP failure = fatal. */
export function classifyPollError(err: unknown): PollErrorClass {
  if (err instanceof SuiteApiError) {
    if (err.status === 404) return "gone";
    if (err.kind === "bad_response") return "contract";
    if (err.status === 0 || err.status >= 500) return "retry";
    return "fatal";
  }
  return "retry";
}

export type JobView = "idle" | "running" | "done" | "error" | "cancelled" | "gone";

export interface UseJobResult {
  status: JobView;
  serverStatus: string | null;
  progress: string;
  current: string;
  events: JobEvent[];
  results: unknown;
  error: string | null;
  errorKind: string | null;
  errorDetail: string | null;
  exitCode: number | null;
  unreachable: boolean;
  cancelRequested: boolean;
  cancelFailed: boolean;
  cancel: () => Promise<void>;
}

type Snapshot = Omit<UseJobResult, "cancel">;

interface Session {
  id: string;
  ctrl: AbortController;
  timer: ReturnType<typeof setTimeout> | undefined;
  stopped: boolean;
  cancelling: boolean;
  stop: () => void;
}

const IDLE: Snapshot = {
  status: "idle", serverStatus: null, progress: "", current: "", events: [], results: null, error: null,
  errorKind: null, errorDetail: null, exitCode: null, unreachable: false, cancelRequested: false, cancelFailed: false,
};
const STARTING: Snapshot = { ...IDLE, status: "running" };

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strOrNull = (v: unknown): string | null => (typeof v === "string" ? v : null);
const isEvent = (e: unknown): e is JobEvent =>
  e !== null && typeof e === "object" && typeof (e as JobEvent).msg === "string";

function fromJob(job: Job, prev: Snapshot): Snapshot {
  const s = str(job.status);
  const status: JobView = s === "done" || s === "error" || s === "cancelled" ? s : "running";
  return {
    ...prev, status, serverStatus: s || null, progress: str(job.progress), current: str(job.current),
    events: Array.isArray(job.events) ? job.events.filter(isEvent) : [], results: job.results ?? null,
    error: strOrNull(job.error), errorKind: strOrNull(job.error_kind), errorDetail: strOrNull(job.error_detail),
    exitCode: typeof job.exit_code === "number" ? job.exit_code : null, unreachable: false,
  };
}

function newSession(id: string): Session {
  const session: Session = {
    id, ctrl: new AbortController(), timer: undefined, stopped: false, cancelling: false,
    stop: () => {
      session.stopped = true;
      if (session.timer !== undefined) clearTimeout(session.timer);
      session.timer = undefined;
      session.ctrl.abort();
    },
  };
  return session;
}

export function useJob(jobId: string | null): UseJobResult {
  const [state, setState] = useState<{ id: string | null; snap: Snapshot }>({ id: null, snap: IDLE });
  const sessionRef = useRef<Session | null>(null);
  // Derived during render: a new id never shows the previous job's data (no setState in the effect body).
  const snap: Snapshot = jobId === null ? IDLE : state.id === jobId ? state.snap : STARTING;

  useEffect(() => {
    if (jobId === null) return;
    const session = newSession(jobId);
    sessionRef.current = session;
    let failures = 0;
    const publish = (fn: (prev: Snapshot) => Snapshot) =>
      setState((st) => ({ id: session.id, snap: fn(st.id === session.id ? st.snap : STARTING) }));
    const tick = async () => {
      session.timer = undefined;
      if (session.stopped) return;
      try {
        const job = await getJob(session.id, session.ctrl.signal);
        if (session.stopped) return;
        failures = 0;
        publish((prev) => fromJob(job, prev));
        if (isTerminalStatus(job.status)) session.stop();
        else session.timer = setTimeout(tick, POLL_MS);
      } catch (err) {
        if (session.stopped) return;
        const kind = classifyPollError(err);
        if (kind === "gone") {
          session.stop();
          publish((prev) => ({ ...prev, status: "gone", unreachable: false }));
          return;
        }
        if (kind === "retry") {
          failures += 1;
          publish((prev) => ({ ...prev, unreachable: true }));
          session.timer = setTimeout(tick, backoffMs(failures));
          return;
        }
        session.stop();
        publish((prev) => ({
          ...prev, status: "error", unreachable: false, errorDetail: null, exitCode: null,
          errorKind: kind === "contract" ? "contract" : null,
          error: kind === "contract" ? BAD_JOB_RESPONSE_TEXT : errorSentence(err),
        }));
      }
    };
    session.timer = setTimeout(tick, 0);
    return () => {
      session.stop();
      if (sessionRef.current === session) sessionRef.current = null;
    };
  }, [jobId]);

  const cancel = async (): Promise<void> => {
    const session = sessionRef.current;
    if (session === null || session.id !== jobId || session.stopped || session.cancelling || snap.status !== "running") return;
    session.cancelling = true;                        // exactly one outstanding cancel POST
    setState((st) => ({
      id: session.id,
      snap: { ...(st.id === session.id ? st.snap : STARTING), cancelRequested: true, cancelFailed: false },
    }));
    try {
      await cancelJob(session.id, session.ctrl.signal);
      session.cancelling = false;                     // accepted: keep polling until the server reports a terminal state
    } catch (err) {
      session.cancelling = false;
      if (session.stopped || sessionRef.current !== session) return;   // disposed or replaced: ignore
      if (err instanceof SuiteApiError && err.status === 404) {
        session.stop();                               // no further GET; a pending GET is aborted and ignored
        setState((st) => ({ id: session.id, snap: { ...(st.id === session.id ? st.snap : STARTING), status: "gone", unreachable: false } }));
        return;
      }
      setState((st) => (st.id !== session.id ? st : {
        id: session.id, snap: { ...st.snap, cancelRequested: false, cancelFailed: true },
      }));
    }
  };

  return { ...snap, cancel };
}
