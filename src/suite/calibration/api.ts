import { request, startJob, SuiteApiError } from "../api";
import type { JobStarted } from "../types";
import { isApplyDocument, isPlanDocument, isRecordDocument, isSessionDocument, isStatusDocument, own } from "./types";
import type { ApplyRequest, BuildRequest, PlanDocument, PlanRequest, RecordRequest, ReportRequest, SessionRequest, UndoRequest } from "./types";

const BASE = "/suite/calibration";
async function checked<T>(path: string, init: RequestInit, guard: (v: unknown) => v is T, signal?: AbortSignal): Promise<T> {
  const doc = await request<unknown>(BASE + path, init, signal);
  if (!guard(doc)) throw new SuiteApiError(200, "", "bad_response");
  return doc;
}
function post(body: unknown): RequestInit { return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }; }
export function singleLineNote(note: string): string { return note.replace(/[\r\n\u0085\u2028\u2029]+/g, " "); }
export async function postPlan(body: PlanRequest, signal?: AbortSignal): Promise<PlanDocument | JobStarted> {
  if (body.deep_dv) return startJob(BASE + "/plan", body, signal);
  return checked("/plan", post(body), isPlanDocument, signal);
}
export function getStatus(kit?: string | null, signal?: AbortSignal) {
  return checked(`/status${kit ? `?kit=${encodeURIComponent(kit)}` : ""}`, { method: "GET" }, isStatusDocument, signal);
}
export function postSession(body: SessionRequest, signal?: AbortSignal) { return checked("/session", post(body), isSessionDocument, signal); }
export function postRecord(body: RecordRequest, signal?: AbortSignal) {
  return checked("/record", post({ ...body, note: singleLineNote(body.note) }), isRecordDocument, signal);
}
export function postApply(body: ApplyRequest, signal?: AbortSignal) { return checked("/apply", post(body), isApplyDocument, signal); }
export function postUndo(body: UndoRequest, signal?: AbortSignal) { return checked("/undo", post(body), isApplyDocument, signal); }
export function startBuild(body: BuildRequest, signal?: AbortSignal) { return startJob(BASE + "/build", body, signal); }
export function startReport(body: ReportRequest, signal?: AbortSignal) { return startJob(BASE + "/report", body, signal); }
export function isJobStarted(v: unknown): v is JobStarted { const id = own(v, "job_id"); return typeof id === "string" && id !== ""; }
