export interface FactItem {
  label: string;
  value: string;
}

export interface ToolReport {
  name: string;
  status: "ok" | "partial" | "unavailable" | "error" | "skipped";
  headline: string;
  details: string[];
}

export interface VideoData {
  path: string;
  file: string;

  // Dolby Vision
  dv_profile: string;
  layer_variant?: string;
  layer_reason?: string;
  el: string;
  bl?: string;
  rpu?: string;
  dv_tool?: string;

  // Video stream
  hdr: string;
  bitrate_mbps: number;
  bit_depth?: number | null;
  color_range?: string;
  duration_min?: number;
  file_size_gb?: number;

  // Source
  source: string;

  // Audio
  audio?: string;
  audio_details?: string;
  audio_score?: number;

  // General quality
  score: number;
  confidence_score?: number;
  confidence_label?: string;

  // TV-aware scoring (Sony Bravia 8 Mark II)
  tv_score?: number;
  tv_label?: string;
  tv_dv_support?: string;
  tv_dv_note?: string;

  // USB compatibility
  usb_compatible?: boolean;
  usb_issues?: string[];
  usb_warnings?: string[];

  // TV heuristic
  tv_profile_supported?: string;
  tv_el_usable?: string;
  tv_container_compatibility?: string;
  tv_playback_note?: string;
  tv_playback?: string;

  // Summaries
  quick_summary?: string;
  insights?: string;
  recommendation?: string;

  batch_rank?: number;

  // Structured fact blocks
  signal_facts: FactItem[];
  media_facts: FactItem[];
  tool_reports: ToolReport[];

  // Magnet-mode marker — true when the analysis came from a partial
  // (head/tail) torrent download rather than a complete file.
  magnet_partial?: boolean;

  // HDR10+ confirmation from hdr10plus_tool (when available).
  //   true  — tool found ≥1 SEI metadata frame
  //   false — tool ran cleanly, no HDR10+ frames present
  //   null  — tool unavailable, errored, or scan was skipped
  hdr10_plus_confirmed?: boolean | null;
  hdr10_plus_frames?: number;
  hdr10_plus_status?: "confirmed" | "negative" | "unavailable" | "error";
}

export type MagnetVerdict = "good" | "bad" | "skip";

export interface MagnetFile {
  index: number;
  name: string;
  size_bytes: number;
  size_gb: number;
  ext: string;
  verdict: MagnetVerdict;
  reasons: string[];
  ffprobe_ok: boolean;
  analysis_path: string | null;
}

export interface MagnetTorrent {
  name: string | null;
  info_hash: string | null;
}

// ── Filename intelligence (from filename_intel.py) ──────────────────────────
export interface ReleaseIntel {
  platform: string | null;
  source: string | null;
  resolution: string | null;
  codec: string | null;
  container: string | null;
  audio_codec: string | null;
  channels: string | null;
  atmos: boolean;
  dts_x: boolean;
  hdr10: boolean;
  hdr10_plus: boolean;
  hdr_generic: boolean;
  hlg: boolean;
  dolby_vision: boolean;
  imax: boolean;
  hybrid: boolean;
  repack: boolean;
  proper: boolean;
  extended: boolean;
  directors_cut: boolean;
  remastered: boolean;
  open_matte: boolean;
  year: number | null;
  release_group: string | null;
  title: string | null;
}

// ── Verification flag (from verification.py) ────────────────────────────────
export interface VerificationFlag {
  code: string;
  severity: "info" | "warn" | "error";
  message: string;
  evidence: string;
}

export interface VerificationReport {
  flags: VerificationFlag[];
  error_count: number;
  warn_count: number;
  info_count: number;
  trust_score: number;
  summary: string;
}

// ── Enriched release record (analysis + intel + verification) ──────────────
export interface EnrichedRelease {
  name: string;
  magnet_uri: string | null;
  analysis: VideoData;
  intel: ReleaseIntel;
  verification: VerificationReport;
  badges: string[];
  composite_score?: number;
  score_reasons?: string[];
  release_recommendation?: string;
}

// ── Comparison matrix (side-by-side) ────────────────────────────────────────
export interface MatrixColumn {
  name: string;
  magnet_uri: string | null;
  badges: string[];
  composite_score: number;
  trust_score: number;
  tv_score: number;
  verification_summary: string;
  error_count: number;
  warn_count: number;
}

export interface MatrixRow {
  label: string;
  values: string[];
  highlight: number | null;
}

export interface ComparisonPayload {
  releases: EnrichedRelease[];
  comparison_matrix: {
    columns: MatrixColumn[];
    rows: MatrixRow[];
  };
  winner: {
    winner_index: number | null;
    winner_name: string | null;
    composite_score?: number;
    reasons: string[];
  };
}

// ── Per-magnet record inside a /compare-magnets/ job ────────────────────────
export interface PerMagnetRecord {
  index: number;
  magnet: string;
  status: "pending" | "running" | "done" | "error";
  error: string | null;
  torrent: MagnetTorrent | null;
  files: MagnetFile[];
  analyses: VideoData[];
  events: { msg: string; ts: number }[];
}

// Sony Bravia 8 Mark II specs (static, for UI display)
export interface TVSpec {
  name: string;
  usb_containers: string[];
  usb_video: string[];
  usb_audio: string[];
  usb_fs: string[];
  hdr_formats: string[];
  max_resolution: string;
  usb_notes: string[];
}

export const BRAVIA_8_II_SPEC: TVSpec = {
  name: "Sony Bravia 8 Mark II",
  usb_containers: ["MKV", "MP4", "TS", "M2TS"],
  usb_video: ["H.265 / HEVC", "H.264 / AVC", "VP9", "AV1"],
  usb_audio: [
    "TrueHD / Atmos", "DTS-HD MA", "DTS:X", "DTS",
    "Dolby Digital Plus (EAC3)", "Dolby Digital (AC3)", "AAC", "LPCM",
  ],
  usb_fs: ["exFAT (recommended)", "FAT32 (4 GB file cap)", "NTFS (read-only)"],
  // Sony TVs (Bravia 8 II included) do not support HDR10+ — files carrying
  // HDR10+ metadata fall back to their HDR10 base layer. We still detect
  // HDR10+ in the bitstream for informational purposes, but it must not be
  // shown as a supported display format here.
  hdr_formats: ["Dolby Vision", "HDR10", "HLG"],
  max_resolution: "3840 × 2160 (4K)",
  usb_notes: [
    "Use exFAT for files larger than 4 GB — FAT32 will silently refuse them.",
    "Profile 7 DV plays via USB; the TV uses BL+RPU and ignores the EL.",
    "TrueHD Atmos passthrough requires ARC/eARC on your receiver.",
    "H.265 10-bit is natively supported via USB on this TV.",
    "NTFS mounts read-only — fine for playback, but format exFAT for ease.",
  ],
};