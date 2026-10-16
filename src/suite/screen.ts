import { getDevices, getSettings, getVersion, errorSentence, SuiteApiError } from "./api";
import { SUITE_DISABLED_TEXT } from "./messages";
import type { VersionState } from "./messages";
import { checkLang, checkPaths } from "./paths";
import type { DevicesDocument, ScoreDocument, SuiteSettings } from "./types";

export function isScoreDocument(v: unknown): v is ScoreDocument { return v !== null && typeof v === "object" && Array.isArray((v as ScoreDocument).records); }
export function chooserBlockReason(text: string, lang: string, ready: boolean): string | null {
  if (!ready) return SUITE_DISABLED_TEXT;
  const check = checkPaths(text);
  if (check.problems.length === 1 && check.problems[0] === "Enter at least one video file path (one per line)." && !checkLang(lang)) return "Enter the video files above first.";
  return check.problems.length || checkLang(lang) ? "Fix the file and language problems before comparing." : null;
}
export function loadSuiteStatus(refresh: boolean, signal: AbortSignal, on: { version: (s: VersionState) => void; devices: (d: DevicesDocument | null) => void; settings: (s: SuiteSettings | null, error: string | null) => void; done: () => void }): void {
  const live = (fn: () => void) => { if (!signal.aborted) fn(); };
  void getVersion(refresh, signal).then(async info => {
    if (signal.aborted) return;
    live(() => on.version({ kind: "loaded", info }));
    if (info.status !== "ok") return;
    await Promise.all([
      getDevices(signal).then(d => live(() => on.devices(d))).catch(() => live(() => on.devices(null))),
      getSettings(signal).then(s => live(() => on.settings(s, null))).catch((err: unknown) => live(() => on.settings(null, errorSentence(err)))),
    ]);
  }).catch((err: unknown) => live(() => on.version({ kind: "failed", status: err instanceof SuiteApiError ? err.status : 0, detail: err instanceof SuiteApiError ? err.detail : "" })))
    .finally(() => live(on.done));
}
