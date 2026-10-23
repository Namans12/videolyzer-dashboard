// src/compare/types.ts — the unified comparison run contracts (Plan 4B), their guards and the pure form helpers.
// Shapes mirror comparison_contracts.py exactly: RunRequest, PublicRunSnapshot (alias RunSnapshot), RunResult and the
// videosuite-quick/1 QuickOrder. There is no frontend internal snapshot: `reports_dir` is refused wherever it appears.
// Nested suite documents keep the additive-key tolerance of the suite contract; required fields are checked.
import supportedDevicesJson from "./supported-devices.json";
import { hasControl, pathIdentity, VIDEO_EXTENSIONS } from "../suite/paths";
import { EVIDENCE_WORDS, VERDICTS } from "../suite/types";
import type { DevicesDocument, PlayerInfo, RankDocument, VersionInfo } from "../suite/types";

// ── request ────────────────────────────────────────────────────────────────────
export type InputKind = "files" | "magnets";
export type ResultsChoice = "overall" | "device" | "both";
export type Depth = "quick" | "full";
export type FsChoice = "fat32" | "exfat" | "ntfs";
export type AudioOutput = "tv_speakers" | "soundbar" | "avr";

export interface MagnetInput { uri: string; file_index: number | null }

export interface RunRequest {
  input_kind: InputKind;
  results: ResultsChoice;
  depth: Depth;
  paths?: string[];
  magnets?: MagnetInput[];
  device: string | null;
  /** The owner's chosen player (null for Overall). */
  primary_player: string | null;
  /** The evaluation selection: "all" with alternatives, otherwise the primary; null for Overall. */
  player: string | null;
  include_alternatives: boolean;
  fs: FsChoice | null;
  lang: string;
  audio_output: AudioOutput | null;
}

// ── snapshots ──────────────────────────────────────────────────────────────────
export const RUN_STAGES = ["planning", "selection_required", "awaiting_confirmation", "facts", "pixels", "rank", "done",
  "error", "cancelled"] as const;
export type RunStage = (typeof RUN_STAGES)[number];
/** Stages during which a backend job is working on the run. */
export const ACTIVE_STAGES: readonly RunStage[] = ["planning", "facts", "pixels", "rank"];
export const FINAL_STAGES: readonly RunStage[] = ["done", "error", "cancelled"];
export const COMPLETED_STAGES = ["planning", "facts", "quick", "pixels", "rank"] as const;
export const FAILURE_STAGES = ["planning", "selection", "facts", "quick", "pixels", "rank", "report"] as const;

export interface MagnetEntry { index: number; name: string; size: number | null }

export interface RunCandidate {
  candidate_id: string;
  kind: "file" | "magnet";
  path: string | null;
  uri: string | null;
  file_index: number | null;
  status: "pending" | "ready" | "unreadable" | "selection_required" | "failed";
  record_id: string | null;
  reason: string | null;
  size: number | null;
  mtime_ns: number | null;
  entries?: MagnetEntry[];
}

export interface RunConditions {
  device: string | null;
  primary_player: string | null;
  player: string | null;
  evaluated_players: string[];
  fs: FsChoice | null;
  lang: string;
  audio_output: AudioOutput | null;
  settings_digest: string | null;
}

export interface RunVersions {
  tool_version: string | null;
  engine_tag: string | null;
  facts_version: number | null;
  rules_versions: Record<string, number>;
}

export interface RunEstimate { pairs: number; minutes: number }

/** The HTTP/frontend run snapshot. It has no `reports_dir` member by contract. */
export interface PublicRunSnapshot {
  run_id: string;
  plan_revision: string;
  request: RunRequest;
  stage: RunStage;
  job_id: string | null;
  candidates: RunCandidate[];
  conditions: RunConditions;
  versions: RunVersions;
  records_digest: string | null;
  estimate: RunEstimate | null;
  created_at: number;
}
export type RunSnapshot = PublicRunSnapshot;

// ── Quick (videosuite-quick/1) ─────────────────────────────────────────────────
export type QuickGroup = {
  rank: number | null; ids: string[]; status: "estimated" | "tie" | "insufficient";
  basis: Record<string, Record<string, unknown>>; limitations: string[];
};
export type QuickOrder = { groups: QuickGroup[]; winner_ids: string[]; coverage_complete: boolean };
export type QuickAlternative = { device: string; player: string; order: QuickOrder };
export type QuickDocument = {
  schema: "videosuite-quick/1"; tool_version: string; results: ResultsChoice; measurement: "metadata";
  records: Array<Record<string, unknown>>; overall: QuickOrder | null; devices: Record<string, QuickOrder>;
  selected: { device: string | null; player: string | null }; alternatives: QuickAlternative[];
  failures: Array<{ path: string; reason: string }>; settings: Record<string, unknown> | null;
};

/** A real videosuite-rank/1 document with the Plan 4B additive fields. */
export type RankAnswer = RankDocument & {
  results?: ResultsChoice;
  coverage?: { expected_pairs: number; scheduled_pairs: number; completed_pairs: number; missing_pairs: number };
  selected?: { device: string | null; player: string | null };
  evaluated_players?: string[];
} & Record<string, unknown>;

