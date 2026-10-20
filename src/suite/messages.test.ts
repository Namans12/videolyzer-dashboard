import { describe, expect, it } from "vitest";
import {
  apiErrorSentence, compareRunText, detailText, errorKindSentence, estimateText,
  pairProgressText, pairsDoneText, parseProgressLine, SATURATION_NOTE,
  summarizeCompareEvents, suiteReady, versionBanner,
} from "./messages";
import type { CurrentPair, VersionState } from "./messages";
import { versionInfo } from "./testing/fixtures";

describe("plain status sentences", () => {
  it.each([
    [0, "", "The Dashboard backend is not answering. Check that the app is running."],
    [400, "not a supported video file", "The request was refused: not a supported video file"],
    [403, "origin not allowed", "Access refused: origin not allowed"],
    [404, "path not found", "Not found: path not found"],
    [409, "a comparison is already running", "Busy: a comparison is already running"],
    [415, "Content-Type must be application/json", "The request was refused: Content-Type must be application/json"],
    [422, "paths: List should have at most 50 items after validation, not 51", "The request is not valid: paths: List should have at most 50 items after validation, not 51"],
    [503, "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)", "VideoSuite is not available: VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"],
    [502, "VideoSuite failed.", "The Dashboard reported an error (HTTP 502): VideoSuite failed."],
    [409, "", "Busy."], [500, "", "The Dashboard reported an error (HTTP 500)."],
  ] as const)("formats HTTP %s", (status, detail, expected) => {
    expect(apiErrorSentence(status, detail)).toBe(expected);
  });
  it.each([
    ["usage", "VideoSuite refused the request (a bad option or device profile)."],
    ["no_files", "VideoSuite could not read any of the files."],
    ["ffprobe_missing", "ffprobe was not found, so VideoSuite cannot read the files. Reinstall the Comparizer."],
    ["failed_pairs", "Some comparison pairs failed; the pairs that finished are used."],
    ["contract", "VideoSuite returned an answer the Dashboard cannot use."],
    ["timeout", "VideoSuite took too long and was stopped."],
    ["failed", "VideoSuite failed."],
    ["unavailable", "VideoSuite is not installed or cannot be started."],
    ["busy", "Another suite job holds the slot; wait for it to finish and try again."],
    ["internal", "The Dashboard hit an internal error while running VideoSuite."],
    [null, "The job failed."], ["weird", "The job failed."],
  ])("formats job error %s", (kind, expected) => {
    expect(errorKindSentence(kind)).toBe(expected);
  });
  it.each([
    ["ok", "ok", "VideoSuite 0.4.0 is ready.", null],
    ["too_old", "warn", "The installed VideoSuite is too old for this screen.", "M"],
    ["unavailable", "error", "VideoSuite is not installed or cannot be found.", "M"],
    ["error", "error", "VideoSuite did not answer the version check.", "M"],
    ["unchecked", "warn", "The Dashboard could not check the VideoSuite version.", "M"],
    ["strange", "error", "VideoSuite reported an unknown status.", "M"],
  ])("formats version status %s", (status, tone, text, detail) => {
    const state: VersionState = { kind: "loaded", info: versionInfo({ status: status!, message: "M" }) };
    expect(versionBanner(state)).toEqual({ tone, text, detail });
    expect(suiteReady(state)).toBe(status === "ok");
  });
  it("handles pending and failed checks and empty version messages", () => {
    const loading: VersionState = { kind: "loading" };
    const failed: VersionState = { kind: "failed", status: 0, detail: "" };
    expect(versionBanner(loading)).toEqual({ tone: "info", text: "Checking VideoSuite…", detail: null });
    expect(versionBanner(failed)).toEqual({ tone: "error", text: apiErrorSentence(0, ""), detail: null });
    expect(suiteReady(loading)).toBe(false);
    expect(suiteReady(failed)).toBe(false);
    expect(versionBanner({ kind: "loaded", info: versionInfo({ status: "too_old", message: "" }) }).detail).toBeNull();
  });
  it.each([
    [3, 4, "3 file(s) chosen; up to 3 are compared (at most 3 pair(s))."],
    [10, 4, "10 file(s) chosen; up to 8 are compared (at most 28 pair(s))."],
    [10, 2, "10 file(s) chosen; up to 4 are compared (at most 6 pair(s))."],
    [1, 4, "1 file(s) chosen; up to 1 are compared (at most 0 pair(s))."],
  ] as const)("describes %s files with top %s", (files, top, expected) => {
    expect(compareRunText(files, top)).toBe(expected);
  });
  it("uses only the supplied CLI estimate", () => {
    expect(estimateText({ pairs: 3, minutes: 12 }, "fast")).toBe("VideoSuite's own rough estimate: about 12 minutes for 3 pair(s) (fast).");
    expect(estimateText({ pairs: 3, minutes: 12 })).toBe("VideoSuite's own rough estimate: about 12 minutes for 3 pair(s).");
  });
  it("describes pair progress and finished counts", () => {
    const pair: CurrentPair = { pair: 1, pairs: 3, label: "A vs B", percent: null, elapsedMin: null, etaMin: null };
    expect(pairProgressText(pair)).toBe("Pair 1 of 3: A vs B: started");
    expect(pairProgressText({ ...pair, elapsedMin: 12 })).toBe("Pair 1 of 3: A vs B: still running (12 min elapsed)");
    expect(pairProgressText({ ...pair, percent: 25, elapsedMin: 0, etaMin: 1 })).toBe("Pair 1 of 3: A vs B: 25% (0 min elapsed, about 1 min left for this pair)");
    expect(pairProgressText({ ...pair, percent: 3, elapsedMin: 0 })).toBe("Pair 1 of 3: A vs B: 3% (0 min elapsed)");
    expect(pairsDoneText({ pairsDone: 1, pairsTotal: 3, pairsFailed: 0 })).toBe("1 of 3 pair(s) finished.");
    expect(pairsDoneText({ pairsDone: 2, pairsTotal: 3, pairsFailed: 1 })).toBe("2 of 3 pair(s) finished, 1 failed.");
    expect(pairsDoneText({ pairsDone: 0, pairsTotal: null, pairsFailed: 0 })).toBe("0 pair(s) finished.");
  });
});

