// The only door to the real fixtures in tests (Test-harness rules): every accessor returns a deep clone.
import scoreJson from "../../../tests/fixtures/suite/score.json";
import scorePartialJson from "../../../tests/fixtures/suite/score_partial.json";
import rankJson from "../../../tests/fixtures/suite/rank.json";
import compareJson from "../../../tests/fixtures/suite/compare.json";
import devicesJson from "../../../tests/fixtures/suite/devices.json";
import versionJson from "../../../tests/fixtures/suite/version.json";
import type {
  CompareDocument, DevicesDocument, Job, PlaybackResult, RankDocument, ScoreDocument, ScoreRecord, SettingsBlock,
  SuiteSettings, VersionInfo,
} from "../types";

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export const scoreDoc = (): ScoreDocument => clone(scoreJson) as unknown as ScoreDocument;
export const scorePartialDoc = (): ScoreDocument => clone(scorePartialJson) as unknown as ScoreDocument;
export const rankDoc = (): RankDocument => clone(rankJson) as unknown as RankDocument;
export const compareDoc = (): CompareDocument => clone(compareJson) as unknown as CompareDocument;
export const devicesDoc = (): DevicesDocument => clone(devicesJson) as unknown as DevicesDocument;
/** The raw `version --json` document (NOT the /suite/version answer; use versionInfo for that). */
export const versionDoc = (): Record<string, unknown> => clone(versionJson) as unknown as Record<string, unknown>;

/** The /suite/version answer for the fixture suite (keys of suite_version.check). */
export function versionInfo(patch: Partial<VersionInfo> = {}): VersionInfo {
  const v = versionDoc();
  return {
    status: "ok", tool_version: "0.4.0", minimum: "0.3.0", capabilities: v.capabilities as string[],
    schemas: v.schemas as Record<string, string>, engine_tag: "562ab353", message: "VideoSuite 0.4.0",
    checked_at: 1000, ...patch,
  };
}

/** The /suite/settings answer derived from devices.json exactly as T15's settings_view does. */
export function suiteSettings(): SuiteSettings {
  const d = devicesDoc();
  const block = d.settings as SettingsBlock;
  const devices: SuiteSettings["devices"] = {};
  for (const dev of d.devices) {
    devices[dev.id] = { label: dev.label, usb_filesystem: dev.settings?.usb_filesystem ?? null,
                        audio_output: dev.settings?.audio_output ?? null, overrides: [] };
  }
  return { tool_version: d.tool_version, supported: true, path: block.path, loaded: block.loaded,
           errors: block.errors, applied_count: block.applied_count, devices };
}

export function scoreRecord(index = 0): ScoreRecord {
  return scoreDoc().records[index];
}

/** A clone of `record` whose playback row `key` is patched. */
export function withPlayback(record: ScoreRecord, key: string, patch: Partial<PlaybackResult>): ScoreRecord {
  const out = clone(record);
  out.playback[key] = { ...out.playback[key], ...patch };
  return out;
}

/** A job dict with the real backend's keys (jobs.py create + suite runner keys). */
export function jobOf(patch: Partial<Job> = {}): Job {
  return { status: "running", progress: "0/1", current: "", total: 1, results: [], error: null, error_kind: null,
           events: [], kind: "suite_score", ...patch };
}

/** A full legacy VideoData-shaped object for old-flow tests (every field the old dashboard renders). */
export function legacyVideo(path = "D:\\Media\\clip.mkv"): Record<string, unknown> {
  const file = path.split(/[\\/]/).pop() ?? path;
  return {
    path, file, dv_profile: "None", el: "none", bl: "none", rpu: "none", dv_tool: "none", hdr: "SDR",
    bitrate_mbps: 1.1, bit_depth: 8, duration_min: 0.2, file_size_gb: 0.01, source: "WEB-DL",
    audio: "none", audio_score: 0, score: 40, confidence_score: 50, confidence_label: "Medium",
    tv_score: 30, tv_label: "Fair", tv_dv_note: "", usb_compatible: true, usb_issues: [], usb_warnings: [],
    tv_playback: "Plays", quick_summary: "summary", insights: "", recommendation: "",
    signal_facts: [], media_facts: [], tool_reports: [],
  };
}
