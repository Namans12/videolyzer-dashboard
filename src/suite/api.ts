// src/suite/api.ts — the only place the Suite tab talks HTTP. Errors become SuiteApiError; text stays text.
import { API } from "./config";
import { apiErrorSentence, detailText } from "./messages";
import type {
  CompareRequest, DevicesDocument, Job, JobStarted, RankRequest, ScoreRequest, SuiteSettings, VersionInfo,
} from "./types";

export type ApiErrorKind =
  | "network" | "bad_request" | "forbidden" | "not_found" | "conflict" | "unsupported_media"
  | "validation" | "unavailable" | "server" | "bad_response";

export function apiErrorKind(status: number): ApiErrorKind {
  switch (status) {
    case 0: return "network";
    case 400: return "bad_request";
    case 403: return "forbidden";
    case 404: return "not_found";
    case 409: return "conflict";
    case 415: return "unsupported_media";
    case 422: return "validation";
    case 503: return "unavailable";
    default: return "server";
  }
}

export class SuiteApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly kind: ApiErrorKind;

  constructor(status: number, detail: string, kind: ApiErrorKind = apiErrorKind(status)) {
    super(detail ? `HTTP ${status}: ${detail}` : `HTTP ${status}`);
    this.name = "SuiteApiError";
    this.status = status;
    this.detail = detail;
    this.kind = kind;
  }
}

/** The sentence for any error a client call can throw (unknown errors count as "backend not answering"). */
export function errorSentence(err: unknown): string {
  return err instanceof SuiteApiError ? apiErrorSentence(err.status, err.detail) : apiErrorSentence(0, "");
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function request<T>(path: string, init: RequestInit, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { ...init, signal });
  } catch (err) {
    if (signal?.aborted) throw err;              // the caller aborted: not an error to show
    throw new SuiteApiError(0, "");
  }
  let text: string;
  try { text = await res.text(); } catch (err) {
    if (signal?.aborted) throw err;
    throw new SuiteApiError(0, "");
  }
  let body: unknown;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!res.ok) throw new SuiteApiError(res.status, detailText(body));
  if (body === null || typeof body !== "object") throw new SuiteApiError(res.status, "", "bad_response");
  return body as T;
}

/** POST a JSON body and return the parsed JSON object (the caller narrows it with its own guard). */
export function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) }, signal);
}

export async function startJob(path: string, body: unknown, signal?: AbortSignal): Promise<JobStarted> {
  const out = await request<{ job_id?: unknown }>(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) }, signal);
  if (typeof out.job_id !== "string" || out.job_id === "") throw new SuiteApiError(200, "", "bad_response");
  return { job_id: out.job_id };
}

export function getVersion(refresh = false, signal?: AbortSignal): Promise<VersionInfo> {
  return request<VersionInfo>(refresh ? "/suite/version?refresh=true" : "/suite/version", { method: "GET" }, signal);
}
export function getDevices(signal?: AbortSignal, refresh = false): Promise<DevicesDocument> {
  return request<DevicesDocument>(refresh ? "/suite/devices?refresh=true" : "/suite/devices", { method: "GET" }, signal);
}
export function getSettings(signal?: AbortSignal, refresh = false): Promise<SuiteSettings> {
  return request<SuiteSettings>(refresh ? "/suite/settings?refresh=true" : "/suite/settings", { method: "GET" }, signal);
}
export function postScore(body: ScoreRequest, signal?: AbortSignal): Promise<JobStarted> {
  return startJob("/suite/score", body, signal);
}
export function postRank(body: RankRequest, signal?: AbortSignal): Promise<JobStarted> {
  return startJob("/suite/rank", body, signal);
}
export function postCompare(body: CompareRequest, signal?: AbortSignal): Promise<JobStarted> {
  return startJob("/suite/compare", body, signal);
}
export async function getJob(id: string, signal?: AbortSignal): Promise<Job> {
  const job = await request<Job>(`/job/${encodeURIComponent(id)}`, { method: "GET" }, signal);
  if (Array.isArray(job) || !["running", "done", "error", "cancelled"].includes(job.status)) {
    throw new SuiteApiError(200, "", "bad_response");
  }
  return job;
}
export async function cancelJob(id: string, signal?: AbortSignal): Promise<void> {
  await request<{ ok?: boolean }>(`/job/${encodeURIComponent(id)}/cancel`, { method: "POST" }, signal);
}
