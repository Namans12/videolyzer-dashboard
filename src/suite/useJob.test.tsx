import { act, StrictMode } from "react";
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { backoffMs, classifyPollError, useJob } from "./useJob";
import { SuiteApiError } from "./api";
import { API } from "./config";
import { callsTo, installFetch, jsonResponse, unexpectedFetch } from "./testing/fetchMock";
import { jobOf, scoreDoc } from "./testing/fixtures";

const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
});
function sequence(...answers: Response[]) {
  let n = 0;
  return installFetch((url) => url === `${API}/job/j1`
    ? answers[Math.min(n++, answers.length - 1)] : unexpectedFetch(url));
}
const running = () => jsonResponse(200, jobOf());

describe("useJob", () => {
  it.each(["done", "error", "cancelled"])("does not abort a completed %s response, including later navigation", async status => {
    const fetch = sequence(jsonResponse(200, jobOf({ status })));
    const { result, unmount } = renderHook(() => useJob("j1"));
    await tick(0);
    expect(result.current.status).toBe(status);
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(false);
    await tick(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
    unmount();
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(false);
  });

  it("handles native AbortError rejections from navigation without retries or console errors", async () => {
    const errors = vi.spyOn(console, "error");
    const fetch = installFetch((_url, init) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    }));
    const { unmount } = renderHook(() => useJob("j1"));
    await tick(0);
    unmount();
    await tick(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(errors).not.toHaveBeenCalled();
  });

  it.each([{}, [], { status: "unexpected" }, { status: null }].map(body => [body]))("stops on malformed job %j", async body => {
    const fetch = sequence(jsonResponse(200, body));
    const { result } = renderHook(() => useJob("j1"));
    await tick(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(result.current).toMatchObject({ status: "error", errorKind: "contract", error: "The Dashboard returned an unusable job response." });
  });
  it("retries response body transport failures", async () => {
    const response = { ...running(), text: () => Promise.reject(new TypeError("terminated")) } as Response;
    const fetch = sequence(response, running());
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); expect(result.current.unreachable).toBe(true);
    await tick(2999); expect(fetch).toHaveBeenCalledTimes(1);
    await tick(1); expect(fetch).toHaveBeenCalledTimes(2); expect(result.current.unreachable).toBe(false);
  });
  it("leaves null idle without fetching", async () => {
    const fetch = installFetch(unexpectedFetch);
    const { result } = renderHook(() => useJob(null));
    await tick(5000);
    expect(result.current.status).toBe("idle");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("polls at response-relative intervals and retains done results", async () => {
    const doc = scoreDoc();
    const fetch = sequence(running(), running(), jsonResponse(200, jobOf({ status: "done", results: doc })));
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); expect(fetch).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("running");
    await tick(1499); expect(fetch).toHaveBeenCalledTimes(1);
    await tick(1); expect(fetch).toHaveBeenCalledTimes(2);
    await tick(1500); expect(fetch).toHaveBeenCalledTimes(3);
    expect(result.current.status).toBe("done");
    expect(result.current.results).toEqual(doc);
    await tick(10000); expect(fetch).toHaveBeenCalledTimes(3);
  });
  it.each(["cancelled", "error"] as const)("stops on %s and preserves error metadata", async (status) => {
    const detail = "not found: D:\\Media\\gone.mkv\nno readable files";
    const fetch = sequence(jsonResponse(200, jobOf({ status, error: "videosuite score exited with code 3.", error_kind: "no_files", error_detail: detail, exit_code: 3 })));
    const { result } = renderHook(() => useJob("j1"));
    await tick(0);
    expect(result.current).toMatchObject({ status, errorKind: "no_files", errorDetail: detail, exitCode: 3, error: "videosuite score exited with code 3." });
    await tick(10000); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each([
    [404, {}, "gone", null, null],
    [403, { detail: "origin not allowed" }, "error", null, "Access refused: origin not allowed"],
    [200, "<html>", "error", "contract", "The Dashboard returned an unusable job response."],
  ])("stops on HTTP %s", async (code, body, status, errorKind, error) => {
    const fetch = sequence(jsonResponse(code as number, body));
    const { result } = renderHook(() => useJob("j1"));
    await tick(10000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(result.current).toMatchObject({ status, errorKind, error, unreachable: false });
  });
  it("backs off network failures then resets after success", async () => {
    let n = 0;
    const fetch = installFetch((url) => {
      if (url !== `${API}/job/j1`) return unexpectedFetch(url);
      if (++n <= 2) throw new TypeError("Failed to fetch");
      return running();
    });
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); expect(result.current.unreachable).toBe(true);
    await tick(2999); expect(fetch).toHaveBeenCalledTimes(1);
    await tick(1); expect(fetch).toHaveBeenCalledTimes(2);
    await tick(5999); expect(fetch).toHaveBeenCalledTimes(2);
    await tick(1); expect(result.current.unreachable).toBe(false);
    await tick(1500); expect(fetch).toHaveBeenCalledTimes(4);
  });
  it("backs off 500 responses with a capped delay", async () => {
    const fetch = sequence(jsonResponse(500, {}));
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); expect(result.current.unreachable).toBe(true);
    const gaps = [3000, 6000, 12000, 15000, 15000];
    expect(gaps.map((_, i) => backoffMs(i + 1))).toEqual(gaps);
    for (const [i, gap] of gaps.entries()) {
      await tick(gap - 1); expect(fetch).toHaveBeenCalledTimes(i + 1);
      await tick(1); expect(fetch).toHaveBeenCalledTimes(i + 2);
    }
  });
  it("aborts on unmount and never overlaps slow GETs", async () => {
    const pending = deferred<Response>();
    const fetch = installFetch((url) => url === `${API}/job/j1` ? pending.promise : unexpectedFetch(url));
    const { unmount } = renderHook(() => useJob("j1"));
    await tick(10000); expect(fetch).toHaveBeenCalledTimes(1);
    const signal = fetch.mock.calls[0][1]?.signal;
    unmount(); expect(signal?.aborted).toBe(true);
    await act(async () => { pending.resolve(running()); });
    await tick(10000); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("waits 1500 ms after a slow answer before the next GET", async () => {
    const pending = deferred<Response>();
    const fetch = installFetch((url) => url === `${API}/job/j1` ? pending.promise : unexpectedFetch(url));
    renderHook(() => useJob("j1"));
    await tick(10000); expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(running()); });
    await tick(1499); expect(fetch).toHaveBeenCalledTimes(1);
    await tick(1); expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("ignores an old pending GET after replacement", async () => {
    const pending = deferred<Response>();
    installFetch((url) => {
      if (url === `${API}/job/A`) return pending.promise;
      if (url === `${API}/job/B`) return running();
      return unexpectedFetch(url);
    });
    const { result, rerender } = renderHook(({ id }) => useJob(id), { initialProps: { id: "A" } });
    await tick(0);
    rerender({ id: "B" });
    await tick(0);
    await act(async () => { pending.resolve(jsonResponse(200, jobOf({ status: "done", current: "stale" }))); });
    expect(result.current).toMatchObject({ status: "running", current: "" });
  });
  it("clears old data immediately when the id changes", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/job/A`) return jsonResponse(200, jobOf({ current: "A current", events: [{ msg: "old", ts: 1 }], results: scoreDoc() }));
      if (url === `${API}/job/B`) return running();
      return unexpectedFetch(url);
    });
    const { result, rerender } = renderHook(({ id }) => useJob(id), { initialProps: { id: "A" } });
    await tick(0);
    rerender({ id: "B" });
    expect(result.current).toMatchObject({ status: "running", current: "", events: [], results: null });
    await tick(10000); expect(callsTo(fetch, "/job/A")).toHaveLength(1);
  });
  it.each([false, true])("cancel shares the signal and waits for server confirmation (StrictMode=%s)", async (strict) => {
    let n = 0;
    const fetch = installFetch((url) => {
      if (url === `${API}/job/j1/cancel`) return jsonResponse(200, {});
      if (url === `${API}/job/j1`) return ++n === 1 ? running() : jsonResponse(200, jobOf({ status: "cancelled" }));
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useJob("j1"), strict ? { wrapper: StrictMode } : {});
    await tick(0); expect(fetch).toHaveBeenCalledTimes(1);
    await act(() => result.current.cancel());
    expect(result.current).toMatchObject({ status: "running", cancelRequested: true });
    const post = callsTo(fetch, "/job/j1/cancel");
    expect(post).toHaveLength(1); expect(post[0][1]?.method).toBe("POST");
    expect(post[0][1]?.signal).toBe(fetch.mock.calls[0][1]?.signal);
    expect(post[0][1]?.signal).toBeDefined();
    await tick(1500); expect(result.current.status).toBe("cancelled");
    expect(result.current.cancelRequested).toBe(true);
  });
  it.each([false, true])("cancel 404 stops even with a pending GET (%s)", async (slow) => {
    const pending = deferred<Response>(); let n = 0;
    const fetch = installFetch((url) => {
      if (url === `${API}/job/j1/cancel`) return jsonResponse(404, {});
      if (url === `${API}/job/j1`) return ++n > 1 && slow ? pending.promise : running();
      return unexpectedFetch(url);
    });
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); if (slow) await tick(1500);
    await act(() => result.current.cancel());
    expect(result.current.status).toBe("gone");
    if (slow) {
      expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
      await act(async () => { pending.resolve(running()); });
    }
    await tick(10000); expect(result.current.status).toBe("gone");
    expect(callsTo(fetch, "/job/j1")).toHaveLength(slow ? 2 : 1);
  });
  it("continues polling after failed cancel", async () => {
    const fetch = installFetch((url) => {
      if (url === `${API}/job/j1/cancel`) throw new TypeError("Failed to fetch");
      return url === `${API}/job/j1` ? running() : unexpectedFetch(url);
    });
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); await act(() => result.current.cancel());
    expect(result.current).toMatchObject({ cancelFailed: true, cancelRequested: false });
    await tick(1500); expect(callsTo(fetch, "/job/j1")).toHaveLength(2);
  });
  it.each(["replace", "unmount"])("ignores late cancel rejection after %s and guards double cancel", async (action) => {
    const pending = deferred<Response>();
    const fetch = installFetch((url) => {
      if (url === `${API}/job/j1/cancel`) return pending.promise;
      if (url === `${API}/job/j1` || url === `${API}/job/B`) return running();
      return unexpectedFetch(url);
    });
    const { result, rerender, unmount } = renderHook(({ id }) => useJob(id), { initialProps: { id: "j1" } });
    await tick(0);
    let cancel!: Promise<void>;
    act(() => { cancel = result.current.cancel(); void result.current.cancel(); });
    expect(callsTo(fetch, "/job/j1/cancel")).toHaveLength(1);
    if (action === "replace") rerender({ id: "B" }); else unmount();
    const spy = vi.spyOn(console, "error");
    try {
      await act(async () => { pending.reject(new TypeError("Failed to fetch")); await cancel; });
      if (action === "replace") expect(result.current).toMatchObject({ cancelFailed: false, cancelRequested: false });
      expect(spy).not.toHaveBeenCalled();
    } finally { spy.mockRestore(); }
  });
  it("does not cancel a completed job", async () => {
    const fetch = sequence(jsonResponse(200, jobOf({ status: "done" })));
    const { result } = renderHook(() => useJob("j1"));
    await tick(0); await act(() => result.current.cancel());
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("classifies poll errors", () => {
    expect([
      new SuiteApiError(404, ""), new SuiteApiError(0, ""), new SuiteApiError(502, ""),
      new SuiteApiError(403, ""), new SuiteApiError(200, "", "bad_response"), new Error("x"),
    ].map(classifyPollError)).toEqual(["gone", "retry", "retry", "fatal", "contract", "retry"]);
  });
});