// ── result ─────────────────────────────────────────────────────────────────────
export interface RunCoverage {
  submitted: number; readable: number; expected_pairs: number; completed_pairs: number; missing_pairs: number;
  complete: boolean;
}
export interface RunFailure { candidate_id: string | null; stage: (typeof FAILURE_STAGES)[number]; kind: string; message: string }
export interface ReportRef { report_id: string; name: string; kind: "json" | "text" | "html" | "log"; url: string; sha256: string }
export interface RunProvenance {
  build_id: string | null; tool_version: string | null; engine_tag: string | null; facts_version: number | null;
  rules_versions: Record<string, number>; records_digest: string | null; settings_digest: string | null;
}
/** One media-record/1 fact row (the run's frozen facts). */
export type MediaRecord = { id: string; schema: string; source: { path: string } & Record<string, unknown> } & Record<string, unknown>;

interface RunResultBase {
  schema: "videosuite-comparison-run/1";
  run_id: string;
  request: RunRequest;
  snapshot: PublicRunSnapshot;
  completed_stages: Array<(typeof COMPLETED_STAGES)[number]>;
  coverage: RunCoverage;
  facts: MediaRecord[];
  failures: RunFailure[];
  reports: ReportRef[];
  estimate: RunEstimate | null;
  provenance: RunProvenance;
}
export type RunResult = RunResultBase & (
  | { measurement: "metadata"; overall: QuickOrder | null; device: QuickDocument | null }
  | { measurement: "pixels"; overall: RankAnswer | null; device: RankAnswer | null }
);

export interface RunDetail { run: RunSnapshot; result: RunResult | null }
export interface RunList { runs: RunSnapshot[] }
export interface RunStarted { job_id: string; run_id: string }
export interface RunJob {
  status: "running" | "done" | "error" | "cancelled";
  progress: string;
  current: string;
  events: { msg: string; ts: number }[];
  error: string | null;
  error_kind: string | null;
  kind: string;
  run: RunSnapshot | null;
  results: RunResult | null;
}

// ── guards ─────────────────────────────────────────────────────────────────────
/** A response broke the frozen contract. The message names the place, never the received value. */
export class ContractViolation extends Error {
  constructor(where: string) {
    super(`Unusable comparison data: ${where}`);
    this.name = "ContractViolation";
  }
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => v !== null && typeof v === "object" && !Array.isArray(v);
const has = (v: Obj, k: string): boolean => Object.prototype.hasOwnProperty.call(v, k);
const own = (v: unknown, k: string): unknown => (isObj(v) && has(v, k) ? v[k] : undefined);
const isStr = (v: unknown): v is string => typeof v === "string";
const isStrOrNull = (v: unknown): v is string | null => v === null || typeof v === "string";
const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v);
const isCount = (v: unknown): v is number => isInt(v) && v >= 0;
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isStrings = (v: unknown): v is string[] => Array.isArray(v) && v.every(isStr);
const oneOf = <T extends string>(v: unknown, xs: readonly T[]): v is T => typeof v === "string" && (xs as readonly string[]).includes(v);
const DIGEST = /^[0-9a-f]{64}$/;
const OPAQUE = /^[A-Za-z0-9_-]{1,128}$/;
const LANG = /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
const FS: readonly FsChoice[] = ["fat32", "exfat", "ntfs"];
const AUDIO: readonly AudioOutput[] = ["tv_speakers", "soundbar", "avr"];
const isDigestOrNull = (v: unknown) => v === null || (isStr(v) && DIGEST.test(v));

function need(ok: boolean, where: string): asserts ok {
  if (!ok) throw new ContractViolation(where);
}

function containsKey(value: unknown, key: string): boolean {
  if (Array.isArray(value)) return value.some((item) => containsKey(item, key));
  if (!isObj(value)) return false;
  return has(value, key) || Object.values(value).some((item) => containsKey(item, key));
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isObj(value)) return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  return JSON.stringify(value);
}
/** Structural equality of two JSON values (key order ignored). */
export function sameJson(a: unknown, b: unknown): boolean {
  return canonical(a) === canonical(b);
}

export const SUPPORTED_DEVICE_IDS: readonly string[] = supportedDevicesJson.devices.map((d) => d.id);
const REQUEST_KEYS = ["input_kind", "results", "depth", "paths", "magnets", "device", "primary_player", "player",
  "include_alternatives", "fs", "lang", "audio_output"];

