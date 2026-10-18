import { useEffect, useRef, useState } from "react";
import { errorSentence, SuiteApiError } from "../api";
import { useJob } from "../useJob";
import * as api from "./api";
import { ANSWER_NOT_IN_KIT_TEXT, CALIBRATION_CONTRACT_TEXT, PREVIEW_AGAIN_TEXT, SESSION_REQUIRED_TEXT, SETTINGS_CHANGED_TEXT } from "./messages";
import { isBuildDocument, isPlanDocument, isReportDocument } from "./types";
import type { ApplyDocument, BuildRequest, CalibrationAnswer, PlanDocument, PlanRequest, RecordRequest, SessionRequest, StatusDocument } from "./types";

type Channel = "status" | "plan" | "build" | "report" | "session" | "preview" | "apply";
interface Snapshot {
  status: StatusDocument | null; loading: boolean; error: string | null;
  pendingAnswers: RecordRequest[]; preview: ApplyDocument | null; applyResult: ApplyDocument | null;
  planDocument: PlanDocument | null; planId: string | null; buildId: string | null; reportId: string | null;
  mutationBusy: boolean;
}
interface Scope {
  key: string | null; ctrl: AbortController; active: boolean; snap: Snapshot;
  revision: number; seq: Map<Channel, number>; saves: Set<string>;
}
const empty = (): Snapshot => ({ status: null, loading: true, error: null, pendingAnswers: [], preview: null,
  applyResult: null, planDocument: null, planId: null, buildId: null, reportId: null, mutationBusy: false });
const pairKey = (test: string, player: string) => JSON.stringify([test, player]);
const copy = <T>(v: T): T => structuredClone(v);
function next(s: Scope, channel: Channel): number { const n = (s.seq.get(channel) ?? 0) + 1; s.seq.set(channel, n); return n; }
function current(s: Scope, channel: Channel, n: number): boolean { return s.active && s.seq.get(channel) === n; }
function message(err: unknown): string {
  if (err instanceof SuiteApiError && err.status === 409 && err.detail === SETTINGS_CHANGED_TEXT) return SETTINGS_CHANGED_TEXT;
  return err instanceof SuiteApiError && err.kind === "bad_response" ? CALIBRATION_CONTRACT_TEXT : errorSentence(err);
}
function mergeAnswer(doc: StatusDocument, answer: CalibrationAnswer): StatusDocument {
  const answers = doc.answers.filter(a => pairKey(a.test, a.player) !== pairKey(answer.test, answer.player));
  const previous = doc.answers.findIndex(a => pairKey(a.test, a.player) === pairKey(answer.test, answer.player));
  if (previous < 0) answers.push(answer); else answers.splice(previous, 0, answer);
  return { ...doc, answers, summary: { done: answers.length, total: doc.kit?.descriptors.length ?? doc.summary.total,
    matches: answers.filter(a => a.match === "matches").length, differs: answers.filter(a => a.match === "differs").length } };
}

/** Server status is the durable source of truth; localStorage is deliberately unnecessary.
 * Pending choices are separate from confirmed answers: no invented timestamps/evidence.
 * Each kit owns a scope; each replaceable operation owns a generation and copied input.
 */
