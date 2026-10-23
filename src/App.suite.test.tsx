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

vi.mock("./app/AppControls", () => ({ AppConnection: () => <div aria-label="App connection" /> }));

const suiteCrash = vi.hoisted(() => ({ enabled: false }));
vi.mock("./suite/SuiteScreen", async importOriginal => {
  const original = await importOriginal<typeof import("./suite/SuiteScreen")>();
  return { SuiteScreen: function SuiteScreen() {
    if (suiteCrash.enabled) throw new Error("private render error");
    return <original.SuiteScreen />;
  } };
});
const calibrationCrash = vi.hoisted(() => ({ enabled: false }));
vi.mock("./suite/calibration/CalibrationScreen", async importOriginal => {
  const original = await importOriginal<typeof import("./suite/calibration/CalibrationScreen")>();
  return { CalibrationScreen: function CalibrationScreen() {
    if (calibrationCrash.enabled) throw new Error("private calibration error");
    return <original.CalibrationScreen />;
  } };
});
afterEach(() => { suiteCrash.enabled = false; calibrationCrash.enabled = false; vi.restoreAllMocks(); });

const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(0); }); };
function setup(extra: FetchHandler = unexpectedFetch) {
  return installFetch((url, init) => {
    if (url === API + "/suite/version") return jsonResponse(200, versionInfo());
    if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
    if (url === API + "/suite/settings") return jsonResponse(200, suiteSettings());
    if (url === API + "/comparison/runs" && init?.method !== "POST") return jsonResponse(200, { runs: [] });
    return extra(url, init);
  });
}
/** The owner's old stored tab "analyze" now opens Advanced > Analyze (Compare is not mounted). */
const legacyAnalyze = () => localStorage.setItem("active_tab", "analyze");
const tab = (name: string) => screen.getByRole("tab", { name });
const openSuite = () => { fireEvent.click(tab("Advanced")); fireEvent.click(tab("Suite (all devices)")); };
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
describe("Suite tab integration", () => {
  it("makes Compare home and preserves legacy tools under Advanced with keyboard tabs", async () => {
    setup(); render(<App />);
    expect(screen.getByLabelText("App connection")).toBeVisible();
    const screens = screen.getByRole("tablist", { name: "Screens" });
    const main = within(screens).getAllByRole("tab");
    expect(main.map(t => t.textContent)).toEqual(["Compare", "Calibration (TV)", "Devices info", "Advanced"]);
    const [compare, calibration, devices, advanced] = main;
    expect(compare).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("panel-compare")).not.toHaveAttribute("hidden");
    await tick();
    expect(within(document.getElementById("panel-compare")!).getByRole("button", { name: "Check files and estimate" })).toBeInTheDocument();
    compare.focus();
    const steps = [["ArrowRight", calibration], ["ArrowRight", devices], ["ArrowRight", advanced], ["ArrowRight", compare],
      ["ArrowLeft", advanced], ["Home", compare], ["End", advanced], ["ArrowLeft", devices], ["End", advanced]] as const;
    for (const [key, target] of steps) {
      fireEvent.keyDown(document.activeElement!, { key });
      expect(target).toHaveFocus();
      expect(target).toHaveAttribute("aria-selected", "true");
      for (const t of main) expect(t).toHaveAttribute("tabindex", t === target ? "0" : "-1");
    }
    await tick();
    // Advanced holds the old Analyze and Suite screens as its own keyboard tabs
    const tools = within(document.getElementById("panel-advanced")!).getByRole("tablist", { name: "Advanced tools" });
    const [analyze, suite] = within(tools).getAllByRole("tab");
    expect([analyze.textContent, suite.textContent]).toEqual(["Analyze", "Suite (all devices)"]);
    expect(analyze).toHaveAttribute("aria-selected", "true");
    analyze.focus();
    fireEvent.keyDown(analyze, { key: "ArrowRight" });
    expect(suite).toHaveFocus(); expect(suite).toHaveAttribute("aria-selected", "true"); expect(analyze).toHaveAttribute("tabindex", "-1");
    fireEvent.keyDown(suite, { key: "Home" });
    expect(analyze).toHaveFocus();
    fireEvent.keyDown(analyze, { key: "End" });
    expect(suite).toHaveFocus();
    await tick();
    expect(localStorage.getItem("active_tab")).toBe("advanced");
    expect(localStorage.getItem("advanced_view")).toBe("suite");
    // panels mount on first use and stay mounted while hidden
    const comparePanel = document.getElementById("panel-compare");
    fireEvent.click(compare);
    expect(document.getElementById("panel-compare")).toBe(comparePanel);
    expect(document.getElementById("panel-advanced")).toHaveAttribute("hidden");
    expect(document.getElementById("panel-suite")).not.toBeNull();
  });
  it.each([["analyze", "Analyze"], ["suite", "Suite (all devices)"]])("migrates the legacy stored tab %s into Advanced", async (stored, view) => {
    localStorage.setItem("active_tab", stored); setup(); render(<App />); await tick();
    expect(tab("Advanced")).toHaveAttribute("aria-selected", "true");
    expect(tab(view)).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("panel-compare")).toBeNull();
  });
  it("shows Devices info read-only from the real catalogue on first open", async () => {
    const fetch = setup(); legacyAnalyze(); render(<App />);
    expect(document.getElementById("panel-devices")).toBeNull();
    fireEvent.click(tab("Devices info")); await tick();
    const panel = document.getElementById("panel-devices")!;
    expect(within(panel).getAllByRole("article").map(a => a.getAttribute("data-device")))
      .toEqual(["sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra"]);
    expect(within(panel).queryAllByRole("button")).toHaveLength(0);
    fireEvent.click(tab("Advanced")); fireEvent.click(tab("Devices info")); await tick();
    expect(document.getElementById("panel-devices")).toBe(panel);
    expect(callsTo(fetch, "/suite/devices")).toHaveLength(2);           // re-read on each visit; the panel stays mounted
  });
  it("mounts the Calibration screen on first open, keeps it mounted while hidden, and gates it on the calibration capability", async () => {
    const fetch = setup(); legacyAnalyze(); render(<App />);
    expect(document.getElementById("panel-calibration")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Calibration (TV)" })); await tick();
    const panel = document.getElementById("panel-calibration")!;
    expect(panel).toHaveAttribute("role", "tabpanel"); expect(panel).toHaveAttribute("aria-labelledby", "tab-calibration");
    expect(within(panel).getByText("Please update VideoSuite to 0.5.0 to use calibration.")).toBeVisible();
    expect(fetch.mock.calls.some(c => String(c[0]).includes("/suite/calibration/"))).toBe(false);
    expect(document.getElementById("panel-suite")).toBeNull();
    openSuite(); await tick();
    expect(panel).toHaveAttribute("hidden"); expect(document.getElementById("panel-calibration")).toBe(panel);
    const suitePanel = document.getElementById("panel-suite")!;
    expect(within(suitePanel).getByText("VideoSuite 0.4.0 is ready.")).toBeVisible();
    expect(within(suitePanel).queryByText(/update VideoSuite to 0.5.0/)).toBeNull();
    expect(within(suitePanel).getByText("Score files")).toBeInTheDocument();
  });
  it("opens the Calibration screen for a capable suite and restores it from the stored tab", async () => {
    localStorage.setItem("active_tab", "calibration");
    const status = { schema: "videosuite-calibrate-status/1", tool_version: "0.5.0", recovery_required: false, kit: null, session: null, answers: [],
      summary: { done: 0, total: 0, matches: 0, differs: 0 } };
    installFetch(url => {
      if (url === API + "/suite/version") return jsonResponse(200, versionInfo({ tool_version: "0.5.0", capabilities: [...versionInfo().capabilities, "calibration"] }));
      if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
      if (url === API + "/suite/settings") return jsonResponse(200, suiteSettings());
      if (url === API + "/suite/calibration/status") return jsonResponse(200, status);
      return unexpectedFetch(url);
    });
    render(<App />); await tick();
    expect(screen.getByRole("tab", { name: "Calibration (TV)" })).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("panel-suite")).toBeNull();
    const panel = document.getElementById("panel-calibration")!;
    expect(panel).not.toHaveAttribute("hidden");
    expect(within(panel).getByRole("region", { name: "Prepare calibration kit" })).toBeVisible();
    expect(within(panel).queryByText(/update VideoSuite to 0.5.0/)).toBeNull();
  });
  it("keeps Analyze and Suite usable when the Calibration screen throws", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    calibrationCrash.enabled = true; setup();
    render(<App />); await tick();
    fireEvent.click(screen.getByRole("tab", { name: "Calibration (TV)" }));
    expect(screen.getByText("The Calibration screen hit an unexpected error. Use Advanced > Analyze, or reload the page.")).toBeVisible();
    expect(document.body.textContent).not.toContain("private calibration error");
    openSuite(); await tick();
    expect(screen.getByText("VideoSuite 0.4.0 is ready.")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" }));
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\still-usable.mkv" } });
    expect(document.getElementById("video-path")).toHaveValue("D:\\still-usable.mkv");
  });
  it("keeps Analyze usable when the Suite screen throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    suiteCrash.enabled = true; legacyAnalyze();
    render(<App />);
    fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" }));
    expect(screen.getByText("The Suite screen hit an unexpected error. Use Advanced > Analyze, or reload the page.")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" }));
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\still-usable.mkv" } });
    expect(document.getElementById("video-path")).toBeVisible();
    expect(document.getElementById("video-path")).toHaveValue("D:\\still-usable.mkv");
  });
  it("starts Analyze without loading suite and preserves both panels on switching", async () => {
    legacyAnalyze(); const fetch = setup(); render(<App />); expect(screen.getByRole("tab", { name: "Analyze" })).toHaveAttribute("aria-selected", "true");
    expect(document.getElementById("panel-suite")).toBeNull(); await tick(1000); expect(fetch).not.toHaveBeenCalled();
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\Media\\x.mkv" } });
    fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" })); await tick();
    expect(screen.getByText("VideoSuite 0.4.0 is ready.")).toBeVisible(); expect(document.getElementById("panel-analyze")).toHaveAttribute("hidden");
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" })); expect(document.getElementById("video-path")).toHaveValue("D:\\Media\\x.mkv");
    expect(document.getElementById("panel-suite")).toHaveAttribute("hidden");
    expect(localStorage.getItem("active_tab")).toBe("advanced"); expect(localStorage.getItem("advanced_view")).toBe("analyze");
  });
  it.each(["compare", "xyz", "devices", "calibration"])("reads stored tab %s", async value => {
    localStorage.setItem("active_tab", value); setup(); render(<App />); await tick();
    const names: Record<string, string> = { compare: "Compare", xyz: "Compare", devices: "Devices info", calibration: "Calibration (TV)" };
    expect(screen.getByRole("tab", { name: names[value] })).toHaveAttribute("aria-selected", "true");
  });
  it.each(["ok", "error", "absent"])("keeps legacy results with magnet suite %s", status => {
    const record = scorePartialDoc().records[0]; const suite = status === "ok" ? { status, tool_version: "0.4.0", partial: true, record, best_player: record.best_player } : { status: "error", message: "VideoSuite scoring failed", error_kind: "internal" };
    legacyAnalyze(); localStorage.setItem("last_results", JSON.stringify([{ ...legacyVideo("Movie.2160p.mkv"), ...(status === "absent" ? {} : { suite }) }]));
    const { container } = render(<App />); expect(screen.getByText("Analysis Result")).toBeVisible(); expect(screen.getAllByText(/TV Score/i).length).toBeGreaterThan(0);
    if (status === "ok") expect(screen.getByText(PARTIAL_TEXT)).toBeVisible();
    if (status === "error") expect(screen.getByText("VideoSuite could not score this file: VideoSuite scoring failed")).toBeVisible();
    if (status === "absent") expect(screen.queryByText("VideoSuite")).toBeNull();
    expect(screen.getAllByText("legacy (Bravia only)").length).toBeGreaterThanOrEqual(3);
    expect(container.querySelectorAll("details.legacy-panel")).toHaveLength(2);
    for (const el of container.querySelectorAll("details.legacy-panel")) expect(el).not.toHaveAttribute("open");
  });
  it("uses the shared relative API constant", () => { expect(API).toBe(""); expect(appSource).not.toContain("const API ="); });
  it("renders local media paths as text", () => {
    expect(appSource).not.toContain("file:///");
    expect(appSource).toContain('{item.path}');
  });
  it("tolerates throwing storage for tabs, theme and completed analysis", async () => {
    const fetch = setup(url => {
      if (url.startsWith(API + "/analyze-path/?")) return jsonResponse(200, { job_id: "a1" });
      if (url === API + "/job/a1") return jsonResponse(200, { status: "done", results: [legacyVideo()] });
      return unexpectedFetch(url);
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("full", "QuotaExceededError"); });
    const { container } = render(<App />); await tick(); openSuite(); await tick();
    fireEvent.click(screen.getByRole("tab", { name: "Analyze" })); fireEvent.click(container.querySelector('#panel-analyze input[type="checkbox"]')!);
    fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\Media\\clip.mkv" } }); fireEvent.click(screen.getByRole("button", { name: "Analyze Path" })); await tick();
    expect(screen.getByText("Analysis Result")).toBeVisible(); expect(callsTo(fetch, "/job/a1")).toHaveLength(1);
  });
  it("keeps a running suite job alive while its panel is hidden", async () => {
    let polls = 0; const fetch = setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: "s1" });
      if (url === API + "/job/s1") return jsonResponse(200, ++polls < 3 ? jobOf() : jobOf({ status: "done", results: scoreDoc() }));
      return unexpectedFetch(url);
    });
    legacyAnalyze(); render(<App />); fireEvent.click(screen.getByRole("tab", { name: "Suite (all devices)" })); await tick();
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
    legacyAnalyze(); const { container } = render(<App />); fireEvent.change(document.getElementById("video-path")!, { target: { value: "D:\\a.mkv" } }); fireEvent.click(screen.getByRole("button", { name: "Analyze Path" })); await tick();
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
    legacyAnalyze(); const { container } = render(<App />); fireEvent.change(document.getElementById("magnet-chip-input")!, { target: { value: "magnet:?xt=urn:btih:a magnet:?xt=urn:btih:b" } }); fireEvent.click(screen.getByRole("button", { name: "Compare 2 Magnets" })); await tick();
    expect(screen.queryByText("VideoSuite could not score this file: Episode two only")).toBeNull(); fireEvent.click(screen.getByText("S01E02"));
    expect(screen.getByText("VideoSuite could not score this file: Episode two only")).toBeVisible(); expect(screen.getByText(unverifiedNotice("inferred")!)).toBeVisible();
    expect(within(container.querySelector("#series-compare") as HTMLElement).getByText("Episode metadata")).toBeVisible();
    expect(container.querySelectorAll(".episode-suite")).toHaveLength(2);
  });
});
