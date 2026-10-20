import { API } from "../suite/config";

export type ActiveJob = { job_id: string; kind: string; status: string; current: string };
export type AppStatus = {
  schema: "videosuite-app-status/1"; build_id: string; install_id: string; session_id: string;
  origin: string; ready: boolean; quit_pending: boolean;
  checks: { id: string; status: "ok" | "missing" | "error"; message: string; capability: string }[];
  active_jobs: ActiveJob[];
};
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export function parseAppStatus(value: unknown): AppStatus {
  if (!object(value) || value.schema !== "videosuite-app-status/1"
    || !["build_id", "install_id", "session_id", "origin"].every(k => typeof value[k] === "string")
    || typeof value.ready !== "boolean" || typeof value.quit_pending !== "boolean"
    || !Array.isArray(value.active_jobs) || !value.active_jobs.every(j => object(j)
      && ["job_id", "kind", "status", "current"].every(k => typeof j[k] === "string"))
    || !Array.isArray(value.checks) || !value.checks.every(c => object(c)
      && ["id", "message", "capability"].every(k => typeof c[k] === "string")
      && ["ok", "missing", "error"].includes(String(c.status)))) throw new Error("Invalid app status");
  return value as AppStatus;
}
export async function fetchAppStatus(): Promise<AppStatus> {
  const response = await fetch(API + "/app/status", { cache: "no-store" });
  if (!response.ok) throw new Error("App status unavailable");
  return parseAppStatus(await response.json());
}

// Module memory only. A different launch/session always bootstraps anew.
let credential: { token: string; session_id: string } | undefined;
export async function appControl(status: AppStatus, route: "quit" | "open-log", body: object) {
  if (credential?.session_id !== status.session_id) {
    credential = undefined;
    const response = await fetch(API + "/app/bootstrap", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", cache: "no-store",
    });
    if (!response.ok) throw new Error("App controls unavailable. Reopen the shortcut to reconnect.");
    const value: unknown = await response.json();
    if (!object(value) || typeof value.token !== "string" || value.session_id !== status.session_id) {
      throw new Error("App session changed. Reopen the shortcut to reconnect.");
    }
    credential = { token: value.token, session_id: status.session_id };
  }
  const response = await fetch(API + "/app/" + route, {
    method: "POST", headers: { "Content-Type": "application/json", "X-VideoSuite-Token": credential.token },
    body: JSON.stringify(body), cache: "no-store",
  });
  if (!response.ok) {
    credential = undefined;
    throw new Error("App control failed. Reconnect to check app status.");
  }
  const value: unknown = await response.json();
  if (route === "quit" && (!object(value) || value.accepted !== true)) {
    throw new Error("Stop is already in progress. Reconnect to check app status.");
  }
  return value;
}

export async function fetchActiveJob(id: string): Promise<{ status: string; current: string }> {
  const response = await fetch(API + "/job/" + encodeURIComponent(id), { cache: "no-store" });
  if (response.status === 404) return { status: "gone", current: "" };
  if (!response.ok) throw new Error("Job connection lost. Reconnect to retry.");
  const value: unknown = await response.json();
  if (!object(value) || typeof value.status !== "string") throw new Error("Invalid job status");
  return { status: value.status, current: typeof value.current === "string" ? value.current : "" };
}