export function useCalibration(kitId: string | null) {
  const [state, setState] = useState<{ key: string | null; snap: Snapshot }>(() => ({ key: kitId, snap: empty() }));
  const scope = useRef<Scope | null>(null);
  // Remember every key transition, including A -> B (still loading) -> A.
  // Otherwise the old A snapshot could reappear with saves belonging to a disposed scope.
  if (state.key !== kitId) setState({ key: kitId, snap: empty() });
  const snap = state.key === kitId ? state.snap : empty();
  const publish = (s: Scope, patch: Partial<Snapshot>) => {
    if (!s.active) return;
    s.snap = { ...s.snap, ...patch };
    setState({ key: s.key, snap: s.snap });
  };
  const invalidate = (s: Scope) => {
    s.revision += 1;
    next(s, "preview"); next(s, "report");
    publish(s, { preview: null, reportId: null, loading: s.snap.status ? false : s.snap.loading });
  };
  useEffect(() => {
    const s: Scope = { key: kitId, ctrl: new AbortController(), active: true, snap: empty(), revision: 0, seq: new Map(), saves: new Set() };
    scope.current = s;
    const n = next(s, "status");
    // Async completion publishes; StrictMode cleanup aborts and rejects all old publications.
    void api.getStatus(kitId, s.ctrl.signal).then(
      status => { if (current(s, "status", n)) publish(s, { status, loading: false }); },
      err => { if (current(s, "status", n)) publish(s, { loading: false, error: message(err) }); },
    );
    return () => { s.active = false; s.ctrl.abort(); if (scope.current === s) scope.current = null; };
  }, [kitId]);

  const planJob = useJob(snap.planId);
  const buildJob = useJob(snap.buildId);
  const reportJob = useJob(snap.reportId);
  const getScope = () => { const s = scope.current; return s?.active && s.key === kitId ? s : null; };

  const refresh = async (): Promise<boolean> => {
    const s = getScope(); if (!s || s.saves.size || s.snap.mutationBusy) return false;
    const n = next(s, "status"); const revision = s.revision;
    publish(s, { loading: true, error: null });
    try {
      const status = await api.getStatus(s.snap.status?.kit?.id ?? s.key, s.ctrl.signal);
      if (!current(s, "status", n) || revision !== s.revision) return false;
      invalidate(s); publish(s, { status, loading: false }); return true;
    } catch (err) { if (current(s, "status", n) && revision === s.revision) publish(s, { loading: false, error: message(err) }); return false; }
  };

  const answer = async (test: string, player: string, outcome: string, note: string): Promise<boolean> => {
    const s = getScope(); const key = pairKey(test, player);
    if (!s || s.saves.has(key) || s.snap.mutationBusy || s.snap.loading) return false;
    const doc = s.snap.status;
    if (!doc?.session || !doc.kit) { publish(s, { error: SESSION_REQUIRED_TEXT }); return false; }
    const descriptor = doc.kit.descriptors.find(d => d.test === test && d.player === player);
    if (!descriptor?.outcomes.some(o => o.id === outcome)) { publish(s, { error: ANSWER_NOT_IN_KIT_TEXT }); return false; }
    const body: RecordRequest = { kit: doc.kit.id, test, player, outcome, note: api.singleLineNote(note) };
    s.saves.add(key); next(s, "status"); invalidate(s);
    publish(s, { error: null, pendingAnswers: [...s.snap.pendingAnswers, body] });
    try {
      const result = await api.postRecord(copy(body), s.ctrl.signal);
      if (!s.active) return false;
      if (result.answer.kit_id !== body.kit || result.answer.test !== test || result.answer.player !== player || result.answer.outcome !== outcome) throw new SuiteApiError(200, "", "bad_response");
      // Merge by pair into the latest snapshot. A response's summary may predate a concurrent save.
      publish(s, { status: mergeAnswer(s.snap.status!, result.answer) }); return true;
    } catch (err) { if (s.active) publish(s, { error: message(err) }); return false; }
    finally {
      s.saves.delete(key);
      if (s.active) publish(s, { pendingAnswers: s.snap.pendingAnswers.filter(a => pairKey(a.test, a.player) !== key) });
    }
  };

  const settingsChanged = () => { const s = getScope(); if (s) invalidate(s); };
  const confirmSession = async (request: SessionRequest): Promise<boolean> => {
    const s = getScope(); if (!s || s.saves.size || s.snap.mutationBusy) return false;
    const body = copy(request); const n = next(s, "session"); next(s, "status"); invalidate(s);
    publish(s, { mutationBusy: true, error: null });
    try {
      const result = await api.postSession(body, s.ctrl.signal);
      if (!current(s, "session", n)) return false;
      const status = await api.getStatus(result.kit_id, s.ctrl.signal);
      if (!current(s, "session", n)) return false;
      publish(s, { status, loading: false }); return true;
    } catch (err) { if (current(s, "session", n)) publish(s, { loading: false, error: message(err) }); return false; }
    finally { if (current(s, "session", n)) publish(s, { mutationBusy: false }); }
  };
  const loadPreview = async (): Promise<boolean> => {
    const s = getScope(); const kit = s?.snap.status?.kit?.id;
    if (!s || !kit || s.saves.size || s.snap.mutationBusy) return false;
    const n = next(s, "preview"); const revision = s.revision;
    publish(s, { preview: null, error: null });
    try {
      const preview = await api.postApply({ kit, preview: true, revision: null, confirm_veto: [] }, s.ctrl.signal);
      if (!current(s, "preview", n) || revision !== s.revision) return false;
      publish(s, { preview }); return true;
    } catch (err) { if (current(s, "preview", n) && revision === s.revision) publish(s, { error: message(err) }); return false; }
  };
  const mutate = async (kind: "apply" | "undo", confirmations: string[] = [], undoKit?: string): Promise<boolean> => {
    const s = getScope(); const kit = kind === "undo" && undoKit ? undoKit : s?.snap.status?.kit?.id;
    if (!s || !kit || s.saves.size || s.snap.mutationBusy) return false;
    const preview = s.snap.preview;
    if (kind === "apply" && !preview) { publish(s, { error: PREVIEW_AGAIN_TEXT }); return false; }
    const body = { kit, preview: false, revision: preview?.revision ?? null, confirm_veto: [...confirmations] };
    const n = next(s, "apply"); invalidate(s); publish(s, { mutationBusy: true, error: null, applyResult: null });
    try {
      const result = kind === "apply" ? await api.postApply(body, s.ctrl.signal) : await api.postUndo({ kit }, s.ctrl.signal);
      if (!current(s, "apply", n)) return false;
      publish(s, { applyResult: result }); return true;
    } catch (err) { if (current(s, "apply", n)) publish(s, { error: message(err) }); return false; }
    finally { if (current(s, "apply", n)) publish(s, { mutationBusy: false }); }
  };
  const plan = async (request: PlanRequest): Promise<boolean> => {
    const s = getScope(); if (!s) return false;
    const body = copy(request); const n = next(s, "plan");
    publish(s, { planDocument: null, planId: null, error: null });
    try {
      const result = await api.postPlan(body, s.ctrl.signal);
      if (!current(s, "plan", n)) return false;
      publish(s, api.isJobStarted(result) ? { planId: result.job_id } : { planDocument: result }); return true;
    } catch (err) { if (current(s, "plan", n)) publish(s, { error: message(err) }); return false; }
  };
  const build = async (request: BuildRequest): Promise<boolean> => {
    const s = getScope(); if (!s) return false;
    const body = copy(request); const n = next(s, "build");
    publish(s, { buildId: null, error: null });
    try {
      const result = await api.startBuild(body, s.ctrl.signal);
      if (!current(s, "build", n)) return false;
      publish(s, { buildId: result.job_id }); return true;
    } catch (err) { if (current(s, "build", n)) publish(s, { error: message(err) }); return false; }
  };
  const report = async (request: { paths: string[] }): Promise<boolean> => {
    const s = getScope(); const kit = s?.snap.status?.kit?.id;
    if (!s || !kit || s.saves.size || s.snap.mutationBusy) return false;
    const body = { kit, paths: [...request.paths] }; const n = next(s, "report"); const revision = s.revision;
    publish(s, { reportId: null, error: null });
    try {
      const result = await api.startReport(body, s.ctrl.signal);
      if (!current(s, "report", n) || revision !== s.revision) return false;
      publish(s, { reportId: result.job_id }); return true;
    } catch (err) { if (current(s, "report", n) && revision === s.revision) publish(s, { error: message(err) }); return false; }
  };
  const planDocument = planJob.status === "done" && isPlanDocument(planJob.results) ? planJob.results : snap.planDocument;
  const buildDocument = buildJob.status === "done" && isBuildDocument(buildJob.results) ? buildJob.results : null;
  const reportDocument = reportJob.status === "done" && isReportDocument(reportJob.results) ? reportJob.results : null;
  const invalidJob = (planJob.status === "done" && !isPlanDocument(planJob.results))
    || (buildJob.status === "done" && !isBuildDocument(buildJob.results))
    || (reportJob.status === "done" && !isReportDocument(reportJob.results));
  return { ...snap, error: invalidJob ? CALIBRATION_CONTRACT_TEXT : snap.error,
    answers: snap.status?.answers ?? [], planDocument, buildDocument, reportDocument, planJob, buildJob, reportJob,
    answer, refresh, confirmSession, settingsChanged, loadPreview, apply: (ids: string[] = []) => mutate("apply", ids), undo: (kit?: string) => mutate("undo", [], kit),
    plan, build, report };
}
