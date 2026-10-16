// src/jobStatus.ts — one definition of "finished" for every poller in the UI (backend: jobs.TERMINAL).
export const TERMINAL_STATUSES = ["done", "error", "cancelled"] as const;
export type TerminalStatus = (typeof TERMINAL_STATUSES)[number];

export function isTerminalStatus(status: unknown): status is TerminalStatus {
  return typeof status === "string" && (TERMINAL_STATUSES as readonly string[]).includes(status);
}

export type PollOutcome = "continue" | "done" | "error" | "cancelled" | "gone";

/** What one GET /job/{id} answer means for a poller: 404 = purged, other non-2xx = failed. */
export function pollOutcome(httpStatus: number, body: unknown): PollOutcome {
  if (httpStatus === 404) return "gone";
  if (httpStatus < 200 || httpStatus >= 300) return "error";
  const status = body !== null && typeof body === "object" ? (body as { status?: unknown }).status : undefined;
  return isTerminalStatus(status) ? status : "continue";
}

export const CANCELLED_TEXT = "Cancelled";
export const JOB_GONE_TEXT =
  "This job is no longer known to the Dashboard (it was cleaned up or the backend restarted). Start it again.";