export function asRunRequest(value: unknown, where = "request"): RunRequest {
  need(isObj(value), where);
  need(Object.keys(value).every((k) => REQUEST_KEYS.includes(k)), `${where}: unexpected key`);
  const v = value;
  need(oneOf(v.input_kind, ["files", "magnets"] as const), `${where}.input_kind`);
  need(oneOf(v.results, ["overall", "device", "both"] as const), `${where}.results`);
  need(oneOf(v.depth, ["quick", "full"] as const), `${where}.depth`);
  need(typeof v.include_alternatives === "boolean", `${where}.include_alternatives`);
  need(isStr(v.lang) && LANG.test(v.lang), `${where}.lang`);
  need(v.fs === null || oneOf(v.fs, FS), `${where}.fs`);
  need(v.audio_output === null || oneOf(v.audio_output, AUDIO), `${where}.audio_output`);
  for (const k of ["device", "primary_player", "player"]) need(isStrOrNull(v[k]), `${where}.${k}`);
  if (v.input_kind === "files") {
    need(isStrings(v.paths) && v.paths.length > 0 && v.paths.length <= 50 && (v.magnets === undefined || v.magnets === null),
      `${where}.paths`);
  } else {
    need(Array.isArray(v.magnets) && v.magnets.length > 0 && v.magnets.length <= 8 && (v.paths === undefined || v.paths === null)
      && v.magnets.every((m) => isObj(m) && isStr(m.uri) && m.uri.startsWith("magnet:?")
        && (m.file_index === null || isCount(m.file_index))), `${where}.magnets`);
    need(v.depth === "quick", `${where}: magnets are Quick only`);
  }
  if (v.results === "overall") {
    need(v.device === null && v.primary_player === null && v.player === null && v.fs === null && v.audio_output === null,
      `${where}: overall takes no device or players`);
  } else {
    need(isStr(v.device) && SUPPORTED_DEVICE_IDS.includes(v.device), `${where}.device`);
    need(isStr(v.primary_player) && v.primary_player !== "all", `${where}.primary_player`);
    need(v.player === (v.include_alternatives ? "all" : v.primary_player), `${where}.player`);
  }
  return value as unknown as RunRequest;
}

function asCandidate(value: unknown, where: string): RunCandidate {
  need(isObj(value), where);
  const v = value;
  need(isStr(v.candidate_id), `${where}.candidate_id`);
  need(oneOf(v.kind, ["file", "magnet"] as const), `${where}.kind`);
  need(oneOf(v.status, ["pending", "ready", "unreadable", "selection_required", "failed"] as const), `${where}.status`);
  for (const k of ["path", "uri", "record_id", "reason"]) need(v[k] === undefined || isStrOrNull(v[k]), `${where}.${k}`);
  for (const k of ["file_index", "size", "mtime_ns"]) need(v[k] === undefined || v[k] === null || isCount(v[k]), `${where}.${k}`);
  need((v.kind === "file") === isStr(v.path) && (v.kind === "magnet") === isStr(v.uri), `${where}: source`);
  need(v.status !== "ready" || isStr(v.record_id), `${where}.record_id`);
  if (v.entries !== undefined && v.entries !== null) {
    need(Array.isArray(v.entries) && v.entries.every((e) => isObj(e) && isCount(e.index) && isStr(e.name)
      && (e.size === undefined || e.size === null || isCount(e.size))), `${where}.entries`);
  }
  return {
    candidate_id: v.candidate_id, kind: v.kind, path: (v.path ?? null) as string | null, uri: (v.uri ?? null) as string | null,
    file_index: (v.file_index ?? null) as number | null, status: v.status, record_id: (v.record_id ?? null) as string | null,
    reason: (v.reason ?? null) as string | null, size: (v.size ?? null) as number | null,
    mtime_ns: (v.mtime_ns ?? null) as number | null,
    ...(Array.isArray(v.entries) ? { entries: (v.entries as Obj[]).map((e) => ({
      index: e.index as number, name: e.name as string, size: (e.size ?? null) as number | null })) } : {}),
  };
}

function asEstimate(value: unknown, where: string): RunEstimate | null {
  if (value === null) return null;
  need(isObj(value) && isCount(value.pairs) && isCount(value.minutes), where);
  return value as unknown as RunEstimate;
}

function asRulesVersions(value: unknown, where: string): void {
  need(isObj(value) && Object.values(value).every(isInt), where);
}

/** Narrows a PublicRunSnapshot; a `reports_dir` member (internal-only) is a contract violation. */
export function asPublicSnapshot(value: unknown, where = "run"): PublicRunSnapshot {
  need(isObj(value), where);
  need(!has(value, "reports_dir"), `${where}: an internal-only field was sent`);
  const v = value;
  need(isStr(v.run_id) && OPAQUE.test(v.run_id), `${where}.run_id`);
  need(isStr(v.plan_revision) && OPAQUE.test(v.plan_revision), `${where}.plan_revision`);
  const request = asRunRequest(v.request, `${where}.request`);
  need(oneOf(v.stage, RUN_STAGES), `${where}.stage`);
  need(isStrOrNull(v.job_id), `${where}.job_id`);
  need(Array.isArray(v.candidates), `${where}.candidates`);
  const candidates = v.candidates.map((c, i) => asCandidate(c, `${where}.candidates[${i}]`));
  const c = v.conditions;
  need(isObj(c), `${where}.conditions`);
  for (const k of ["device", "primary_player", "player", "settings_digest"]) need(isStrOrNull(c[k]), `${where}.conditions.${k}`);
  need(isStrings(c.evaluated_players) && isStr(c.lang), `${where}.conditions`);
  need((c.fs === null || oneOf(c.fs, FS)) && (c.audio_output === null || oneOf(c.audio_output, AUDIO)), `${where}.conditions`);
  need(isDigestOrNull(c.settings_digest), `${where}.conditions.settings_digest`);
  need(c.device === request.device && c.primary_player === request.primary_player && c.player === request.player,
    `${where}.conditions: device or players differ from the request`);
  need(request.results !== "overall" || (c.evaluated_players as string[]).length === 0, `${where}.conditions.evaluated_players`);
  const ver = v.versions;
  need(isObj(ver) && isStrOrNull(ver.tool_version) && isStrOrNull(ver.engine_tag)
    && (ver.facts_version === null || isInt(ver.facts_version)), `${where}.versions`);
  asRulesVersions(ver.rules_versions, `${where}.versions.rules_versions`);
  need(isDigestOrNull(v.records_digest), `${where}.records_digest`);
  const estimate = asEstimate(v.estimate, `${where}.estimate`);
  need(isNum(v.created_at), `${where}.created_at`);
  return {
    run_id: v.run_id, plan_revision: v.plan_revision, request, stage: v.stage, job_id: v.job_id, candidates,
    conditions: c as unknown as RunConditions, versions: ver as unknown as RunVersions,
    records_digest: v.records_digest as string | null, estimate, created_at: v.created_at,
  };
}