const REAL_LINES = [
  "3 file(s) shortlisted, 3 pair(s), roughly 12 minutes (fast; rough estimate, Dolby Vision RPU reading with --deep-dv adds several minutes per file)",
  "  [1/3] Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: started (fast)",
  "  [1/3] Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: 25% Analyzed 1/69 frame pairs (streaming)... (0 min elapsed, about 1 min left)",
  "  Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv: done",
  "  [2/3] Tom & Jerry (2021) 100%.mkv vs plain_b.mp4: started (fast)",
  "  [2/3] Tom & Jerry (2021) 100%.mkv vs plain_b.mp4: 25% Analyzed 1/69 frame pairs (streaming)... (0 min elapsed, about 1 min left)",
  "  Tom & Jerry (2021) 100%.mkv vs plain_b.mp4: done",
  "  [3/3] Спайдер-Мен 2160p.mkv vs plain_b.mp4: started (fast)",
  "  [3/3] Спайдер-Мен 2160p.mkv vs plain_b.mp4: 25% Analyzed 1/69 frame pairs (streaming)... (0 min elapsed, about 1 min left)",
  "  Спайдер-Мен 2160p.mkv vs plain_b.mp4: done"
];

describe("captured CLI progress and HTTP detail reference", () => {
  it("passes every assertion from progress_reference.mjs", () => {
    const assert = {
      deepEqual(actual: unknown, expected: unknown) { expect(actual).toEqual(expected); },
      equal(actual: unknown, expected: unknown, message?: string) { expect(actual, message).toBe(expected); },
    };
    function progress(line: string) {
      const parsed = parseProgressLine(line);
      if (parsed?.kind !== "progress") throw new Error("Expected a progress line");
      return parsed;
    }
const real = REAL_LINES;
const parsed = real.map(parseProgressLine);
assert.deepEqual(parsed[0], { kind: "estimate", files: 3, pairs: 3, minutes: 12, mode: "fast" });
assert.deepEqual(parsed[1], { kind: "started", pair: 1, pairs: 3, label: "Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv", mode: "fast" });
assert.deepEqual(parsed[2], { kind: "progress", pair: 1, pairs: 3, label: "Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv",
                              percent: 25, message: "Analyzed 1/69 frame pairs (streaming)...", elapsedMin: 0, etaMin: 1 });
assert.deepEqual(parsed[3], { kind: "pair_end", label: "Tom & Jerry (2021) 100%.mkv vs Спайдер-Мен 2160p.mkv", ok: true });
assert.equal(parsed.filter((p) => p === null).length, 0, "every real line parses");
// synthesized with the CLI's own f-strings (cli.py:654-671), values the fake runner cannot produce
const syn = {
  heartbeat: "  [2/3] A.mkv vs B.mkv: still running (12 min elapsed)",
  noEta: "  [2/3] A.mkv vs B.mkv: 3% Loading (0 min elapsed)",
  hundred: "  [2/3] A.mkv vs B.mkv: 100% Done (41 min elapsed)",
  emptyMsg: "  [2/3] A.mkv vs B.mkv: 50%  (7 min elapsed, about 7 min left)",
  failed: "  A.mkv vs B.mkv: FAILED",
  oserr: "  A.mkv vs B.mkv: PermissionError: [WinError 32] locked",
  accurate: "8 file(s) shortlisted, 28 pair(s), roughly 1680 minutes (accurate; rough estimate, Dolby Vision RPU reading with --deep-dv adds several minutes per file)",
};
assert.deepEqual(parseProgressLine(syn.heartbeat), { kind: "heartbeat", pair: 2, pairs: 3, label: "A.mkv vs B.mkv", elapsedMin: 12 });
assert.equal(progress(syn.noEta).etaMin, null);
assert.equal(progress(syn.noEta).percent, 3);
assert.equal(progress(syn.hundred).percent, 100);
assert.equal(progress(syn.emptyMsg).message, "");
assert.equal(progress(syn.emptyMsg).etaMin, 7);
assert.deepEqual(parseProgressLine(syn.failed), { kind: "pair_end", label: "A.mkv vs B.mkv", ok: false });
assert.equal(parseProgressLine(syn.oserr), null);
assert.deepEqual(parseProgressLine(syn.accurate), { kind: "estimate", files: 8, pairs: 28, minutes: 1680, mode: "accurate" });
for (const other of ["Comparing 3 file(s) (fast)", "Done.", "Cancelled.", "(further progress lines are not recorded)",
                     "videosuite: settings: settings.json: devices must be an object", "", "  skipped C.mkv: will not play on any device"]) {
  assert.equal(parseProgressLine(other), null, other);
}
assert.equal(progress("  [1/3] A.mkv vs B.mkv: 25% x (0 min elapsed, about 1 min left)\r\n").percent, 25);
// summary over the real events, as the job's events list would hold them
const events = [{ msg: "Comparing 3 file(s) (fast)", ts: 1 }, ...real.map((msg, i) => ({ msg, ts: i + 2 }))];
const sum = summarizeCompareEvents(events);
assert.deepEqual(sum.estimate, { files: 3, pairs: 3, minutes: 12, mode: "fast" });
assert.equal(sum.pairsTotal, 3); assert.equal(sum.pairsDone, 3); assert.equal(sum.pairsFailed, 0); assert.equal(sum.current, null);
assert.equal(sum.lastLine, "Спайдер-Мен 2160p.mkv vs plain_b.mp4: done");
const mid = summarizeCompareEvents(events.slice(0, 7));
assert.deepEqual(mid.current, { pair: 2, pairs: 3, label: "Tom & Jerry (2021) 100%.mkv vs plain_b.mp4", percent: 25, elapsedMin: 0, etaMin: 1 });
assert.equal(mid.pairsDone, 1);
// detail extraction on REAL bodies
assert.equal(detailText({ detail: [{ type: "too_long", loc: ["body", "paths"], msg: "List should have at most 50 items after validation, not 51", input: ["C:\\secret\\a.mkv"] }] }),
             "paths: List should have at most 50 items after validation, not 51");
assert.equal(detailText({ detail: [{ type: "value_error", loc: ["body", "fs"], msg: "Value error, fs must be fat32, exfat or ntfs", input: "hfs", ctx: { error: {} } }] }),
             "fs: Value error, fs must be fat32, exfat or ntfs");
assert.equal(detailText({ detail: "not a supported video file" }), "not a supported video file");
assert.equal(detailText({ detail: [{ loc: ["body", "paths", 3], msg: "String should have at most 4096 characters" }, { msg: "x" }] }),
             "paths.3: String should have at most 4096 characters; x");
assert.equal(detailText(null), ""); assert.equal(detailText({ detail: 5 }), ""); assert.equal(detailText("<b>x</b>"), "");
// a JSON body built by the UI keeps & % non-ASCII and backslashes exactly
const p = "D:\\Médias\\Tom & Jerry (2021) 100%\\Спайдер-Мен.mkv";
assert.equal(JSON.parse(JSON.stringify({ paths: [p] })).paths[0], p);
// S11: the backend keeps the FIRST 2000 lines plus one note (suite_routes.py:304-311), not a sliding window
const many = [{ msg: "Comparing 3 file(s) (fast)", ts: 0 }, ...real.slice(0, 3).map((msg, i) => ({ msg, ts: i + 1 }))];
while (many.length < 2000) many.push({ msg: real[2], ts: many.length });
many.push({ msg: SATURATION_NOTE, ts: 2001 });
const sat = summarizeCompareEvents(many);
assert.equal(sat.saturated, true);
assert.equal(summarizeCompareEvents(events).saturated, false);

    expect(detailText({ detail: [{ loc: ["body", "paths"], msg: "invalid", input: ["C:\\secret\\a.mkv"] }] })).not.toContain("secret");
  });
});
