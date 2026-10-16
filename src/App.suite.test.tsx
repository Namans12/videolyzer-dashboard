import { act } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { API } from "./suite/config";
import { callsTo, installFetch, jsonResponse, unexpectedFetch } from "./suite/testing/fetchMock";
import type { FetchHandler } from "./suite/testing/fetchMock";
import { devicesDoc, jobOf, legacyVideo, scoreDoc, scorePartialDoc, suiteSettings, versionInfo } from "./suite/testing/fixtures";
import { PARTIAL_TEXT, unverifiedNotice } from "./suite/verdict";
import appSource from "./App.tsx?raw";

const suiteCrash = vi.hoisted(() => ({ enabled: false }));
vi.mock("./suite/SuiteScreen", async importOriginal => {
  const original = await importOriginal<typeof import("./suite/SuiteScreen")>();
  return { SuiteScreen: function SuiteScreen() {
    if (suiteCrash.enabled) throw new Error("private render error");
    return <original.SuiteScreen />;
  } };
});
afterEach(() => { suiteCrash.enabled = false; vi.restoreAllMocks(); });

const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(0); }); };
function setup(extra: FetchHandler = unexpectedFetch) {
  return installFetch((url, init) => {
    if (url === API + "/suite/version") return jsonResponse(200, versionInfo());
    if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
    if (url === API + "/suite/settings") return jsonResponse(200, suiteSettings());
    return extra(url, init);
  });
}
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
describe("Suite tab integration", () => {
  it("selects and focuses tabs with arrows, Home and End using a roving tab stop", async () => {
    setup(); render(<App />);
    const analyze = screen.getByRole("tab", { name: "Analyze" });
    const suite = screen.getByRole("tab", { name: "Suite (all devices)" });
    expect(analyze).toHaveAttribute("tabindex", "0");
    expect(suite).toHaveAttribute("tabindex", "-1");
    analyze.focus();
    for (const [key, target] of [["ArrowRight", suite], ["ArrowRight", analyze], ["ArrowLeft", suite], ["ArrowLeft", analyze], ["End", suite], ["Home", analyze]] as const) {
      fireEvent.keyDown(document.activeElement!, { key });
      expect(target).toHaveFocus();
      expect(target).toHaveAttribute("aria-selected", "true");
      expect(target).toHaveAttribute("tabindex", "0");
      expect(target === suite ? analyze : suite).toHaveAttribute("tabindex", "-1");
    }
    await tick();
  });
  it("keeps Analyze usable when the Suite screen throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    suiteCrash.enabled = true;
    render(<App />);
    fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" }));
    expect(screen.getByText("The Suite screen hit an unexpected error. Use the Analyze tab, or reload the page.")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" }));
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\still-usable.mkv" } });
    expect(document.getElementById("video-path")).toBeVisible();
    expect(document.getElementById("video-path")).toHaveValue("D:\\still-usable.mkv");
  });
  it("starts Analyze without loading suite and preserves both panels on switching", async () => {
    const fetch = setup(); render(<App />); expect(screen.getByRole("tab", { name: "Analyze" })).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("panel-suite")).toBeNull(); await tick(1000); expect(fetch).not.toHaveBeenCalled();
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\Media\\x.mkv" } });
    fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" })); await tick();
    expect(screen.getByText("VideoSuite 0.4.0 is ready.")).toBeVisible(); expect(document.getElementById("panel-analyze")).toHaveAttribute("hidden");
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" })); expect(document.getElementById("video-path")).toHaveValue("D:\\Media\\x.mkv");
    expect(document.getElementById("panel-suite")).toHaveAttribute("hidden"); expect(localStorage.getItem("active_tab")).toBe("analyze");
  });
  it.each(["suite", "xyz"])("reads stored tab %s", async value => {
    localStorage.setItem("active_tab", value); setup(); render(<App />); await tick();
    expect(screen.getByRole("tab", { name: value === "suite" ? "Suite (all devices)" : "Analyze" })).toHaveAttribute("aria-selected", "true");
  });
  it.each(["ok", "error", "absent"])("keeps legacy results with magnet suite %s", status => {
    const record = scorePartialDoc().records[0]; const suite = status === "ok" ? { status, tool_version: "0.4.0", partial: true, record, best_player: record.best_player } : { status: "error", message: "VideoSuite scoring failed", error_kind: "internal" };
    localStorage.setItem("last_results", JSON.stringify([{ ...legacyVideo("Movie.2160p.mkv"), ...(status === "absent" ? {} : { suite }) }]));
    const { container } = render(<App />); expect(screen.getByText("Analysis Result")).toBeVisible(); expect(screen.getAllByText(/TV Score/i).length).toBeGreaterThan(0);
    if (status === "ok") expect(screen.getByText(PARTIAL_TEXT)).toBeVisible();
    if (status === "error") expect(screen.getByText("VideoSuite could not score this file: VideoSuite scoring failed")).toBeVisible();
    if (status === "absent") expect(screen.queryByText("VideoSuite")).toBeNull();
    expect(screen.getAllByText("legacy (Bravia only)").length).toBeGreaterThanOrEqual(3);
    expect(container.querySelectorAll("details.legacy-panel")).toHaveLength(2);
    for (const el of container.querySelectorAll("details.legacy-panel")) expect(el).not.toHaveAttribute("open");
  });
  it("uses the shared loopback API constant", () => { expect(API).toBe("http://127.0.0.1:8000"); expect(appSource).not.toContain("const API ="); });
  it("tolerates throwing storage for tabs, theme and completed analysis", async () => {
    const fetch = setup(url => {
      if (url.startsWith(API + "/analyze-path/?")) return jsonResponse(200, { job_id: "a1" });
      if (url === API + "/job/a1") return jsonResponse(200, { status: "done", results: [legacyVideo()] });
      return unexpectedFetch(url);
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    const { container } = render(<App />); fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" })); await tick();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" })); fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\Media\\clip.mkv" } }); fireEvent.click(screen.getByRole("button", { name: "Analyze Path" })); await tick();
    expect(screen.getByText("Analysis Result")).toBeVisible(); expect(callsTo(fetch, "/job/a1")).toHaveLength(1);
  });
  it("keeps a running suite job alive while its panel is hidden", async () => {
    let polls = 0; const fetch = setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: "s1" });
      if (url === API + "/job/s1") return jsonResponse(200, ++polls < 3 ? jobOf() : jobOf({ status: "done", results: scoreDoc() }));
      return unexpectedFetch(url);
    });
    render(<App />); fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" })); await tick();
    fireEvent.change(screen.getByLabelText(/Video files on/), { target: { value: "D:\\a.mkv" } }); fireEvent.click(screen.getByText("Score files")); await tick();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" })); await tick(3100); fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" }));
    expect(screen.getByRole("heading", { name: "clip_a_crf18.mkv" })).toBeVisible(); await tick(10000); expect(callsTo(fetch, "/job/s1")).toHaveLength(3);
  });
  it("clears the legacy analyzing banner on terminal failures", async () => {
    let polls = 0;
    setup(url => {
      if (url.startsWith(API + "/analyze-path/?")) return jsonResponse(200, { job_id: "a1" });
      if (url === API + "/job/a1") return ++polls === 1 ? jsonResponse(200, { status: "running", current: "working", progress: "1/2" }) : jsonResponse(404, {});
      return unexpectedFetch(url);
    });
    const { container } = render(<App />); fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\a.mkv" } }); fireEvent.click(screen.getByRole("button", { name: "Analyze Path" })); await tick();
    expect(screen.getByText(/⏳ Analyzing:/)).toBeVisible(); await tick(600);
    expect(container.querySelector(".job-progress")).toBeNull();
    expect(screen.queryByText(/⏳ Analyzing:/)).toBeNull();
  });
  it("shows each expanded episode release's own suite block", async () => {
    const partial = scorePartialDoc().records[0];
    const comparison = (key: string) => ({
      releases: [0, 1].map(i => ({ name: `${key}.release${i}`, magnet_uri: null, analysis: { ...legacyVideo(`${key}.${i}.mkv`), ...(key === "S01E02" ? { suite: i === 0 ? { status: "error", error_kind: "internal", message: "Episode two only" } : { status: "ok", partial: true, record: partial, best_player: partial.best_player } } : {}) }, intel: {}, verification: { flags: [], summary: "ok", trust_score: 50 }, badges: [] })),
      comparison_matrix: { columns: [0, 1].map(i => ({ name: `${key}.${i}`, magnet_uri: null, composite_score: 50, trust_score: 50, tv_score: 30, badges: [], verification_summary: "ok", error_count: 0, warn_count: 0 })), rows: [{ label: "Episode metadata", values: ["A", "B"], highlight: null }] },
      winner: { winner_index: 0, winner_name: "a", reasons: [] },
    });
    const episodes = ["S01E01", "S01E02"].map(key => ({ key, season: 1, episode: 1, present_in: [0, 1], missing: [], contested: true, winner_torrent_index: 0, comparison: comparison(key) }));
    setup(url => {
      if (url.startsWith(API + "/compare-magnets/?")) return jsonResponse(200, { job_id: "c1", total: 2 });
      if (url === API + "/job/c1") return jsonResponse(200, { status: "done", per_magnet: [], dropped: [], mode: "series", comparison: comparison("S01E01"), episode_comparison: { torrents: [0, 1].map(index => ({ index, name: `release${index}`, magnet_uri: null, episode_count: 2, win_count: 1, avg_score: 50 })), episodes, summary: { winner_index: 0, winner_name: "a", reasons: [], total_episodes: 2, contested_count: 2 } } });
      return unexpectedFetch(url);
    });
    const { container } = render(<App />); fireEvent.change(document.getElementById("magnet-chip-input")!, { target: { value: "magnet:?xt=urn:btih:a magnet:?xt=urn:btih:b" } }); fireEvent.click(screen.getByRole("button", { name: "Compare 2 Magnets" })); await tick();
    expect(screen.queryByText("VideoSuite could not score this file: Episode two only")).toBeNull(); fireEvent.click(screen.getByText("S01E02"));
    expect(screen.getByText("VideoSuite could not score this file: Episode two only")).toBeVisible(); expect(screen.getByText(unverifiedNotice("inferred")!)).toBeVisible();
    expect(within(container.querySelector("#series-compare") as HTMLElement).getByText("Episode metadata")).toBeVisible();
    expect(container.querySelectorAll(".episode-suite")).toHaveLength(2);
  });
});