const hasMeasuredScore = (value: unknown): boolean => {
  if (Array.isArray(value)) return value.some(hasMeasuredScore);
  if (!isObj(value)) return false;
  return Object.keys(value).some((key) => key === "quality_score" || key === "device_score" || hasMeasuredScore(value[key]));
};

export function asQuickOrder(value: unknown, where = "quick order"): QuickOrder {
  const groups = own(value, "groups");
  need(isObj(value) && Array.isArray(groups) && isStrings(own(value, "winner_ids"))
    && typeof own(value, "coverage_complete") === "boolean", `${where}: not a QuickOrder`);
  for (const group of groups) {
    const rank = own(group, "rank");
    need((rank === null || isInt(rank)) && isStrings(own(group, "ids"))
      && oneOf(own(group, "status"), ["estimated", "tie", "insufficient"] as const)
      && isObj(own(group, "basis")) && isStrings(own(group, "limitations")), `${where}: bad group`);
    for (const row of Object.values(own(group, "basis") as Obj)) {
      need(isObj(row), `${where}: bad basis`);
      const playback = own(row, "playback");
      if (playback === undefined) continue;
      need(oneOf(own(playback, "evidence"), EVIDENCE_WORDS) && oneOf(own(playback, "verdict"), VERDICTS), `${where}: bad playback`);
    }
  }
  need(!hasMeasuredScore(value), `${where}: a metadata estimate carries a measured score`);
  return value as unknown as QuickOrder;
}

/** Narrows a parsed videosuite-quick/1 document; tolerates additive keys, rejects measured scores. */
export function asQuickDocument(value: unknown): QuickDocument {
  const results = own(value, "results");
  need(isObj(value) && own(value, "schema") === "videosuite-quick/1" && own(value, "measurement") === "metadata"
    && oneOf(results, ["overall", "device", "both"] as const) && Array.isArray(own(value, "records"))
    && isObj(own(value, "devices")) && isObj(own(value, "selected")) && Array.isArray(own(value, "alternatives")),
  "not a videosuite-quick/1 document");
  need(isStr(own(value, "tool_version")), "quick tool_version");
  need((own(value, "records") as unknown[]).every(isObj), "quick records");
  const failures = own(value, "failures");
  need(Array.isArray(failures) && failures.every((f) => isStr(own(f, "path")) && isStr(own(f, "reason"))), "quick failures");
  need(has(value, "settings") && (value.settings === null || isObj(value.settings)), "quick settings");
  const overall = own(value, "overall");
  if (overall !== null) asQuickOrder(overall, "overall");
  for (const [key, order] of Object.entries(own(value, "devices") as Obj)) asQuickOrder(order, key);
  for (const alternative of own(value, "alternatives") as unknown[]) {
    need(isStr(own(alternative, "device")) && isStr(own(alternative, "player")), "alternative without device/player identity");
    asQuickOrder(own(alternative, "order"), "alternative");
  }
  const selected = own(value, "selected") as Obj;
  need(isStrOrNull(selected.device) && isStrOrNull(selected.player), "quick selected");
  return value as unknown as QuickDocument;
}

const RANK_ROW_LISTS = ["ranking", "will_not_play", "not_compared"];
/** Absent is fine; present must hold the right type (the renderer calls number methods on these). */
const optional = (v: Obj, k: string, ok: (x: unknown) => boolean): boolean => v[k] === undefined || ok(v[k]);
const numOrNull = (x: unknown) => x === null || isNum(x);
const strOrNull = (x: unknown) => x === null || isStr(x);
const nums = (x: unknown) => Array.isArray(x) && x.every(isNum);

/** One device ranking row: identity, the playback score and verdict are required; every rendered score is a finite
 *  number (or null where the suite says "not compared"). */
function rankRowOk(r: unknown): boolean {
  if (!isObj(r)) return false;
  return isStr(r.id) && isStr(r.label) && isNum(r.playback_score) && isStr(r.verdict)
    && (r.evidence === null || r.evidence === undefined || oneOf(r.evidence, EVIDENCE_WORDS))
    && optional(r, "quality_score", numOrNull) && optional(r, "device_score", numOrNull)
    && optional(r, "ranking_score", numOrNull) && optional(r, "neutral_quality_score", numOrNull)
    && optional(r, "quality_rank", isNum) && optional(r, "device_rank", isNum) && optional(r, "neutral_quality_rank", isNum)
    && optional(r, "tier", isNum) && optional(r, "score_range", nums) && optional(r, "reasons", isStrings)
    && optional(r, "why", strOrNull) && optional(r, "effective_hdr", strOrNull) && optional(r, "effective_audio", strOrNull);
}

