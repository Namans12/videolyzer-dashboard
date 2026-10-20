// src/suite/messages.ts — every sentence the Suite tab shows for a status, an error or a progress line.
import type { VersionInfo } from "./types";

// ── HTTP errors ─────────────────────────────────────────────────────────────
/** The readable part of an error body. A pydantic 422 list becomes "loc: msg; ..." and its `input` is never used. */
export function detailText(body: unknown): string {
  if (body === null || typeof body !== "object") return "";
  const d = (body as { detail?: unknown }).detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((item: unknown) => {
        if (item === null || typeof item !== "object") return null;
        const { msg, loc } = item as { msg?: unknown; loc?: unknown };
        if (typeof msg !== "string") return null;
        const where = Array.isArray(loc) ? loc.filter((x) => x !== "body").map(String).join(".") : "";
        return where ? `${where}: ${msg}` : msg;
      })
      .filter((x): x is string => x !== null)
      .join("; ");
  }
  return "";
}

function withDetail(prefix: string, detail: string): string {
  return detail ? `${prefix}: ${detail}` : `${prefix}.`;
}

export function apiErrorSentence(status: number, detail: string): string {
  if (status === 0) return "The Dashboard backend is not answering. Check that the app is running.";
  if (status === 400 || status === 415) return withDetail("The request was refused", detail);
  if (status === 403) return withDetail("Access refused", detail);
  if (status === 404) return withDetail("Not found", detail);
  if (status === 409) return withDetail("Busy", detail);
  if (status === 422) return withDetail("The request is not valid", detail);
  if (status === 503) return withDetail("VideoSuite is not available", detail);
  return withDetail(`The Dashboard reported an error (HTTP ${status})`, detail);
}

// ── job error kinds (docs/SUITE_INTEGRATION.md section 3) ──────────────────────
export const ERROR_KIND_SENTENCES: Record<string, string> = {
  usage: "VideoSuite refused the request (a bad option or device profile).",
  no_files: "VideoSuite could not read any of the files.",
  ffprobe_missing: "ffprobe was not found, so VideoSuite cannot read the files. Reinstall the Comparizer.",
  failed_pairs: "Some comparison pairs failed; the pairs that finished are used.",
  contract: "VideoSuite returned an answer the Dashboard cannot use.",
  timeout: "VideoSuite took too long and was stopped.",
  failed: "VideoSuite failed.",
  unavailable: "VideoSuite is not installed or cannot be started.",
  busy: "Another suite job holds the slot; wait for it to finish and try again.",
  internal: "The Dashboard hit an internal error while running VideoSuite.",
};

export function errorKindSentence(kind: string | null | undefined): string {
  return kind && Object.hasOwn(ERROR_KIND_SENTENCES, kind) ? ERROR_KIND_SENTENCES[kind] : "The job failed.";
}

export const UNREACHABLE_TEXT = "Lost contact with the Dashboard backend; retrying…";
export const CANCELLING_TEXT = "Cancelling… (waiting for the Dashboard to stop the job)";
export const CANCEL_FAILED_TEXT = "The cancel request did not reach the Dashboard; the job is still running.";
export const NO_RECORDS_TEXT = "VideoSuite returned no records.";
export const SUITE_DISABLED_TEXT = "The Suite controls are disabled until VideoSuite is ready.";

// ── version banner ──────────────────────────────────────────────────────────
export type VersionState =
  | { kind: "loading" }
  | { kind: "loaded"; info: VersionInfo }
  | { kind: "failed"; status: number; detail: string };

export interface BannerText { tone: "ok" | "warn" | "error" | "info"; text: string; detail: string | null; }

export function versionBanner(state: VersionState): BannerText {
  if (state.kind === "loading") return { tone: "info", text: "Checking VideoSuite…", detail: null };
  if (state.kind === "failed") return { tone: "error", text: apiErrorSentence(state.status, state.detail), detail: null };
  const { info } = state;
  const detail = info.message ? info.message : null;
  switch (info.status) {
    case "ok": return { tone: "ok", text: `VideoSuite ${info.tool_version ?? "?"} is ready.`, detail: null };
    case "too_old": return { tone: "warn", text: "The installed VideoSuite is too old for this screen.", detail };
    case "unavailable": return { tone: "error", text: "VideoSuite is not installed or cannot be found.", detail };
    case "error": return { tone: "error", text: "VideoSuite did not answer the version check.", detail };
    case "unchecked": return { tone: "warn", text: "The Dashboard could not check the VideoSuite version.", detail };
    default: return { tone: "error", text: "VideoSuite reported an unknown status.", detail };
  }
}

export function suiteReady(state: VersionState): boolean {
  return state.kind === "loaded" && state.info.status === "ok";
}

