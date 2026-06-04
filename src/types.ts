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
  hdr_formats: ["Dolby Vision", "HDR10+", "HDR10", "HLG"],
  max_resolution: "3840 × 2160 (4K)",
  usb_notes: [
    "Use exFAT for files larger than 4 GB — FAT32 will silently refuse them.",
    "Profile 7 DV plays via USB; the TV uses BL+RPU and ignores the EL.",
    "TrueHD Atmos passthrough requires ARC/eARC on your receiver.",
    "H.265 10-bit is natively supported via USB on this TV.",
    "NTFS mounts read-only — fine for playback, but format exFAT for ease.",
  ],
};