function creditOk(f: unknown): boolean {
  return isObj(f) && isStr(f.a) && isStr(f.b) && isStr(f.rule) && isStr(f.shown_as) && strOrNull(f.winner)
    && Array.isArray(f.measured) && f.measured.every(numOrNull);
}

const namesOk = (keys: string[]) => (x: unknown) => Array.isArray(x) && x.every((e) => isObj(e) && keys.every((k) => isStr(e[k])));

/** Narrows a real videosuite-rank/1 document (measured pair evidence); additive keys are kept, but every required or
 *  rendered value must have its contract type, so a malformed document is refused here and never crashes a view. */
export function asRankDocument(value: unknown, where = "rank document"): RankAnswer {
  need(isObj(value) && value.schema === "videosuite-rank/1", `${where}: not a videosuite-rank/1 document`);
  need(isStr(value.tool_version), `${where}.tool_version`);
  need(Array.isArray(value.quality) && value.quality.every((q) => isObj(q) && isStr(q.id) && isStr(q.label)
    && isNum(q.rank) && (q.quality_score === null || isNum(q.quality_score)) && optional(q, "games", isNum)
    && optional(q, "margin", numOrNull)), `${where}.quality`);
  for (const k of ["same_picture_groups", "missing_pairs"]) {
    need(Array.isArray(value[k]) && (value[k] as unknown[]).every(isStrings), `${where}.${k}`);
  }
  need(Array.isArray(value.not_compared), `${where}.not_compared`);
  need(optional(value, "stale_reports", namesOk(["name1", "name2", "reason"])), `${where}.stale_reports`);
  need(optional(value, "legacy_reports", namesOk(["name1", "name2"])), `${where}.legacy_reports`);
  need(optional(value, "head_to_head_conflicts", namesOk(["winner", "ranked_above"])), `${where}.head_to_head_conflicts`);
  need(isObj(value.devices), `${where}.devices`);
  for (const [key, device] of Object.entries(value.devices)) {
    need(isObj(device), `${where}.devices.${key}`);
    for (const list of RANK_ROW_LISTS) {
      const rows = device[list];
      need(Array.isArray(rows) && rows.every(rankRowOk), `${where}.devices.${key}.${list}`);
    }
    need(optional(device, "crossing_note", strOrNull) && optional(device, "crossing", numOrNull)
      && optional(device, "quality_basis", isStr), `${where}.devices.${key}`);
    need(optional(device, "format_credit_removed", (x) => Array.isArray(x) && x.every(creditOk)),
      `${where}.devices.${key}.format_credit_removed`);
  }
  return value as unknown as RankAnswer;
}

function asFailure(value: unknown, where: string): RunFailure {
  need(isObj(value) && isStrOrNull(value.candidate_id) && oneOf(value.stage, FAILURE_STAGES) && isStr(value.kind)
    && isStr(value.message), where);
  return value as unknown as RunFailure;
}

function asReport(value: unknown, runId: string, where: string): ReportRef {
  need(isObj(value) && isStr(value.report_id) && OPAQUE.test(value.report_id) && isStr(value.sha256) && DIGEST.test(value.sha256)
    && oneOf(value.kind, ["json", "text", "html", "log"] as const), where);
  const name = value.name;
  need(isStr(name) && name.length > 0 && name.length <= 255 && !/[\\/]/.test(name) && !hasControl(name), `${where}.name`);
  const base = `/comparison/runs/${runId}/reports/${value.report_id}`;
  need(isStr(value.url) && (value.url === base || value.url === `${base}?download=0` || value.url === `${base}?download=1`),
    `${where}.url`);
  return value as unknown as ReportRef;
}

/** Narrows a videosuite-comparison-run/1 result; the discriminator is depth/measurement. */
export function asRunResult(value: unknown, where = "result"): RunResult {
  need(isObj(value), where);
  need(!containsKey(value, "reports_dir"), `${where}: an internal-only field was sent`);
  const v = value;
  need(v.schema === "videosuite-comparison-run/1", `${where}.schema`);
  need(isStr(v.run_id), `${where}.run_id`);
  const request = asRunRequest(v.request, `${where}.request`);
  const snapshot = asPublicSnapshot(v.snapshot, `${where}.snapshot`);
  need(snapshot.run_id === v.run_id && sameJson(snapshot.request, request), `${where}: differs from its snapshot`);
  const quick = request.depth === "quick";
  need(v.measurement === (quick ? "metadata" : "pixels"), `${where}.measurement`);
  need(Array.isArray(v.completed_stages) && v.completed_stages.every((s) => oneOf(s, COMPLETED_STAGES)), `${where}.completed_stages`);
  const cov = v.coverage;
  need(isObj(cov) && ["submitted", "readable", "expected_pairs", "completed_pairs", "missing_pairs"].every((k) => isCount(cov[k]))
    && typeof cov.complete === "boolean", `${where}.coverage`);
  const c = cov as unknown as RunCoverage;
  need(c.readable <= c.submitted && c.completed_pairs + c.missing_pairs === c.expected_pairs, `${where}.coverage: inconsistent`);
  need(!c.complete || (c.missing_pairs === 0 && c.readable === c.submitted), `${where}.coverage: complete with gaps`);
  need(Array.isArray(v.facts) && v.facts.every((r) => isObj(r) && isStr(r.id) && isStr(own(r.source, "path"))), `${where}.facts`);
  need(Array.isArray(v.failures), `${where}.failures`);
  const failures = v.failures.map((f, i) => asFailure(f, `${where}.failures[${i}]`));
  need(!c.complete || !failures.some((f) => f.stage === "facts" || f.stage === "pixels"), `${where}: complete with failures`);
  need(Array.isArray(v.reports), `${where}.reports`);
  v.reports.forEach((r, i) => asReport(r, v.run_id as string, `${where}.reports[${i}]`));
  asEstimate(v.estimate, `${where}.estimate`);
  const p = v.provenance;
  need(isObj(p) && ["build_id", "tool_version", "engine_tag", "records_digest", "settings_digest"].every((k) => isStrOrNull(p[k]))
    && (p.facts_version === null || isInt(p.facts_version)), `${where}.provenance`);
  asRulesVersions(p.rules_versions, `${where}.provenance.rules_versions`);
  need(request.results !== "overall" || v.device === null, `${where}: an overall run carries no device answer`);
  need(request.results !== "device" || v.overall === null, `${where}: a device run carries no overall answer`);
  if (v.overall !== null) {
    if (quick) asQuickOrder(v.overall, `${where}.overall`);
    else need(Object.keys(asRankDocument(v.overall, `${where}.overall`).devices).length === 0, `${where}.overall: not neutral`);
  }
  if (v.device !== null) {
    if (quick) need(asQuickDocument(v.device).results !== "overall", `${where}.device: no device results`);
    else asRankDocument(v.device, `${where}.device`);
  }
  return { ...(v as unknown as RunResult), snapshot };
}

