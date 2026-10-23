import { act } from "react";
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useComparison } from "./useComparison";
import { API } from "../suite/config";
import { EVIDENCE_WORDS } from "../suite/types";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "../suite/testing/fetchMock";
import { localJustPlayerAlternatives, magnetQuickOverall, publicSnapshotsHttp } from "./testing/fixtures";
import type { Json } from "./testing/fixtures";
import type { QuickDocument, RunRequest } from "./types";

/** The exact plain message the backend's planning job ends with when the OpenSSL files are missing (L5/L6/U6). */
const OPENSSL_TEXT = "Magnet links are unavailable on this PC (OpenSSL 1.1 files not found); file comparison still works";

/** A failed planning job built from the real magnet run snapshot and the real plan job: the run ends `error`, has no
 *  RunResult, and the job carries the backend's message (comparison_runs.plan_run). */
function failedPlanning(): { failedRun: Json; failedJob: Json; jobId: string } {
  const run = magnetQuickOverall().run as Json;
  const jobId = "00000000000000000000000000000009";
  const failedRun: Json = { ...run, stage: "error", job_id: jobId };
  const planJob = publicSnapshotsHttp().plan_job as Json;
  const failedJob: Json = {
    ...planJob, status: "error", error: OPENSSL_TEXT, error_kind: "magnet_unavailable",
    events: [{ msg: "Planning 2 link(s)", ts: 1700000000 }, { msg: `Error: ${OPENSSL_TEXT}`, ts: 1700000000 }], run: failedRun,
  };
  return { failedRun, failedJob, jobId };
}

const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
});

function evidenceWords(doc: QuickDocument): string[] {
  const out: string[] = [];
  for (const order of Object.values(doc.devices)) {
    for (const group of order.groups) {
      for (const row of Object.values(group.basis)) out.push(String((row.playback as { evidence?: unknown }).evidence));
    }
  }
  return out;
}

