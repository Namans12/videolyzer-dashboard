import { describe, expect, it } from "vitest";
import { CANCELLED_TEXT, JOB_GONE_TEXT, TERMINAL_STATUSES, isTerminalStatus, pollOutcome } from "./jobStatus";

describe("job terminal statuses", () => {
  it("defines exactly the backend terminal statuses", () => {
    expect(TERMINAL_STATUSES).toEqual(["done", "error", "cancelled"]);
    expect(CANCELLED_TEXT).toBe("Cancelled");
    expect(JOB_GONE_TEXT).toBe("This job is no longer known to the Dashboard (it was cleaned up or the backend restarted). Start it again.");
  });
  it.each(["done", "error", "cancelled"])("recognizes %s", (status) => {
    expect(isTerminalStatus(status)).toBe(true);
  });
  it.each(["running", "queued", "", undefined, null, 5, "DONE"])("rejects %s", (status) => {
    expect(isTerminalStatus(status)).toBe(false);
  });
  it.each([
    [200, { status: "running" }, "continue"],
    [200, { status: "done" }, "done"],
    [200, { status: "error" }, "error"],
    [200, { status: "cancelled" }, "cancelled"],
    [404, { detail: "Job not found." }, "gone"],
    [500, {}, "error"], [200, null, "continue"], [200, "garbage", "continue"],
    [199, { status: "done" }, "error"], [300, { status: "done" }, "error"],
  ])("interprets HTTP %s with %j as %s", (http, body, outcome) => {
    expect(pollOutcome(http as number, body)).toBe(outcome);
  });
});