export function asRunDetail(value: unknown): RunDetail {
  need(isObj(value) && has(value, "run") && has(value, "result"), "run detail");
  const run = asPublicSnapshot(value.run);
  const result = value.result === null ? null : asRunResult(value.result);
  need(result === null || result.run_id === run.run_id, "run detail: result of another run");
  return { run, result };
}

export function asRunList(value: unknown): RunList {
  need(isObj(value) && Array.isArray(value.runs), "run list");
  return { runs: value.runs.map((r, i) => asPublicSnapshot(r, `runs[${i}]`)) };
}

export function asRunStarted(value: unknown): RunStarted {
  need(isObj(value) && isStr(value.job_id) && value.job_id !== "" && isStr(value.run_id) && OPAQUE.test(value.run_id), "run start");
  return { job_id: value.job_id, run_id: value.run_id };
}

/** GET /job/{id} for a comparison plan/run job: `run` is its public snapshot, `results` a RunResult when present. */
export function asRunJob(value: unknown): RunJob {
  need(isObj(value) && oneOf(value.status, ["running", "done", "error", "cancelled"] as const), "job");
  const v = value;
  need(!containsKey(v, "reports_dir"), "job: an internal-only field was sent");
  const events = Array.isArray(v.events)
    ? v.events.filter((e) => isObj(e) && isStr(e.msg)).map((e) => ({ msg: (e as Obj).msg as string, ts: isNum((e as Obj).ts) ? (e as Obj).ts as number : 0 }))
    : [];
  return {
    status: v.status as RunJob["status"], progress: isStr(v.progress) ? v.progress : "", current: isStr(v.current) ? v.current : "", events,
    error: isStr(v.error) ? v.error : null, error_kind: isStr(v.error_kind) ? v.error_kind : null,
    kind: isStr(v.kind) ? v.kind : "",
    run: v.run === undefined || v.run === null ? null : asPublicSnapshot(v.run, "job.run"),
    results: isObj(v.results) ? asRunResult(v.results, "job.results") : null,
  };
}

// ── suite capability ───────────────────────────────────────────────────────────
export const UNIFIED_MIN_VERSION = "0.7.0";
export const UNIFIED_CAPABILITIES = ["compare_all", "compare_plan", "neutral_only", "record_snapshot", "quick_recommendations"];
export const UNIFIED_UPDATE_TEXT = "This screen needs VideoSuite 0.7.0 or later. Run the latest Video Comparator installer, "
  + "then restart the app. Advanced and Calibration keep working with the installed suite.";

/** Whether the installed suite can run the unified flow (the backend checks again before work). */
export function unifiedSupport(info: VersionInfo | null): { ok: boolean; text: string } {
  if (info === null) return { ok: false, text: "Checking VideoSuite…" };
  if (info.status !== "ok") return { ok: false, text: `VideoSuite is not ready. ${info.message || ""}`.trim() };
  const caps = Array.isArray(info.capabilities) ? info.capabilities : [];
  if (!UNIFIED_CAPABILITIES.every((c) => caps.includes(c))) return { ok: false, text: UNIFIED_UPDATE_TEXT };
  return { ok: true, text: `VideoSuite ${info.tool_version ?? "?"} is ready.` };
}

// ── fixed devices ──────────────────────────────────────────────────────────────
export interface SupportedDevice {
  id: string;
  /** The release's fixed short label, e.g. "Sony Bravia 8 II (TV)". */
  label: string;
  /** The catalogue's own label from /suite/devices. */
  catalogueLabel: string;
  players: PlayerInfo[];
  defaultPlayer: string | null;
}

