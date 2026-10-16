// What the Suite UI reads from the Dashboard (videosuite 0.4.0 contract, fixtures in tests/fixtures/suite).
// The contract is additive: unknown extra keys are tolerated and never required.

export type Verdict = "PLAYS_WELL" | "PLAYS_DEGRADED" | "RISKY" | "WONT_PLAY";
export const VERDICTS: readonly Verdict[] = ["PLAYS_WELL", "PLAYS_DEGRADED", "RISKY", "WONT_PLAY"];

export type EvidenceWord = "official" | "owner" | "reviewed" | "forum" | "inferred" | "unknown";
export const EVIDENCE_WORDS: readonly EvidenceWord[] = ["official", "owner", "reviewed", "forum", "inferred", "unknown"];

export type VersionStatus = "ok" | "too_old" | "unavailable" | "error" | "unchecked";

export interface SettingsBlock {
  path: string | null;
  loaded: boolean;
  errors: string[];
  applied_count: number;
}

/** GET /suite/version (always HTTP 200). */
export interface VersionInfo {
  status: VersionStatus | string;
  tool_version: string | null;
  minimum: string;
  capabilities: string[];
  schemas: Record<string, string>;
  engine_tag: string | null;
  message: string;
  checked_at: number;
}

export interface OverrideInfo { rule: string; source: string; reason: string; confidence: string; }

export interface PlayerInfo {
  id: string;
  label: string;
  default: boolean;
  rule_counts: Record<string, number>;
  overrides?: OverrideInfo[];
}

export interface DeviceInfo {
  id: string;
  label: string;
  rules_version?: number;
  verified_by_owner?: boolean;
  players: PlayerInfo[];
  settings?: { usb_filesystem: string | null; audio_output: string | null };
}

/** GET /suite/devices: the CLI's devices document. */
export interface DevicesDocument {
  schema: string;
  tool_version: string;
  devices: DeviceInfo[];
  settings?: SettingsBlock;
}

/** GET /suite/settings (T15). */
export interface SettingsOverride {
  player: string | null;
  rule: string | null;
  source: string | null;
  reason: string | null;
  confidence: string | null;
}
export interface DeviceSettings {
  label: string;
  usb_filesystem: string | null;
  audio_output: string | null;
  overrides: SettingsOverride[];
}
export interface SuiteSettings {
  tool_version: string | null;
  supported: boolean;
  path: string | null;
  loaded: boolean;
  errors: string[];
  applied_count: number;
  devices: Record<string, DeviceSettings>;
}

export interface Issue {
  code: string;
  severity: string;      // veto | warn | risk | info (unknown values tolerated)
  message: string;
  confidence: string;    // one of the six words, or "n/a"
  rule?: string;
}

/** records[].playback["<device>/<player>"] */
export interface PlaybackResult {
  device: string;
  player: string;
  verdict: Verdict | string;
  score: number;
  score_range: number[];
  evidence: string;
  effective_hdr: string;
  effective_hdr_class?: string;
  effective_audio: string | null;
  issues: Issue[];
  hints: string[];
  usb_filesystem?: string | null;
  usb_filesystem_source?: string | null;
  audio_output?: string | null;
}

/** records[].best_player["<device>"] */
export interface BestPlayer {
  player: string;
  label: string;
  verdict: Verdict | string;
  score: number;
  default_player: string;
  switch: boolean;
  reason: string;
}

export interface RecordSource {
  path: string;
  magnet?: string | null;
  probe_coverage?: string;
  partial?: boolean;
  probed_bytes?: number;
  real_name?: string;
  caveats?: string[];
}

export interface ScoreRecord {
  id: string;
  source: RecordSource;
  playback: Record<string, PlaybackResult>;
  best_player?: Record<string, BestPlayer>;
}

export interface ScoreDocument {
  schema: string;
  tool_version: string;
  records: ScoreRecord[];
  settings?: SettingsBlock;
}

export interface QualityRow {
  id: string;
  label: string;
  rank: number;
  games: number;
  quality_score: number | null;
  margin?: number;
}

export interface RankRow {
  id: string;
  label: string;
  quality_score: number | null;
  quality_rank: number;
  playback_score: number;
  score_range?: number[];
  verdict: Verdict | string;
  evidence: string | null;
  effective_hdr: string | null;
  effective_audio: string | null;
  reasons: string[];
  why: string | null;
  tier?: number;
  device_score?: number | null;
  ranking_score?: number | null;
  device_rank?: number;
  neutral_quality_score?: number | null;
  neutral_quality_rank?: number;
  quality_note?: string;
}

export interface FormatCreditRemoved {
  a: string;
  b: string;
  rule: string;
  shown_as: string;
  measured: (number | null)[];
  winner: string | null;
}

export interface RankDevice {
  ranking: RankRow[];
  will_not_play: RankRow[];
  not_compared: RankRow[];
  crossing?: number | null;
  crossing_note: string | null;
  quality_basis?: string;
  format_credit_removed?: FormatCreditRemoved[];
}

export interface RankDocument {
  schema: string;
  tool_version: string;
  quality: QualityRow[];
  same_picture_groups: string[][];
  not_compared: string[];
  missing_pairs: string[][];
  weights: { quality: number; playback: number };
  devices: Record<string, RankDevice>;
  stale_reports?: { name1: string; name2: string; mode?: string; reason: string }[];
  legacy_reports?: { name1: string; name2: string }[];
  head_to_head_conflicts?: { winner: string; ranked_above: string }[];
  settings?: SettingsBlock;
}

export interface CompareDocument {
  schema: string;
  tool_version: string;
  mode: string;
  estimate: { pairs: number; minutes: number };
  shortlist: string[];
  dropped: { file: string; reason: string }[];
  pairs: { a: string; b: string; report: string | null }[];
  failed: number;
  settings?: SettingsBlock;
}

export interface JobEvent { msg: string; ts: number; }

/** GET /job/{id}. `status` is running | done | error | cancelled. Suite jobs put the CLI document in `results`. */
export interface Job {
  status: string;
  progress: string;
  current: string;
  total: number;
  results: unknown;
  error: string | null;
  error_kind: string | null;
  events: JobEvent[];
  kind: string;
  error_detail?: string;
  exit_code?: number;
}

export interface JobStarted { job_id: string; }

/** analysis.suite on a magnet analysis (magnet_suite.py). */
export type MagnetSuiteResult =
  | { status: "ok"; tool_version?: string; partial?: boolean; record?: ScoreRecord; best_player?: Record<string, BestPlayer> }
  | { status: "unavailable"; message?: string; tool_version?: string | null }
  | { status: "error"; error_kind?: string; message?: string }
  | { status: "skipped"; message?: string };

export interface DeviceOptions {
  device?: string;
  player?: string;
  fs?: "fat32" | "exfat" | "ntfs";
  lang?: string;
}
export interface ScoreRequest extends DeviceOptions { paths: string[]; }
export interface RankRequest extends DeviceOptions { paths: string[]; reports_dir: string; }
export interface CompareRequest { paths: string[]; out_dir: string; mode: "fast" | "accurate"; top: number; }