describe("useComparison", () => {
  it("reconnects real Just Player plus alternatives without changing the primary", async () => {
    const detail = localJustPlayerAlternatives();
    const listeners = vi.spyOn(window, "addEventListener");
    const fetch = installFetch((url) => {
      if (url === `${API}/comparison/runs`) return jsonResponse(200, { runs: [detail.run] });
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, detail);
      return unexpectedFetch(url);
    });
    for (let mount = 0; mount < 2; mount += 1) {
      const { result, unmount } = renderHook(() => useComparison());
      await tick(0);
      const run = result.current.run!;
      expect(run.request).toMatchObject({ primary_player: "just_player", player: "all", include_alternatives: true });
      expect(run.conditions).toMatchObject({ primary_player: "just_player", player: "all", evaluated_players: ["builtin_usb", "kodi", "just_player"] });
      const device = result.current.result!.device as QuickDocument;
      expect(device.selected).toEqual({ device: "sony_bravia_8_ii", player: "just_player" });
      expect(device.alternatives.map((a) => a.player)).toEqual(["builtin_usb", "kodi"]);
      for (const word of evidenceWords(device)) expect(EVIDENCE_WORDS).toContain(word);
      expect(result.current.busy).toBe(false);
      await tick(20000);
      unmount();
    }
    // done runs are read once per reconnect: list + detail, no job polling
    expect(callsTo(fetch, "/comparison/runs")).toHaveLength(2);
    expect(callsTo(fetch, "/comparison/runs/run-0001")).toHaveLength(2);
    expect(listeners.mock.calls.map((c) => c[0])).not.toContain("beforeunload");
    expect(listeners.mock.calls.map((c) => c[0])).not.toContain("unload");
    expect(localStorage.length).toBe(0);
  });

  it("plans, waits for confirmation, confirms exactly once and stops polling when done", async () => {
    const http = publicSnapshotsHttp();
    const planJob = http.plan_job as Json;
    const pending = { run: planJob.run, result: null };
    let confirmed = false;
    const fetch = installFetch((url, init) => {
      if (url === `${API}/comparison/runs`) {
        if (init?.method !== "POST") return jsonResponse(200, { runs: [] });
        confirmed = true;
        return jsonResponse(200, { job_id: "run-job", run_id: "run-0001" });
      }
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, planJob);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, confirmed ? http.detail : pending);
      if (url === `${API}/job/run-job`) return jsonResponse(200, http.run_job);
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    expect(result.current.run).toBeNull();
    const request = (planJob.run as Json).request as RunRequest;
    await act(async () => { result.current.plan(request); });
    await tick(0);
    expect(bodyOf(fetch, "/comparison/plans")).toEqual(request);
    expect(result.current.run?.stage).toBe("awaiting_confirmation");
    expect(result.current.busy).toBe(false);
    await act(async () => { result.current.start(); result.current.start(); });
    await tick(0);
    await tick(0);
    expect(callsTo(fetch, "/comparison/runs").filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(bodyOf(fetch, "/comparison/runs", 1)).toEqual({ run_id: "run-0001", plan_revision: "rev-302aa61015d20d93" });
    expect(result.current.run?.stage).toBe("done");
    expect(result.current.result?.run_id).toBe("run-0001");
    const polls = callsTo(fetch, "/job/run-job").length;
    await tick(30000);
    expect(callsTo(fetch, "/job/run-job")).toHaveLength(polls);
    await act(async () => { result.current.start(); });
    await tick(0);
    expect(callsTo(fetch, "/comparison/runs").filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("keeps a failed planning job's own message, also after a reconnect", async () => {
    const { failedRun, failedJob, jobId } = failedPlanning();
    const fetch = installFetch((url, init) => {
      if (url === `${API}/comparison/runs` && init?.method !== "POST") return jsonResponse(200, { runs: [failedRun] });
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: jobId, run_id: failedRun.run_id });
      if (url === `${API}/job/${jobId}`) return jsonResponse(200, failedJob);
      if (url === `${API}/comparison/runs/${String(failedRun.run_id)}`) return jsonResponse(200, { run: failedRun, result: null });
      return unexpectedFetch(url);
    });
    const first = renderHook(() => useComparison());
    await tick(0);
    await tick(10);
    expect(first.result.current.job).toMatchObject({ status: "error", error: OPENSSL_TEXT });   // reconnect read it
    await act(async () => { first.result.current.reset(); });
    await act(async () => { first.result.current.plan((failedRun.request) as RunRequest); });
    await tick(0);
    await tick(10);
    expect(first.result.current.run?.stage).toBe("error");
    expect(first.result.current.result).toBeNull();
    expect(first.result.current.job).toMatchObject({ status: "error", error: OPENSSL_TEXT });
    expect(first.result.current.busy).toBe(false);
    first.unmount();
    expect(callsTo(fetch, `/job/${jobId}`).length).toBeGreaterThanOrEqual(2);
  });

  it("lets Start be retried after a busy refusal and still sends one request per double click", async () => {
    const http = publicSnapshotsHttp();
    const planJob = http.plan_job as Json;
    const pending = { run: planJob.run, result: null };
    let posts = 0;
    const fetch = installFetch((url, init) => {
      if (url === `${API}/comparison/runs`) {
        if (init?.method !== "POST") return jsonResponse(200, { runs: [] });
        posts += 1;
        if (posts === 1) return jsonResponse(409, { detail: "Another comparison is using the computer. Try again when it finishes." });
        return jsonResponse(200, { job_id: "run-job", run_id: "run-0001" });
      }
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, planJob);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, posts >= 2 ? http.detail : pending);
      if (url === `${API}/job/run-job`) return jsonResponse(200, http.run_job);
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    await act(async () => { result.current.plan((planJob.run as Json).request as RunRequest); });
    await tick(0);
    await act(async () => { result.current.start(); result.current.start(); });
    await tick(0);
    await tick(0);
    const confirms = () => callsTo(fetch, "/comparison/runs").filter(([, init]) => init?.method === "POST").length;
    expect(confirms()).toBe(1);
    expect(result.current.run?.stage).toBe("awaiting_confirmation");
    expect(result.current.error).toContain("Another comparison is using the computer");
    await act(async () => { result.current.start(); result.current.start(); });
    await tick(0);
    await tick(0);
    expect(confirms()).toBe(2);
    expect(result.current.run?.stage).toBe("done");
    expect(result.current.error).toBeNull();
  });

  it("cancels its own pending plan before planning again", async () => {
    const http = publicSnapshotsHttp();
    const planJob = http.plan_job as Json;
    const fetch = installFetch((url, init) => {
      if (url === `${API}/comparison/runs` && init?.method !== "POST") return jsonResponse(200, { runs: [] });
      if (url === `${API}/comparison/plans`) return jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, planJob);
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, { run: planJob.run, result: null });
      if (url === `${API}/comparison/runs/run-0001/cancel`) return jsonResponse(200, { run_id: "run-0001", stage: "cancelled" });
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    const request = (planJob.run as Json).request as RunRequest;
    await act(async () => { result.current.plan(request); });
    await tick(0);
    expect(result.current.run?.stage).toBe("awaiting_confirmation");
    await act(async () => { result.current.plan(request); });
    await tick(0);
    const order = fetch.mock.calls.map((c) => `${(c[1] as RequestInit | undefined)?.method ?? "GET"} ${String(c[0]).slice(API.length)}`)
      .filter((s) => s.startsWith("POST"));
    expect(order).toEqual(["POST /comparison/plans", "POST /comparison/runs/run-0001/cancel", "POST /comparison/plans"]);
    expect(bodyOf(fetch, "/comparison/runs/run-0001/cancel")).toEqual({});
  });

  it("prefers the newest finished run on reconnect and offers to discard an older pending plan", async () => {
    const done = localJustPlayerAlternatives();
    const oldPlan = { ...((publicSnapshotsHttp().plan_job as Json).run as Json), run_id: "run-0000" };
    const fetch = installFetch((url, init) => {
      if (url === `${API}/comparison/runs` && init?.method !== "POST") {
        return jsonResponse(200, { runs: callsTo(fetch, "/comparison/runs/run-0000/cancel").length ? [done.run] : [done.run, oldPlan] });
      }
      if (url === `${API}/comparison/runs/run-0001`) return jsonResponse(200, done);
      if (url === `${API}/comparison/runs/run-0000/cancel`) return jsonResponse(200, { run_id: "run-0000", stage: "cancelled" });
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    expect(result.current.run?.run_id).toBe("run-0001");
    expect(result.current.run?.stage).toBe("done");
    expect(result.current.leftoverPlans.map((r) => r.run_id)).toEqual(["run-0000"]);
    await act(async () => { result.current.discardLeftoverPlans(); });
    await tick(0);
    expect(callsTo(fetch, "/comparison/runs/run-0000/cancel")).toHaveLength(1);
    expect(result.current.leftoverPlans).toEqual([]);
    expect(result.current.run?.run_id).toBe("run-0001");
  });

  it("clears a shown error when the owner changes the inputs", async () => {
    installFetch((url) => {
      if (url === `${API}/comparison/runs`) return jsonResponse(200, { runs: [] });
      if (url === `${API}/comparison/plans`) return jsonResponse(404, { detail: "Not found: path not found" });
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    const request = ((publicSnapshotsHttp().plan_job as Json).run as Json).request as RunRequest;
    await act(async () => { result.current.plan(request); });
    await tick(0);
    expect(result.current.error).not.toBeNull();
    await act(async () => { result.current.dismissError(); });
    expect(result.current.error).toBeNull();
  });

  it("ignores a stale answer for a run that is no longer shown and stops on a purged job", async () => {
    const http = publicSnapshotsHttp();
    let releaseFirst: (r: Response) => void = () => undefined;
    const fetch = installFetch((url) => {
      if (url === `${API}/comparison/runs`) return jsonResponse(200, { runs: [] });
      if (url === `${API}/comparison/plans`) {
        const n = callsTo(fetch, "/comparison/plans").length;
        if (n === 1) return new Promise<Response>((resolve) => { releaseFirst = resolve; });
        return jsonResponse(200, { job_id: "gone-job", run_id: "run-0002" });
      }
      if (url === `${API}/job/gone-job`) return jsonResponse(404, { detail: "Job not found." });
      if (url === `${API}/comparison/runs/run-0002`) return jsonResponse(404, { detail: "Run not found." });
      if (url === `${API}/job/plan-job`) return jsonResponse(200, http.plan_job);
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useComparison());
    await tick(0);
    const request = ((http.plan_job as Json).run as Json).request as RunRequest;
    await act(async () => { result.current.plan(request); });
    await act(async () => { result.current.plan(request); });
    await tick(0);
    expect(result.current.gone).toBe(true);
    releaseFirst(jsonResponse(200, { job_id: "plan-job", run_id: "run-0001" }));
    await tick(5000);
    expect(callsTo(fetch, "/job/plan-job")).toHaveLength(0);
    expect(result.current.run).toBeNull();
    const before = fetch.mock.calls.length;
    await tick(30000);
    expect(fetch.mock.calls.length).toBe(before);
  });
});