/** The fixed supported devices, in release order, that the live catalogue actually contains (extra profiles ignored). */
export function supportedDevices(devices: DevicesDocument | null): SupportedDevice[] {
  if (devices === null || !Array.isArray(devices.devices)) return [];
  const out: SupportedDevice[] = [];
  for (const fixed of supportedDevicesJson.devices) {
    const found = devices.devices.find((d) => d.id === fixed.id);
    if (!found || !Array.isArray(found.players)) continue;
    const players = found.players.filter((p) => isStr(p.id) && isStr(p.label));
    out.push({ id: fixed.id, label: fixed.label, catalogueLabel: found.label, players,
               defaultPlayer: (players.find((p) => p.default) ?? players[0])?.id ?? null });
  }
  return out;
}

export function deviceLabel(id: string | null): string {
  return supportedDevicesJson.devices.find((d) => d.id === id)?.label ?? (id ?? "");
}

export function playerLabel(devices: DevicesDocument | null, device: string | null, player: string | null): string {
  if (player === null) return "";
  const found = devices?.devices.find((d) => d.id === device)?.players.find((p) => p.id === player);
  return found ? found.label : player;
}

// ── copy ───────────────────────────────────────────────────────────────────────
export const ONE_TITLE_TEXT = "Add versions of one movie/title only. If you mixed titles or episodes, remove them and start "
  + "a separate run for each title.";
export const TRANSFER_NOTICE = "Checking magnet links fetches limited pieces of each movie (the start, and for MP4/M2TS "
  + "also the end) and may exchange data with other peers. It is not a full download, and the facts it reads are partial.";
export const CLOSE_TAB_TEXT = "You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.";
export const FULL_NEEDS_FILES_TEXT = "Download the files first, then choose Downloaded files.";
export const RESULTS_LABELS: Record<ResultsChoice, string> = { overall: "Best overall", device: "Best for my device", both: "Both" };
export const DEPTH_LABELS: Record<Depth, string> = { quick: "Quick (metadata)", full: "Full (picture comparison)" };
export const FS_LABELS: Record<FsChoice, string> = { exfat: "exFAT", fat32: "FAT32", ntfs: "NTFS" };
export const AUDIO_LABELS: Record<AudioOutput, string> = { tv_speakers: "TV speakers", soundbar: "Soundbar", avr: "AV receiver" };
export const STAGE_TEXT: Record<RunStage, string> = {
  planning: "Reading the files and planning…", selection_required: "Choose one video file for each link.",
  awaiting_confirmation: "Plan ready. Check it, then start.", facts: "Reading file facts…",
  pixels: "Comparing the pictures pair by pair…", rank: "Ranking…", done: "Finished.", error: "Stopped with a problem.",
  cancelled: "Cancelled.",
};

// ── form ───────────────────────────────────────────────────────────────────────
export interface CompareForm {
  inputKind: InputKind;
  pathsText: string;
  magnetsText: string;
  /** Explicit video-entry choice per magnet link (from the returned file list). */
  fileChoices: Record<string, number>;
  results: ResultsChoice;
  depth: Depth;
  device: string;
  /** "" means "this device's default player" until the catalogue is loaded. */
  primaryPlayer: string;
  includeAlternatives: boolean;
  fs: FsChoice | null;
  audioOutput: AudioOutput | null;
  lang: string;
}

export function defaultPlayerFor(devices: DevicesDocument | null, device: string): string {
  return supportedDevices(devices).find((d) => d.id === device)?.defaultPlayer ?? "";
}

/** The fresh form: Both, TV with its default player, Full for downloaded files and Quick for magnets. */
export function defaultForm(kind: InputKind, devices: DevicesDocument | null): CompareForm {
  const device = SUPPORTED_DEVICE_IDS[0];
  return {
    inputKind: kind, pathsText: "", magnetsText: "", fileChoices: {}, results: "both", depth: kind === "magnets" ? "quick" : "full",
    device, primaryPlayer: defaultPlayerFor(devices, device), includeAlternatives: true, fs: null, audioOutput: null, lang: "eng",
  };
}

/** Switching input resets the displayed depth default and any file-entry choices. */
export function switchInput(form: CompareForm, kind: InputKind): CompareForm {
  return { ...form, inputKind: kind, depth: kind === "magnets" ? "quick" : "full", fileChoices: {} };
}

/** A new device starts from its own default player and its own saved conditions: a USB file system or audio output
 *  picked for another device is never carried over. */
export function chooseDevice(form: CompareForm, device: string, devices: DevicesDocument | null): CompareForm {
  if (device === form.device) return form;
  return { ...form, device, primaryPlayer: defaultPlayerFor(devices, device), fs: null, audioOutput: null };
}

/** The per-run playback conditions go back to "your saved setting" for a new comparison. */
export function clearRunConditions(form: CompareForm): CompareForm {
  return { ...form, fs: null, audioOutput: null };
}

/** A magnet link names its torrent by an info-hash: 40 hex characters or the 32-character base32 form (BEP 9),
 *  or a v2 multihash (btmh, "1220" + 64 hex). */
const INFO_HASH = /^(?:urn:btih:(?:[0-9a-f]{40}|[a-z2-7]{32})|urn:btmh:1220[0-9a-f]{64})$/i;
function hasUsableInfoHash(link: string): boolean {
  const query = link.slice("magnet:?".length);
  return query.split("&").some((part) => {
    const [name, value = ""] = part.split("=");
    if (name.toLowerCase() !== "xt") return false;
    try {
      return INFO_HASH.test(decodeURIComponent(value));
    } catch {
      return false;                                   // a broken %-escape is not a usable link
    }
  });
}

