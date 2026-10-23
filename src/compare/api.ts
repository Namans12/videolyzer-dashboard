// src/compare/api.ts — the unified comparison run API (POST /comparison/plans, /comparison/runs, GET runs/detail, job
// polling) plus the privileged native file chooser. Every answer passes the contract guards in ./types; a broken or
// leaking answer becomes SuiteApiError "bad_response" (its message never contains the received values).
import { API } from "../suite/config";
import { postJson, request, SuiteApiError } from "../suite/api";
import {
  asRunDetail, asRunJob, asRunList, asRunStarted, ContractViolation,
} from "./types";
import type { RunDetail, RunJob, RunList, RunRequest, RunStarted } from "./types";

function guarded<T>(guard: (value: unknown) => T, value: unknown): T {
  try {
    return guard(value);
  } catch (err) {
    if (err instanceof ContractViolation) throw new SuiteApiError(200, "", "bad_response");
    throw err;
  }
}

export async function planRun(body: RunRequest, signal?: AbortSignal): Promise<RunStarted> {
  return guarded(asRunStarted, await postJson<unknown>("/comparison/plans", body, signal));
}

/** Confirms exactly this immutable plan revision (the backend refuses a stale or second confirmation with 409). */
export async function confirmRun(run_id: string, plan_revision: string, signal?: AbortSignal): Promise<RunStarted> {
  return guarded(asRunStarted, await postJson<unknown>("/comparison/runs", { run_id, plan_revision }, signal));
}

export async function listRuns(signal?: AbortSignal): Promise<RunList> {
  return guarded(asRunList, await request<unknown>("/comparison/runs", { method: "GET" }, signal));
}

export async function getRun(run_id: string, signal?: AbortSignal): Promise<RunDetail> {
  return guarded(asRunDetail, await request<unknown>(`/comparison/runs/${encodeURIComponent(run_id)}`, { method: "GET" }, signal));
}

/** Cancels a pending plan (or asks the run's job to stop at its next boundary). */
export async function cancelRun(run_id: string, signal?: AbortSignal): Promise<{ run_id: string; stage: string }> {
  const out = await postJson<{ run_id?: unknown; stage?: unknown }>(
    `/comparison/runs/${encodeURIComponent(run_id)}/cancel`, {}, signal);
  if (typeof out.run_id !== "string" || typeof out.stage !== "string") throw new SuiteApiError(200, "", "bad_response");
  return { run_id: out.run_id, stage: out.stage };
}

export async function getRunJob(job_id: string, signal?: AbortSignal): Promise<RunJob> {
  return guarded(asRunJob, await request<unknown>(`/job/${encodeURIComponent(job_id)}`, { method: "GET" }, signal));
}

// ── native chooser (privileged: per-launch token kept in module memory only) ─────
let token: string | null = null;

async function bootstrap(): Promise<string> {
  const out = await postJson<{ token?: unknown }>("/app/bootstrap", {});
  if (typeof out.token !== "string" || out.token === "") throw new SuiteApiError(200, "", "bad_response");
  return out.token;
}

async function chooserCall(secret: string): Promise<Response> {
  try {
    return await fetch(`${API}/app/choose-files`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-VideoSuite-Token": secret }, body: "{}", cache: "no-store",
    });
  } catch {
    throw new SuiteApiError(0, "");
  }
}

/** Opens the Windows file dialog through the backend. Paths are selected, never uploaded or copied. */
export async function chooseFiles(): Promise<{ paths: string[]; cancelled: boolean }> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (token === null) token = await bootstrap();
    const res = await chooserCall(token);
    let body: unknown;
    try { body = JSON.parse(await res.text()) as unknown; } catch { body = null; }
    if (res.status === 403 && attempt === 0) { token = null; continue; }
    if (!res.ok) {
      if (res.status === 403) token = null;
      const detail = body !== null && typeof body === "object" && typeof (body as { detail?: unknown }).detail === "string"
        ? (body as { detail: string }).detail : "";
      throw new SuiteApiError(res.status, detail);
    }
    const paths = (body as { paths?: unknown } | null)?.paths;
    const cancelled = (body as { cancelled?: unknown } | null)?.cancelled;
    if (!Array.isArray(paths) || !paths.every((p) => typeof p === "string") || typeof cancelled !== "boolean") {
      throw new SuiteApiError(200, "", "bad_response");
    }
    return { paths: paths as string[], cancelled };
  }
  throw new SuiteApiError(403, "App control authorization required.");
}
