import { act, StrictMode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ComparePanel } from "./ComparePanel";
import { isCompareDocument, isRankDocument, shouldRank } from "./compare";
import { API } from "./config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "./testing/fetchMock";
import { compareDoc, devicesDoc, jobOf, rankDoc } from "./testing/fixtures";
import { CANCELLING_TEXT, LOG_SUMMARY_TEXT, SATURATED_TEXT, SATURATION_NOTE, SUITE_DISABLED_TEXT } from "./messages";

const paths = ["C:\\fixtures\\suite\\clip_a_crf18.mkv", "C:\\fixtures\\suite\\clip_b_crf34.mkv", "C:\\fixtures\\suite\\clip_c_crf26.mp4"];
const out = "D:\\Reports\\run 1";
const tick = async (ms = 0) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  // Each POST completion commits a render, whose effect schedules the next 0 ms handoff.
  for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(0); });
};
const props = () => ({ paths, options: { lang: "eng" }, disabledReason: null, devices: devicesDoc(), onSettingsBlock: vi.fn() });
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
const lines = [
  "3 file(s) shortlisted, 3 pair(s), roughly 12 minutes (fast; rough estimate, Dolby Vision RPU reading with --deep-dv adds several minutes per file)",
  "  [1/3] Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: started (fast)",
  "  [1/3] Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: 25% Analyzed 1/69 frame pairs (streaming)... (0 min elapsed, about 1 min left)",
  "  Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: done",
  "  [2/3] Tom & Jerry (2021) 100%.mkv vs plain_b.mp4: started (fast)",
  "  [2/3] Tom & Jerry (2021) 100%.mkv vs plain_b.mp4: 25% Analyzed 1/69 frame pairs (streaming)... (0 min elapsed, about 1 min left)",
];
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
function start() { fireEvent.change(screen.getByLabelText("Folder for the comparison reports"), { target: { value: out } }); fireEvent.click(screen.getByRole("button", { name: "Compare and rank" })); }
describe("compare chain", () => {
  it("disables accepted rank cancellation and sends exactly one cancel POST", async () => {
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "done", results: compareDoc() }));
      if (url === API + "/suite/rank") return jsonResponse(200, { job_id: "r1" });
      if (url === API + "/job/r1") return jsonResponse(200, jobOf());
      if (url === API + "/job/r1/cancel") return jsonResponse(200, {});
      return unexpectedFetch(url);
    });
    render(<ComparePanel {...props()} />); start(); await tick(10);
    const cancel = screen.getByRole("button", { name: "Cancel ranking" });
    fireEvent.click(cancel); await tick();
    expect(cancel).toBeDisabled();
    fireEvent.click(cancel); await tick();
    expect(callsTo(fetch, "/job/r1/cancel")).toHaveLength(1);
  });
  it("guards document shapes and rank eligibility", () => {
    expect(isCompareDocument(compareDoc())).toBe(true); expect(isCompareDocument({})).toBe(false);
    expect(isRankDocument(rankDoc())).toBe(true); expect(isRankDocument({ quality: [], devices: null })).toBe(false);
    expect(shouldRank({ status: "error", errorKind: "failed_pairs", results: compareDoc() })).toBe(true);
    expect(shouldRank({ status: "error", errorKind: "timeout", results: compareDoc() })).toBe(false);
  });
  it("disables with a reason and validates file count, folder and top without requests", async () => {
    const fetch = installFetch(unexpectedFetch); const p = props();
    const { rerender } = render(<ComparePanel {...p} disabledReason={SUITE_DISABLED_TEXT} />);
    expect(screen.getByText(SUITE_DISABLED_TEXT)).toBeVisible(); expect(screen.getByRole("button")).toBeDisabled();
    rerender(<ComparePanel {...p} paths={paths.slice(0, 1)} />); start();
    expect(screen.getByText("Choose at least two video files to compare.")).toBeVisible();
    rerender(<ComparePanel {...p} />); fireEvent.change(screen.getByLabelText("Folder for the comparison reports"), { target: { value: "D:\\R&D" } });
    fireEvent.click(screen.getByText("Compare and rank")); expect(screen.getByText(/The folder path cannot contain/)).toBeVisible();
    fireEvent.change(screen.getByLabelText(/Files kept/), { target: { value: "7" } }); fireEvent.click(screen.getByText("Compare and rank"));
    expect(screen.getByText("Top must be a whole number from 1 to 6.")).toBeVisible(); await tick(); expect(fetch).not.toHaveBeenCalled();
  });
  it.each([false, true])("runs compare then rank exactly once (StrictMode=%s)", async strict => {
    let n = 0; const p = props();
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/suite/rank") return jsonResponse(200, { job_id: "r1" });
      if (url === API + "/job/c1") return jsonResponse(200, ++n === 1 ? jobOf({ current: "comparing", events: lines.map(msg => ({ msg, ts: 0 })) }) : jobOf({ status: "done", results: compareDoc() }));
      if (url === API + "/job/r1") return jsonResponse(200, jobOf({ status: "done", results: rankDoc() }));
      return unexpectedFetch(url);
    });
    render(<ComparePanel {...p} />, strict ? { wrapper: StrictMode } : {});
    expect(screen.getByText("3 file(s) chosen; up to 3 are compared (at most 3 pair(s)).")).toBeVisible();
    start(); await tick(1);
    expect(bodyOf(fetch, "/suite/compare")).toEqual({ paths, out_dir: out, mode: "fast", top: 4 });
    expect(screen.getByText("VideoSuite's own rough estimate: about 12 minutes for 3 pair(s) (fast).")).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "25");
    expect(screen.getByText(/Pair 2 of 3:.*25%/)).toBeVisible(); expect(screen.getByText(/1 of 3 pair\(s\) finished/)).toBeVisible();
    await tick(1600); await tick(5);
    expect(bodyOf(fetch, "/suite/rank")).toEqual({ paths, reports_dir: out, lang: "eng" });
    expect(screen.getByText("Picture quality (device-neutral)")).toBeVisible();
    await tick(10000); expect(callsTo(fetch, "/suite/rank")).toHaveLength(1);
    expect(callsTo(fetch, "/job/c1")).toHaveLength(2); expect(callsTo(fetch, "/job/r1")).toHaveLength(1);
    expect(p.onSettingsBlock.mock.calls.map(c => c[1])).toEqual(["compare:c1", "rank:r1"]);
  });
  it.each(["failed_pairs", "timeout"])("ranks only eligible errors: %s", async kind => {
    const doc = compareDoc(); doc.failed = 1; doc.pairs[1].report = null;
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "error", error_kind: kind, results: doc }));
      if (url === API + "/suite/rank") return jsonResponse(409, { detail: "too many suite jobs are running" });
      return unexpectedFetch(url);
    });
    render(<ComparePanel {...props()} />); start(); await tick(10);
    expect(callsTo(fetch, "/suite/rank")).toHaveLength(kind === "failed_pairs" ? 1 : 0);
    expect(screen.getByText(kind === "failed_pairs" ? "Some comparison pairs failed; the pairs that finished are used." : "VideoSuite took too long and was stopped.")).toBeVisible();
    if (kind === "failed_pairs") expect(screen.getByText("Failed pair: clip_a_crf18.mkv vs clip_c_crf26.mp4")).toBeVisible();
  });
  it("shows a 409 start error without polling", async () => {
    const fetch = installFetch(url => url === API + "/suite/compare" ? jsonResponse(409, { detail: "a comparison is already running" }) : unexpectedFetch(url));
    render(<ComparePanel {...props()} />); start(); await tick(); expect(screen.getByRole("alert")).toHaveTextContent("Busy: a comparison is already running"); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("guards pending compare start and ignores completion after unmount", async () => {
    const pending = deferred<Response>(); const fetch = installFetch(url => url === API + "/suite/compare" ? pending.promise : unexpectedFetch(url));
    const { unmount } = render(<ComparePanel {...props()} />); start(); fireEvent.click(screen.getByRole("button"));
    expect(fetch).toHaveBeenCalledTimes(1); expect(screen.getByText("Starting the comparison…")).toBeDisabled(); unmount();
    await act(async () => { pending.resolve(jsonResponse(200, { job_id: "c-old" })); }); await tick(10000); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("keeps the handoff busy and ignores obsolete rank start on unmount", async () => {
    const pending = deferred<Response>(); const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "done", results: compareDoc() }));
      if (url === API + "/suite/rank") return pending.promise;
      return unexpectedFetch(url);
    });
    const { unmount } = render(<ComparePanel {...props()} />); start(); await tick(10);
    expect(screen.getByText("Starting the ranking…")).toBeVisible(); expect(screen.getByText("Compare and rank")).toBeDisabled();
    fireEvent.click(screen.getByText("Compare and rank")); expect(callsTo(fetch, "/suite/compare")).toHaveLength(1);
    unmount(); await act(async () => { pending.resolve(jsonResponse(200, { job_id: "r-old" })); }); await tick(10000); expect(callsTo(fetch, "/job/r-old")).toHaveLength(0);
  });
  it("retries ranking with the immutable original snapshot and guards duplicate retries", async () => {
    const pending = deferred<Response>(); let ranks = 0;
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "done", results: compareDoc() }));
      if (url === API + "/suite/rank") return ++ranks === 1 ? jsonResponse(409, { detail: "too many suite jobs are running" }) : pending.promise;
      if (url === API + "/job/r1") return jsonResponse(200, jobOf({ status: "done", results: rankDoc() }));
      return unexpectedFetch(url);
    });
    const p = props(); const { rerender } = render(<ComparePanel {...p} />); start();
    rerender(<ComparePanel {...p} paths={["D:\\other.mkv", "D:\\more.mkv"]} options={{ lang: "pt-BR" }} />);
    await tick(10); expect(screen.getByRole("alert")).toHaveTextContent("Busy: too many suite jobs are running");
    const retry = screen.getByText("Retry ranking"); act(() => { fireEvent.click(retry); fireEvent.click(retry); }); await tick(10);
    expect(callsTo(fetch, "/suite/rank")).toHaveLength(2);
    expect(bodyOf(fetch, "/suite/rank", 1)).toEqual({ paths, reports_dir: out, lang: "eng" });
    await act(async () => { pending.resolve(jsonResponse(200, { job_id: "r1" })); }); await tick(10);
    expect(screen.getByText("Picture quality (device-neutral)")).toBeVisible();
    const compare = screen.getByRole("button", { name: "Compare and rank" });
    expect(compare).not.toBeDisabled();
    fireEvent.click(compare); await tick(10);
    expect(callsTo(fetch, "/suite/compare")).toHaveLength(2);
    expect(bodyOf(fetch, "/suite/compare", 1)).toMatchObject({ paths: ["D:\\other.mkv", "D:\\more.mkv"] });
  });
  it("cancels and reruns the same snapshot without ranking", async () => {
    let polls = 0; const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: ++polls === 1 ? "running" : "cancelled" }));
      if (url === API + "/job/c1/cancel") return jsonResponse(200, {});
      return unexpectedFetch(url);
    });
    render(<ComparePanel {...props()} />); start(); await tick(1); fireEvent.click(screen.getByText("Cancel comparison")); await tick();
    const cancel = screen.getByText("Cancel comparison");
    expect(cancel).toBeDisabled();
    fireEvent.click(cancel); await tick();
    expect(callsTo(fetch, "/job/c1/cancel")).toHaveLength(1);
    expect(screen.getByText(CANCELLING_TEXT)).toBeVisible(); await tick(1500); expect(screen.getByText("Cancelled")).toBeVisible();
    fireEvent.click(screen.getByText("Run the comparison again")); await tick(1);
    expect(callsTo(fetch, "/suite/compare")).toHaveLength(2); expect(bodyOf(fetch, "/suite/compare", 1)).toEqual(bodyOf(fetch, "/suite/compare"));
  });
  it.each([false, true])("caps the log and suppresses stale progress on saturation %s", async saturated => {
    const events = (saturated ? [...lines, ...Array(1994).fill(lines[2]), SATURATION_NOTE] : Array.from({ length: 120 }, (_, i) => `line ${i + 1}`)).map(msg => ({ msg, ts: 0 }));
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ events, current: "live current" }));
      return unexpectedFetch(url);
    });
    const { container } = render(<ComparePanel {...props()} />); start(); await tick(1);
    expect(screen.getByText(LOG_SUMMARY_TEXT)).toBeVisible();
    const logDetails = container.querySelector("details.suite-log");
    expect(logDetails).not.toBeNull();
    expect(logDetails).not.toHaveAttribute("open");
    if (saturated) { expect(screen.getByText(SATURATED_TEXT)).toBeVisible(); expect(screen.queryByRole("progressbar")).toBeNull(); expect(screen.queryByText(/Pair \d+ of \d+:/)).toBeNull(); expect(screen.getByText("live current")).toBeVisible(); }
    else { const log = container.querySelector("pre")!.textContent!; expect(log).toContain("line 71\n"); expect(log).not.toContain("line 70\n"); expect(log).toContain("line 120"); }
    expect(callsTo(fetch, "/job/c1")).toHaveLength(1);
  });
  it("updates the shortlist preview", () => {
    render(<ComparePanel {...props()} paths={Array.from({ length: 10 }, (_, i) => `D:\\${i}.mkv`)} />);
    fireEvent.change(screen.getByLabelText(/Files kept/), { target: { value: "2" } });
    expect(screen.getByText("10 file(s) chosen; up to 4 are compared (at most 6 pair(s)).")).toBeVisible();
  });
  it("clears old rank output on a new run and renders log text literally", async () => {
    let starts = 0;
    const fetch = installFetch(url => {
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: `c${++starts}` });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "done", results: compareDoc() }));
      if (url === API + "/suite/rank") return jsonResponse(200, { job_id: "r1" });
      if (url === API + "/job/r1") return jsonResponse(200, jobOf({ status: "done", results: rankDoc() }));
      if (url === API + "/job/c2") return jsonResponse(200, jobOf({ events: [{ msg: "<img src=x onerror=alert(1)>", ts: 0 }] }));
      return unexpectedFetch(url);
    });
    const { container } = render(<ComparePanel {...props()} />); start(); await tick(10);
    expect(screen.getByText("Picture quality (device-neutral)")).toBeVisible();
    start(); expect(screen.queryByText("Picture quality (device-neutral)")).toBeNull(); await tick(10);
    expect(container.querySelector("pre")).toHaveTextContent("<img src=x onerror=alert(1)>"); expect(container.querySelector("img")).toBeNull();
    await tick(3000); expect(callsTo(fetch, "/job/c1")).toHaveLength(1);
  });
});