const DRIVE = /^[A-Za-z]:[\\/]/;
const unquote = (s: string) => (s.length >= 2 && s.startsWith('"') && s.endsWith('"') ? s.slice(1, -1).trim() : s);
const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p;

/** Inline validation before any work: counts, repeated entries and duplicate file names. No grouping or sorting. */
export function checkCandidates(kind: InputKind, text: string): { items: string[]; problems: string[] } {
  const items: string[] = [];
  const problems: string[] = [];
  const seen = new Set<string>();
  const names = new Map<string, number>();
  let count = 0;
  String(text ?? "").split(/\r?\n/).forEach((raw, i) => {
    const n = i + 1;
    const line = unquote(raw.trim());
    if (line === "") return;
    count += 1;
    if (hasControl(line)) { problems.push(`Line ${n} contains a control character.`); return; }
    if (kind === "magnets") {
      if (line.length > 8192) { problems.push(`Line ${n} is too long for a magnet link.`); return; }
      if (!line.startsWith("magnet:?")) { problems.push(`Line ${n} is not a magnet link (it must start with magnet:?).`); return; }
      if (!hasUsableInfoHash(line)) {
        problems.push(`Line ${n} is not a usable magnet link: the part after urn:btih: must be the 40-character torrent hash `
          + "(0-9 and A-F) or its 32-character form (A-Z and 2-7). Copy the whole link again.");
        return;
      }
      const key = line.toLowerCase();
      if (seen.has(key)) { problems.push(`Line ${n} repeats a link already listed. Remove the repeated line.`); return; }
      seen.add(key);
      items.push(line);
      return;
    }
    if (line.length > 4096) { problems.push(`Line ${n}: the path is longer than 4096 characters.`); return; }
    if (!DRIVE.test(line)) { problems.push(`Line ${n}: use a full local path that starts with a drive letter, such as D:\\Movies\\film.mkv.`); return; }
    if (!VIDEO_EXTENSIONS.some((e) => line.toLowerCase().endsWith(e))) {
      problems.push(`Line ${n} is not a supported video file (${VIDEO_EXTENSIONS.join(" ")}).`);
      return;
    }
    const identity = pathIdentity(line);
    if (seen.has(identity)) { problems.push(`Line ${n} repeats a file already listed. Remove the repeated line.`); return; }
    seen.add(identity);
    const name = baseName(line);
    const earlier = names.get(name.toLowerCase());
    if (earlier !== undefined) {
      problems.push(`Line ${n} has the same file name as line ${earlier} (${name}). Comparison reports are matched by file `
        + "name, so give each version a different name in Explorer, or remove one.");
      return;
    }
    names.set(name.toLowerCase(), n);
    items.push(line);
  });
  const [min, max] = kind === "magnets" ? [2, 8] : [2, 50];
  if (count < min) {
    problems.push(kind === "magnets" ? `Add at least 2 magnet links to compare (you have ${count}).`
      : `Add at least 2 versions to compare (you have ${count}).`);
  }
  if (count > max) {
    problems.push(kind === "magnets" ? `Too many links: ${count}. Check at most 8 magnet links at a time.`
      : `Too many versions: ${count}. Compare at most 50 files at a time.`);
  }
  return { items, problems };
}

/** The RunRequest for the form. Overall sends no device/players/conditions; the evaluation player is derived from
 *  the alternatives checkbox ("all" with alternatives, otherwise the primary). */
export function buildRunRequest(form: CompareForm): RunRequest {
  const overall = form.results === "overall";
  const items = checkCandidates(form.inputKind, form.inputKind === "files" ? form.pathsText : form.magnetsText).items;
  const source = form.inputKind === "files"
    ? { paths: items }
    : { magnets: items.map((uri) => ({ uri, file_index: Object.hasOwn(form.fileChoices, uri) ? form.fileChoices[uri] : null })) };
  return {
    input_kind: form.inputKind, results: form.results, depth: form.inputKind === "magnets" ? "quick" : form.depth, ...source,
    device: overall ? null : form.device,
    primary_player: overall ? null : form.primaryPlayer,
    player: overall ? null : form.includeAlternatives ? "all" : form.primaryPlayer,
    include_alternatives: form.includeAlternatives,
    fs: overall ? null : form.fs, lang: form.lang, audio_output: overall ? null : form.audioOutput,
  };
}

/** Why the form cannot be planned yet, or null. */
export function formProblem(form: CompareForm, devices: DevicesDocument | null): string | null {
  const text = form.inputKind === "files" ? form.pathsText : form.magnetsText;
  if (checkCandidates(form.inputKind, text).problems.length > 0) {
    return form.inputKind === "files" ? "Add 2–50 distinct video files above first." : "Add 2–8 distinct magnet links above first.";
  }
  if (!LANG.test(form.lang)) return "Language must be a code such as eng or pt-BR.";
  if (form.results !== "overall") {
    const device = supportedDevices(devices).find((d) => d.id === form.device);
    if (!device) return "The device list is not available yet, so only Best overall can run.";
    if (!device.players.some((p) => p.id === form.primaryPlayer)) return "Choose a player for this device.";
  }
  return null;
}
