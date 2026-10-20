import { act, StrictMode } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SuiteScreen } from "./SuiteScreen";
import { chooserBlockReason, isScoreDocument } from "./screen";
import { API } from "./config";
import { bodyOf, callsTo, installFetch, jsonResponse, unexpectedFetch } from "./testing/fetchMock";
import type { FetchHandler } from "./testing/fetchMock";
import { compareDoc, devicesDoc, jobOf, rankDoc, scoreDoc, suiteSettings, versionInfo } from "./testing/fixtures";
import { SUITE_DISABLED_TEXT } from "./messages";
import { JOB_GONE_TEXT } from "../jobStatus";

// Browser app compilation must reject accidental Node globals.
// @ts-expect-error Node process must not be typed in the app program.
export type BrowserProcessMustBeAbsent = typeof process;
// @ts-expect-error Node Buffer must not be typed in the app program.
export type BrowserBufferMustBeAbsent = typeof Buffer;

const tick = async (ms = 0) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  for (let i = 0; i < 5; i++) await act(async () => { await vi.advanceTimersByTimeAsync(0); });
};
const enter = (text = "D:\\a.mkv\nD:\\b.mkv") => fireEvent.change(screen.getByLabelText(/Video files on/), { target: { value: text } });
function setup(extra: FetchHandler = unexpectedFetch) {
  return installFetch((url, init) => {
    if (url === API + "/suite/version" || url === API + "/suite/version?refresh=true") return jsonResponse(200, versionInfo());
    if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
    if (url === API + "/suite/settings") return jsonResponse(200, suiteSettings());
    return extra(url, init);
  });
}
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
describe("Suite screen", () => {
  it("clears device and settings snapshots after a not-ok recheck before later requests", async () => {
    let refreshes = 0;
    let deviceReads = 0;
    let settingsReads = 0;
    const pendingDevices = deferred<Response>();
    const pendingSettings = deferred<Response>();
    const settings = suiteSettings();
    settings.devices.sony_bravia_8_ii.usb_filesystem = "exfat";
    const fetch = installFetch(url => {
      if (url === API + "/suite/version") return jsonResponse(200, versionInfo());
      if (url === API + "/suite/version?refresh=true") return jsonResponse(200, versionInfo({ status: ++refreshes === 1 ? "unavailable" : "ok" }));
      if (url === API + "/suite/devices") return ++deviceReads === 1 ? jsonResponse(200, devicesDoc()) : pendingDevices.promise;
      if (url === API + "/suite/settings") return ++settingsReads === 1 ? jsonResponse(200, settings) : pendingSettings.promise;
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: "s1" });
      if (url === API + "/job/s1") return jsonResponse(200, jobOf());
      return unexpectedFetch(url);
    });
    render(<SuiteScreen />); await tick(); enter();
    expect(within(screen.getByLabelText("Device")).getAllByRole("option")).toHaveLength(devicesDoc().devices.length + 1);
    expect(screen.getByText(/^From your settings:/)).toBeVisible();
    fireEvent.click(screen.getByText("Check again")); await tick();
    expect(within(screen.getByLabelText("Device")).getAllByRole("option")).toHaveLength(1);
    expect(screen.queryByText(/^From your settings:/)).toBeNull();
    expect(screen.queryByRole("region", { name: "Owner settings" })).toBeNull();
    fireEvent.click(screen.getByText("Score files"));
    expect(callsTo(fetch, "/suite/score")).toHaveLength(0);
    fireEvent.click(screen.getByText("Check again")); await tick();
    expect(within(screen.getByLabelText("Device")).getAllByRole("option")).toHaveLength(1);
    expect(screen.queryByText(/^From your settings:/)).toBeNull();
    fireEvent.click(screen.getByText("Score files")); await tick();
    expect(bodyOf(fetch, "/suite/score")).toEqual({ paths: ["D:\\a.mkv", "D:\\b.mkv"], lang: "eng" });
    await act(async () => {
      pendingDevices.resolve(jsonResponse(200, { ...devicesDoc(), devices: [] }));
      pendingSettings.resolve(jsonResponse(200, suiteSettings()));
    });
    await tick();
    expect(screen.queryByText(/^From your settings:/)).toBeNull();
  });
  it.each([false, true])("loads once and shows legend/settings (StrictMode=%s)", async strict => {
    const fetch = setup(); render(<SuiteScreen />, strict ? { wrapper: StrictMode } : {});
    expect(screen.getByText("Checking VideoSuite…")).toBeVisible(); expect(fetch).not.toHaveBeenCalled(); await tick();
    expect(screen.getByText("VideoSuite 0.4.0 is ready.")).toBeVisible();
    for (const path of ["/suite/version", "/suite/devices", "/suite/settings"]) expect(callsTo(fetch, path)).toHaveLength(1);
    expect(screen.getByRole("region", { name: "Evidence legend" })).toBeVisible(); expect(screen.getByText("Settings at the last settings check:")).toBeVisible();
  });
  it("does not load devices or settings for an old suite", async () => {
    const fetch = installFetch(url => url === API + "/suite/version" ? jsonResponse(200, versionInfo({ status: "too_old", message: "Reinstall the Comparizer." })) : unexpectedFetch(url));
    render(<SuiteScreen />); await tick(); expect(screen.getByText("Reinstall the Comparizer.")).toBeVisible();
    expect(screen.getAllByText(SUITE_DISABLED_TEXT).length).toBeGreaterThan(0); expect(screen.getByLabelText(/Video files on/)).toBeDisabled();
    expect(screen.getByText("Score files")).toBeDisabled(); expect(screen.getByRole("button", { name: "Compare and rank" })).toBeDisabled();
    expect(screen.queryByText("Loading settings…")).toBeNull(); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("guards pending initial/recheck requests and aborts on unmount", async () => {
    const initial = deferred<Response>(); const refresh = deferred<Response>(); let signal: AbortSignal | null | undefined;
    const fetch = installFetch((url, init) => {
      if (url === API + "/suite/version") return initial.promise;
      if (url === API + "/suite/version?refresh=true") { signal = init?.signal; return refresh.promise; }
      if (url === API + "/suite/devices") return jsonResponse(200, devicesDoc());
      if (url === API + "/suite/settings") return jsonResponse(200, suiteSettings());
      return unexpectedFetch(url);
    });
    const { unmount } = render(<SuiteScreen />); await tick(); fireEvent.click(screen.getByText("Check again")); expect(callsTo(fetch, "/suite/version?refresh=true")).toHaveLength(0);
    await act(async () => { initial.resolve(jsonResponse(200, versionInfo())); }); await tick();
    const button = screen.getByText("Check again"); act(() => { fireEvent.click(button); fireEvent.click(button); });
    expect(callsTo(fetch, "/suite/version?refresh=true")).toHaveLength(1); unmount(); expect(signal?.aborted).toBe(true);
    await act(async () => { refresh.resolve(jsonResponse(200, versionInfo())); }); await tick();
    expect(callsTo(fetch, "/suite/devices")).toHaveLength(1);
  });
  it("rechecks after a network failure", async () => {
    const fetch = installFetch(url => {
      if (url === API + "/suite/version") throw new TypeError("offline");
      if (url === API + "/suite/version?refresh=true") return jsonResponse(200, versionInfo({ status: "unavailable" }));
      return unexpectedFetch(url);
    });
    render(<SuiteScreen />); await tick(); expect(screen.getByText("The Dashboard backend is not answering. Check that the app is running.")).toBeVisible();
    fireEvent.click(screen.getByText("Check again")); await tick(); expect(callsTo(fetch, "/suite/version?refresh=true")).toHaveLength(1);
  });
  it("scores exact quoted/non-ASCII paths and stops on done", async () => {
    let n = 0; const fetch = setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: "s1" });
      if (url === API + "/job/s1") return jsonResponse(200, ++n === 1 ? jobOf() : jobOf({ status: "done", results: scoreDoc() }));
      return unexpectedFetch(url);
    });
    render(<SuiteScreen />); await tick(); enter('C:\\fixtures\\suite\\clip_a_crf18.mkv\n"D:\\Médias\\Tom & Jerry 100%.mkv"');
    fireEvent.click(screen.getByText("Score files")); await tick(); expect(bodyOf(fetch, "/suite/score")).toEqual({ paths: ["C:\\fixtures\\suite\\clip_a_crf18.mkv", "D:\\Médias\\Tom & Jerry 100%.mkv"], lang: "eng" });
    await tick(1500); expect(screen.getAllByRole("heading", { level: 3 }).some(h => h.textContent === "clip_a_crf18.mkv")).toBe(true);
    await tick(10000); expect(callsTo(fetch, "/job/s1")).toHaveLength(2);
  });
  it.each([400, 422])("shows sanitized HTTP %s without polling", async status => {
    const detail = status === 400 ? "not a supported video file" : [{ loc: ["body", "paths"], msg: "List should have at most 50 items after validation, not 51", input: ["C:\\secret\\a.mkv"] }];
    const fetch = setup(url => url === API + "/suite/score" ? jsonResponse(status, { detail }) : unexpectedFetch(url));
    render(<SuiteScreen />); await tick(); enter(); fireEvent.click(screen.getByText("Score files")); await tick();
    expect(screen.getByRole("alert")).toHaveTextContent(status === 400 ? "The request was refused: not a supported video file" : "The request is not valid: paths: List should have at most 50 items after validation, not 51");
    expect(document.body.textContent).not.toContain("secret"); expect(fetch.mock.calls.some(c => String(c[0]).includes("/job/"))).toBe(false);
  });
  it.each(["error", "gone"])("shows terminal score %s", async status => {
    setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: "s1" });
      if (url === API + "/job/s1") return status === "gone" ? jsonResponse(404, {}) : jsonResponse(200, jobOf({ status: "error", error_kind: "no_files", error_detail: "D:\\Media\\gone.mkv" }));
      return unexpectedFetch(url);
    });
    const { container } = render(<SuiteScreen />); await tick(); enter(); fireEvent.click(screen.getByText("Score files")); await tick();
    expect(screen.getByText(status === "gone" ? JOB_GONE_TEXT : "VideoSuite could not read any of the files.")).toBeVisible();
    if (status === "error") expect(container.querySelector(".suite-details")).not.toHaveAttribute("open");
  });
  it("cancels then starts a new score without polling old job", async () => {
    let starts = 0; let polls = 0; const fetch = setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: `s${++starts}` });
      if (url === API + "/job/s1/cancel") return jsonResponse(200, {});
      if (url === API + "/job/s1") return jsonResponse(200, jobOf({ status: ++polls === 1 ? "running" : "cancelled" }));
      if (url === API + "/job/s2") return jsonResponse(200, jobOf({ status: "done", results: scoreDoc() }));
      return unexpectedFetch(url);
    });
    render(<SuiteScreen />); await tick(); enter(); fireEvent.click(screen.getByText("Score files")); await tick();
    const cancel = screen.getByText("Cancel");
    fireEvent.click(cancel); await tick();
    expect(cancel).toBeDisabled();
    fireEvent.click(cancel); await tick();
    expect(callsTo(fetch, "/job/s1/cancel")).toHaveLength(1);
    await tick(1500);
    expect(screen.getByText("Cancelled")).toBeVisible(); fireEvent.click(screen.getByText("Score files")); await tick(10000);
    expect(callsTo(fetch, "/suite/score")).toHaveLength(2); expect(callsTo(fetch, "/job/s1")).toHaveLength(2);
  });
  it("guards pending score starts and obsolete answers", async () => {
    const pending = deferred<Response>(); const fetch = setup(url => url === API + "/suite/score" ? pending.promise : unexpectedFetch(url));
    const { unmount } = render(<SuiteScreen />); await tick(); enter(); const button = screen.getByText("Score files");
    act(() => { fireEvent.click(button); fireEvent.click(button); }); expect(screen.getByText("Starting…")).toBeDisabled(); expect(callsTo(fetch, "/suite/score")).toHaveLength(1);
    unmount(); await act(async () => { pending.resolve(jsonResponse(200, { job_id: "s-old" })); }); await tick(10000); expect(callsTo(fetch, "/job/s-old")).toHaveLength(0);
  });
  it.each(["D:\\a.mkv\nD:\\b.mkv\nD:\\owner.txt", Array.from({ length: 51 }, (_, i) => `D:\\${i}.mkv`).join("\n")])("never sends a valid subset", async text => {
    const fetch = setup(); render(<SuiteScreen />); await tick(); enter(text);
    expect(screen.getByText("Score files")).toBeDisabled(); expect(screen.getByRole("button", { name: "Compare and rank" })).toBeDisabled();
    expect(screen.getByText("Fix the file and language problems before comparing.")).toBeVisible(); fireEvent.click(screen.getByRole("button", { name: "Compare and rank" })); await tick(); expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("blocks invalid language and empty paths then enables corrected inputs", async () => {
    setup(); render(<SuiteScreen />); await tick(); expect(screen.getByText("Enter the video files above first.")).toBeVisible(); enter();
    fireEvent.change(screen.getByLabelText("Audio and subtitle language"), { target: { value: "english" } }); expect(screen.getByText("Score files")).toBeDisabled(); expect(screen.getByRole("button", { name: "Compare and rank" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Audio and subtitle language"), { target: { value: "eng" } }); expect(screen.getByText("Score files")).not.toBeDisabled(); expect(screen.getByRole("button", { name: "Compare and rank" })).not.toBeDisabled();
    expect(chooserBlockReason("", "eng", false)).toBe(SUITE_DISABLED_TEXT); expect(isScoreDocument({ records: [] })).toBe(true); expect(isScoreDocument(null)).toBe(false);
  });
  it.each([[false, false], [true, false], [false, true], [true, true]])("latest completion wins across score/compare/rank, strict=%s, rankFirst=%s", async (strict, rankFirst) => {
    let scores = 0; const p1 = { path: "D:\\Owner\\a.json", loaded: true, errors: [], applied_count: 1 }; const p2 = { path: "D:\\Owner\\b.json", loaded: false, errors: ["settings.json: devices must be an object"], applied_count: 0 };
    setup(url => {
      if (url === API + "/suite/score") return jsonResponse(200, { job_id: `s${++scores}` });
      if (url === API + "/job/s1" || url === API + "/job/s2") return jsonResponse(200, jobOf({ status: "done", results: { ...scoreDoc(), settings: p1 } }));
      if (url === API + "/suite/compare") return jsonResponse(200, { job_id: "c1" });
      if (url === API + "/job/c1") return jsonResponse(200, jobOf({ status: "done", results: { ...compareDoc(), settings: p2 } }));
      if (url === API + "/suite/rank") return jsonResponse(200, { job_id: "r1" });
      if (url === API + "/job/r1") return jsonResponse(200, jobOf({ status: "done", results: { ...rankDoc(), settings: p2 } }));
      return unexpectedFetch(url);
    });
    render(<SuiteScreen />, strict ? { wrapper: StrictMode } : {}); await tick(); enter();
    const settings = () => within(screen.getByRole("region", { name: "Owner settings" }));
    if (!rankFirst) {
      fireEvent.click(screen.getByText("Score files")); await tick();
      expect(settings().getByText("Settings file: D:\\Owner\\a.json (loaded)")).toBeVisible();
    }
    fireEvent.change(screen.getByLabelText("Folder for the comparison reports"), { target: { value: "D:\\Reports\\run" } }); fireEvent.click(screen.getByRole("button", { name: "Compare and rank" })); await tick(10);
    expect(settings().getByText("Settings file: D:\\Owner\\b.json (could not be used; defaults in use)")).toBeVisible();
    fireEvent.click(screen.getByText("Score files")); await tick(); expect(settings().getByText("Settings file: D:\\Owner\\a.json (loaded)")).toBeVisible();
    expect(settings().getByText("Settings used by the latest completed run:")).toBeVisible();
    expect(screen.getByText("Picture quality (device-neutral)")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Device check" })).getByRole("heading", { name: "clip_a_crf18.mkv" })).toBeVisible();
  });
  it("uses only theme variables and a visible notice rule", async () => {
    // Vitest stubs CSS raw imports, including glob imports. Resolve only at test
    // runtime with a local structural type so Node types never enter the app.
    const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: "utf8") => string }>("node:fs");
    const css = readFileSync("src/suite/suite.css", "utf8");
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/); expect(css).toContain("var(--severity-error)"); expect(css).toMatch(/\.suite-notice\s*\{[^}]*display:\s*block/);
  });
});