// ── compare progress (CLI stderr lines, videosuite/cli.py:646-671; proven in plan3-reference) ─────
export const ESTIMATE_RE = /^ *(\d+) file\(s\) shortlisted, (\d+) pair\(s\), roughly (\d+) minutes \((fast|accurate); /;
export const STARTED_RE = /^ *\[(\d+)\/(\d+)\] (.+): started \((fast|accurate)\)$/;
export const PROGRESS_RE = /^ *\[(\d+)\/(\d+)\] (.+?): (\d{1,3})% (.*) \((\d+) min elapsed(?:, about (\d+) min left)?\)$/;
export const HEARTBEAT_RE = /^ *\[(\d+)\/(\d+)\] (.+): still running \((\d+) min elapsed\)$/;
export const PAIR_END_RE = /^ *(?!\[\d+\/\d+\] )(.+): (done|FAILED)$/;

export type ProgressLine =
  | { kind: "estimate"; files: number; pairs: number; minutes: number; mode: string }
  | { kind: "started"; pair: number; pairs: number; label: string; mode: string }
  | { kind: "progress"; pair: number; pairs: number; label: string; percent: number; message: string; elapsedMin: number; etaMin: number | null }
  | { kind: "heartbeat"; pair: number; pairs: number; label: string; elapsedMin: number }
  | { kind: "pair_end"; label: string; ok: boolean };

export function parseProgressLine(line: unknown): ProgressLine | null {
  if (typeof line !== "string") return null;
  const text = line.replace(/[\r\n]+$/, "");
  let m = ESTIMATE_RE.exec(text);
  if (m) return { kind: "estimate", files: +m[1], pairs: +m[2], minutes: +m[3], mode: m[4] };
  m = PROGRESS_RE.exec(text);
  if (m) {
    return { kind: "progress", pair: +m[1], pairs: +m[2], label: m[3], percent: Math.min(100, +m[4]),
             message: m[5], elapsedMin: +m[6], etaMin: m[7] === undefined ? null : +m[7] };
  }
  m = HEARTBEAT_RE.exec(text);
  if (m) return { kind: "heartbeat", pair: +m[1], pairs: +m[2], label: m[3], elapsedMin: +m[4] };
  m = STARTED_RE.exec(text);
  if (m) return { kind: "started", pair: +m[1], pairs: +m[2], label: m[3], mode: m[4] };
  m = PAIR_END_RE.exec(text);
  if (m) return { kind: "pair_end", label: m[1], ok: m[2] === "done" };
  return null;
}

export interface CurrentPair {
  pair: number; pairs: number; label: string;
  percent: number | null; elapsedMin: number | null; etaMin: number | null;
}

export interface CompareProgress {
  estimate: { files: number; pairs: number; minutes: number; mode: string } | null;
  pairsTotal: number | null;
  pairsDone: number;
  pairsFailed: number;
  current: CurrentPair | null;
  lastLine: string | null;
  /** The backend stopped retaining progress lines (suite_routes.py: first 2000 lines + this note). */
  saturated: boolean;
}

/** Exactly the note suite_routes.run_compare_job appends after MAX_PROGRESS_EVENTS lines. */
export const SATURATION_NOTE = "(further progress lines are not recorded)";
export const SATURATED_TEXT = "The Dashboard stopped retaining progress lines; the comparison is still running.";
export const LOG_SUMMARY_TEXT = "Last 50 retained progress lines";
export const BAD_JOB_RESPONSE_TEXT = "The Dashboard returned an unusable job response.";

export function summarizeCompareEvents(events: unknown): CompareProgress {
  const out: CompareProgress = {
    estimate: null, pairsTotal: null, pairsDone: 0, pairsFailed: 0, current: null, lastLine: null, saturated: false,
  };
  for (const ev of Array.isArray(events) ? events : []) {
    const msg = ev !== null && typeof ev === "object" && typeof (ev as { msg?: unknown }).msg === "string"
      ? (ev as { msg: string }).msg : null;
    if (msg === null) continue;
    if (msg.trim() !== "") out.lastLine = msg.trim();
    if (msg === SATURATION_NOTE) { out.saturated = true; continue; }
    const p = parseProgressLine(msg);
    if (!p) continue;
    if (p.kind === "estimate") {
      out.estimate = { files: p.files, pairs: p.pairs, minutes: p.minutes, mode: p.mode };
      out.pairsTotal = p.pairs;
    } else if (p.kind === "started") {
      out.pairsTotal = p.pairs;
      out.current = { pair: p.pair, pairs: p.pairs, label: p.label, percent: null, elapsedMin: null, etaMin: null };
    } else if (p.kind === "progress") {
      out.pairsTotal = p.pairs;
      out.current = { pair: p.pair, pairs: p.pairs, label: p.label, percent: p.percent, elapsedMin: p.elapsedMin, etaMin: p.etaMin };
    } else if (p.kind === "heartbeat") {
      out.pairsTotal = p.pairs;
      const keep = out.current !== null && out.current.pair === p.pair ? out.current.percent : null;
      out.current = { pair: p.pair, pairs: p.pairs, label: p.label, percent: keep, elapsedMin: p.elapsedMin, etaMin: null };
    } else {
      out.pairsDone += 1;
      if (!p.ok) out.pairsFailed += 1;
      out.current = null;
    }
  }
  return out;
}

export function compareRunText(files: number, top: number): string {
  const m = Math.min(files, 2 * top);
  const pairs = (m * (m - 1)) / 2;
  return `${files} file(s) chosen; up to ${m} are compared (at most ${pairs} pair(s)).`;
}

export function estimateText(estimate: { pairs: number; minutes: number }, mode?: string): string {
  return `VideoSuite's own rough estimate: about ${estimate.minutes} minutes for ${estimate.pairs} pair(s)${mode ? ` (${mode})` : ""}.`;
}

export function pairProgressText(c: CurrentPair): string {
  const head = `Pair ${c.pair} of ${c.pairs}: ${c.label}`;
  if (c.percent === null && c.elapsedMin === null) return `${head}: started`;
  if (c.percent === null) return `${head}: still running (${c.elapsedMin} min elapsed)`;
  const tail = c.etaMin !== null
    ? `${c.elapsedMin} min elapsed, about ${c.etaMin} min left for this pair`
    : `${c.elapsedMin} min elapsed`;
  return `${head}: ${c.percent}% (${tail})`;
}

export function pairsDoneText(p: Pick<CompareProgress, "pairsDone" | "pairsTotal" | "pairsFailed">): string {
  const failed = p.pairsFailed > 0 ? `, ${p.pairsFailed} failed` : "";
  return p.pairsTotal === null
    ? `${p.pairsDone} pair(s) finished${failed}.`
    : `${p.pairsDone} of ${p.pairsTotal} pair(s) finished${failed}.`;
}
