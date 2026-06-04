import csv
import json
import os
import re
import shutil
import subprocess
import logging
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".ts", ".m2ts", ".hevc", ".h265")

MEDIAINFO_TIMEOUT_SECONDS = 90
FFPROBE_TIMEOUT_SECONDS   = 90
FFMPEG_TIMEOUT_SECONDS    = 120
DOVI_TIMEOUT_SECONDS      = 60
FFMPEG_SAMPLE_FRAMES      = 120

logger = logging.getLogger("video-analyzer.analysis")

# ── Sony Bravia 8 Mark II ────────────────────────────────────────────────────
BRAVIA_8_II: dict[str, Any] = {
    "name": "Sony Bravia 8 Mark II",
    "dv_support": {
        "8.1":  ("Yes",     "Best native DV — HDR10-compat base layer"),
        "8.4":  ("Yes",     "HLG-base DV — excellent on this TV"),
        "5":    ("Yes",     "Single-layer streaming DV — works great"),
        "8.2":  ("Yes",     "SDR-compat base DV — works"),
        "8.x":  ("Yes",     "Generic Profile 8 — likely works"),
        "7":    ("Partial", "Dual-layer; TV uses BL+RPU only — EL is not rendered. MEL variant may work in Just Player; FEL always falls back."),
        "4":    ("Limited", "Older dual-layer DV — unreliable"),
        "None": ("No",      "No Dolby Vision detected"),
    },
    "dv_tv_score": {
        "8.1": 35, "8.4": 32, "5": 30, "8.x": 27, "8.2": 25,
        "7-MEL": 30, "7-FEL": 22, "7": 26,
        "4": 15, "None": 0,
    },
    "usb_containers": {"MKV", "MP4", "TS", "M2TS"},
    "usb_video":      {"hevc", "h.265", "h265", "avc", "h.264", "h264", "vp9", "av1"},
    "usb_audio":      {"truehd", "dts-hd", "dtshd", "dts", "eac3", "ac3", "aac", "lpcm", "pcm"},
    "max_res":        (3840, 2160),
    "max_depth":      10,
    "usb_fs":         ["exFAT (recommended)", "FAT32 (4 GB file cap)", "NTFS (read-only)"],
    "hdr_formats":    ["Dolby Vision", "HDR10+", "HDR10", "HLG"],
    "usb_notes": [
        "Use exFAT for any file larger than 4 GB — FAT32 will refuse it.",
        "Profile 7 DV plays fine via USB; the TV uses BL+RPU and ignores EL.",
        "TrueHD Atmos passthrough requires an ARC/eARC-capable receiver.",
        "H.265 10-bit is natively supported via USB.",
        "NTFS mounts read-only on this TV — fine for playback.",
    ],
}


# ── Utility helpers ──────────────────────────────────────────────────────────

def resolve_tool(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def command_failed(result: subprocess.CompletedProcess[str] | None) -> bool:
    return result is None or result.returncode != 0


def make_temp_path(suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    return handle.name


def remove_if_exists(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace(",", "").strip()
    if not cleaned:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_fraction(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "/" in text:
        numerator_text, denominator_text = text.split("/", 1)
        num = coerce_float(numerator_text)
        den = coerce_float(denominator_text)
        if num is None or den is None or den == 0:
            return None
        return num / den
    return coerce_float(text)


def format_number(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "Unknown"
    return f"{value:.{decimals}f}"


def format_rate(value: float | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_size_gb(size_bytes: Any) -> str:
    value = coerce_float(size_bytes)
    if value is None or value <= 0:
        return "Unknown"
    return f"{value / 1_000_000_000:.2f} GB"


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def lower_text(value: Any) -> str:
    return normalize_text(value).lower()


# ── MediaInfo / FFprobe helpers ──────────────────────────────────────────────

def extract_mediainfo_track(data: dict[str, Any] | None, track_type: str) -> dict[str, Any] | None:
    if not data:
        return None
    try:
        for track in data["media"]["track"]:
            if track.get("@type") == track_type:
                return track
    except (KeyError, TypeError):
        return None
    return None


def get_mediainfo_tracks(data: dict[str, Any] | None, track_type: str) -> list[dict[str, Any]]:
    if not data:
        return []
    try:
        return [t for t in data["media"]["track"] if t.get("@type") == track_type]
    except (KeyError, TypeError):
        return []


def get_ffprobe_streams(data: dict[str, Any] | None, codec_type: str) -> list[dict[str, Any]]:
    if not data:
        return []
    return [s for s in data.get("streams", []) if s.get("codec_type") == codec_type]


def run_mediainfo(file_path: str) -> dict[str, Any] | None:
    mediainfo_bin = resolve_tool("mediainfo", "MediaInfo")
    if not mediainfo_bin:
        return None
    result = run_command([mediainfo_bin, "--Output=JSON", file_path], timeout=MEDIAINFO_TIMEOUT_SECONDS)
    if result is None or command_failed(result):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    

def run_ffprobe(file_path: str) -> dict[str, Any] | None:
    ffprobe_bin = resolve_tool("ffprobe")
    if not ffprobe_bin:
        return None
    result = run_command(
        [ffprobe_bin, "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", file_path],
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    if result is None or command_failed(result):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def probe_metadata(file_path: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        mi_future = executor.submit(run_mediainfo, file_path)
        ff_future = executor.submit(run_ffprobe, file_path)
        return mi_future.result(), ff_future.result()


# ── Container / stream facts ─────────────────────────────────────────────────

def get_container_name(general_track: dict[str, Any] | None, ffprobe_data: dict[str, Any] | None) -> str:
    if general_track and general_track.get("Format"):
        return normalize_text(general_track["Format"])
    if ffprobe_data:
        name = normalize_text(ffprobe_data.get("format", {}).get("format_long_name", ""))
        if name:
            return name
    return "Unknown"


def get_container_short_name(container_name: str) -> str:
    text = lower_text(container_name)
    if "matroska" in text: return "MKV"
    if "mp4" in text or "mpeg-4" in text: return "MP4"
    if "mpeg-ts" in text or "transport stream" in text: return "TS"
    if "m2ts" in text or "bdav" in text: return "M2TS"
    if "hevc" in text: return "RAW HEVC"
    return "Unknown"


def get_resolution(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    width = height = None
    if ffprobe_video:
        width  = ffprobe_video.get("width")
        height = ffprobe_video.get("height")
    if not width and video_track:  width  = video_track.get("Width")
    if not height and video_track: height = video_track.get("Height")
    wv = coerce_float(width)
    hv = coerce_float(height)
    if wv is None or hv is None:
        return "Unknown"
    return f"{int(wv)}x{int(hv)}"


def get_frame_rate(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    frame_rate = None
    if ffprobe_video:
        frame_rate = (parse_fraction(ffprobe_video.get("avg_frame_rate"))
                      or parse_fraction(ffprobe_video.get("r_frame_rate")))
    if frame_rate is None and video_track:
        frame_rate = coerce_float(video_track.get("FrameRate"))
    return f"{format_rate(frame_rate)} fps" if frame_rate is not None else "Unknown"


def get_video_codec(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    if video_track:
        codec   = normalize_text(video_track.get("Format", ""))
        profile = normalize_text(video_track.get("Format_Profile", ""))
        if codec and profile: return f"{codec} {profile}"
        if codec: return codec
    if ffprobe_video:
        codec   = normalize_text(ffprobe_video.get("codec_long_name", ffprobe_video.get("codec_name", "")))
        profile = normalize_text(ffprobe_video.get("profile", ""))
        if codec and profile: return f"{codec} {profile}"
        if codec: return codec
    return "Unknown"


def get_pixel_format(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    if ffprobe_video and ffprobe_video.get("pix_fmt"):
        return str(ffprobe_video["pix_fmt"])
    if video_track:
        bit_depth = normalize_text(video_track.get("BitDepth", ""))
        chroma    = normalize_text(video_track.get("ChromaSubsampling", ""))
        if chroma and bit_depth: return f"{chroma}, {bit_depth}-bit"
        if chroma: return chroma
    return "Unknown"


def get_bit_depth(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> int | None:
    if ffprobe_video:
        raw   = ffprobe_video.get("bits_per_raw_sample")
        value = coerce_float(raw)
        if value is not None and value > 0:
            return int(value)
        pix_fmt = normalize_text(ffprobe_video.get("pix_fmt", ""))
        match   = re.search(r"(\d{2})", pix_fmt)
        if match:
            maybe = int(match.group(1))
            if maybe in {8, 9, 10, 12, 14, 16}:
                return maybe
    if video_track:
        value = coerce_float(video_track.get("BitDepth"))
        if value is not None:
            return int(value)
    return None


def get_color_range(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    if ffprobe_video:
        v = normalize_text(ffprobe_video.get("color_range", ""))
        if v: return v
    if video_track:
        v = normalize_text(video_track.get("colour_range", video_track.get("ColorRange", "")))
        if v: return v
    return "Unknown"


def get_hdr_summary(video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None) -> str:
    if video_track:
        parts = [
            normalize_text(video_track.get("HDR_Format", "")),
            normalize_text(video_track.get("HDR_Format_Profile", "")),
            normalize_text(video_track.get("HDR_Format_Compatibility", "")),
        ]
        summary = " | ".join(p for p in parts if p)
        if summary:
            return summary
    if not ffprobe_video:
        return ""
    parts = []
    if get_ffprobe_dovi_side_data(ffprobe_video):
        parts.append("Dolby Vision")
    transfer  = normalize_text(ffprobe_video.get("color_transfer", ""))
    primaries = normalize_text(ffprobe_video.get("color_primaries", ""))
    if transfer:  parts.append(transfer)
    if primaries: parts.append(primaries)
    return " | ".join(parts)


def get_mastering_display(video_track: dict[str, Any] | None) -> str:
    if not video_track:
        return "Unknown"
    return normalize_text(video_track.get("MasteringDisplay_Luminance", "")) or "Unknown"


def get_cll_summary(video_track: dict[str, Any] | None) -> str:
    if not video_track:
        return "Unknown"
    max_cll  = normalize_text(video_track.get("MaxCLL", ""))
    max_fall = normalize_text(video_track.get("MaxFALL", ""))
    if max_cll and max_fall: return f"MaxCLL {max_cll} nits / MaxFALL {max_fall} nits"
    if max_cll:  return f"MaxCLL {max_cll} nits"
    if max_fall: return f"MaxFALL {max_fall} nits"
    return "Unknown"


def get_duration_minutes(general_track: dict[str, Any] | None, ffprobe_data: dict[str, Any] | None) -> float:
    duration = None
    if general_track:
        duration = coerce_float(general_track.get("Duration"))
    if duration is None and ffprobe_data:
        duration = coerce_float(ffprobe_data.get("format", {}).get("duration"))
    if duration is None:
        return 0.0
    return round(duration / 60, 1)


def get_bitrate_mbps(
    video_track: dict[str, Any] | None,
    ffprobe_video: dict[str, Any] | None,
    ffprobe_data: dict[str, Any] | None = None,   # ← FIX: container fallback
    file_size_gb: float | None = None,
    duration_min: float | None = None,
) -> float:
    candidates: list[Any] = []
    if video_track:
        candidates.extend([video_track.get("BitRate"), video_track.get("BitRate_Nominal")])
    if ffprobe_video:
        candidates.append(ffprobe_video.get("bit_rate"))
        candidates.append(ffprobe_video.get("tags", {}).get("BPS"))
    for candidate in candidates:
        value = coerce_float(candidate)
        if value and value > 1_000:
            return round(value / 1_000_000, 2)
    # Container-level bitrate as last resort (~5 % over due to audio, still better than 0)
    if ffprobe_data:
        value = coerce_float(ffprobe_data.get("format", {}).get("bit_rate"))
        if value and value > 1_000:
            return round(value / 1_000_000, 2)
    # NEW: derive from file size + duration as last resort
    if file_size_gb and duration_min and duration_min > 0:
        # rough estimate: assume ~85% of file is video
        estimated = (file_size_gb * 1_000_000_000 * 0.85 * 8) / (duration_min * 60) / 1_000_000
        if estimated > 0.5:
            return round(estimated, 2)
    return 0.0


def get_file_size_gb(
    general_track: dict[str, Any] | None,
    ffprobe_data: dict[str, Any] | None,
) -> float:
    if general_track:
        v = coerce_float(general_track.get("FileSize"))
        if v and v > 0:
            return round(v / 1_000_000_000, 2)
    if ffprobe_data:
        v = coerce_float(ffprobe_data.get("format", {}).get("size"))
        if v and v > 0:
            return round(v / 1_000_000_000, 2)
    return 0.0


# ── Dolby Vision helpers ─────────────────────────────────────────────────────

def get_ffprobe_dovi_side_data(ffprobe_video: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ffprobe_video:
        return None
    for side_data in ffprobe_video.get("side_data_list", []):
        if side_data.get("side_data_type") == "DOVI configuration record":
            return side_data
    return None


def parse_profile(text: str) -> str:
    lowered = text.lower()
    match = re.search(r"dvhe\.(\d{2})", lowered)
    if match: return str(int(match.group(1)))
    match = re.search(r"profile[:\s]+(\d+)", lowered)
    if match: return str(int(match.group(1)))
    match = re.search(r"profile\s*(\d+)", lowered)
    if match: return str(int(match.group(1)))
    return "None"


def build_profile_label(base_profile: str, video_track: dict[str, Any] | None,
                         ffprobe_video: dict[str, Any] | None) -> str:
    if base_profile == "None":
        return "None"
    compatibility_text = ""
    if video_track:
        compatibility_text = (
            f"{normalize_text(video_track.get('HDR_Format_Compatibility', ''))} "
            f"{normalize_text(video_track.get('HDR_Format_Profile', ''))}"
        ).lower()
    side_data      = get_ffprobe_dovi_side_data(ffprobe_video)
    compatibility_id = side_data.get("dv_bl_signal_compatibility_id") if side_data else None
    if base_profile == "8":
        if compatibility_id == 4 or "hlg" in compatibility_text: return "8.4"
        if compatibility_id == 2 or "sdr" in compatibility_text: return "8.2"
        if compatibility_id == 1 or "hdr10" in compatibility_text: return "8.1"
        return "8.x"
    return base_profile


def detect_layer_variant(filepath: str, base_profile: str, el: str,
                          video_track: dict[str, Any] | None,
                          ffprobe_video: dict[str, Any] | None) -> tuple[str, str]:
    if base_profile != "7" or el != "Yes":
        return "Single-layer", "Not a profile 7 dual-layer stream."
    name      = os.path.basename(filepath).lower()
    hdr_text  = lower_text(get_hdr_summary(video_track, ffprobe_video))
    dovi_side = get_ffprobe_dovi_side_data(ffprobe_video)
    if "fel" in name and "mel" not in name: return "FEL", "Filename hint matched FEL."
    if "mel" in name and "fel" not in name: return "MEL", "Filename hint matched MEL."
    if dovi_side:
        compat_id = dovi_side.get("dv_bl_signal_compatibility_id")
        if compat_id is not None and ("profile 7" in hdr_text or "dolby vision" in hdr_text):
            return (
                "Dual-layer (FEL/MEL unknown)",
                f"Profile 7 dual-layer; compat id={compat_id} does not prove FEL vs MEL.",
            )
    bitrate_guess = estimate_layer_variant_from_bitrate(filepath)
    if bitrate_guess == "FEL":
        return "FEL", "Bitrate-ratio heuristic suggests FEL (EL >= 15% of BL)."
    if bitrate_guess == "MEL":
        return "MEL", "Bitrate-ratio heuristic suggests MEL (EL < 15% of BL)."
    return "Dual-layer (FEL/MEL unknown)", "Dual-layer DV detected; metadata cannot prove FEL vs MEL."

def estimate_layer_variant_from_bitrate(file_path: str) -> str | None:
    """
    Heuristic: if two video streams exist, compare their bitrates.
    FEL's EL is typically 30-60% of BL bitrate.
    MEL's EL is tiny (under 15%).
    Returns 'FEL', 'MEL', or None if undetermined.
    """
    ffprobe_bin = resolve_tool("ffprobe")
    if not ffprobe_bin:
        return None
    result = run_command(
        [ffprobe_bin, "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=index,bit_rate",
         "-print_format", "json", file_path],
        timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    if result is None or command_failed(result):
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
        if len(streams) >= 2:
            br0 = coerce_float(streams[0].get("bit_rate")) or 0
            br1 = coerce_float(streams[1].get("bit_rate")) or 0
            if br0 > 0 and br1 > 0:
                ratio = min(br0, br1) / max(br0, br1)
                return "FEL" if ratio > 0.15 else "MEL"
    except (json.JSONDecodeError, KeyError, ZeroDivisionError):
        pass
    return None

# ── Audio helpers ────────────────────────────────────────────────────────────

def get_audio_quality_score(label: str, details: str) -> int:
    text = f"{label} {details}".lower()
    if "truehd" in text:              return 5
    if "eac3" in text or "ddp" in text or "dolby digital plus" in text: return 10
    if "dts-hd" in text or "dts x" in text or "dtsx" in text: return 7
    if "dts" in text:                 return 6
    if "ac3" in text or "ac-3" in text or "dolby digital" in text: return 5
    if "pcm" in text or "lpcm" in text: return 6
    if "aac" in text:                 return 3
    if "opus" in text:                return 4
    return 2 if text.strip() else 0


def get_primary_audio_summary(
    mediainfo_audio_tracks: list[dict[str, Any]],
    ffprobe_audio_streams: list[dict[str, Any]],
) -> tuple[str, str, int]:
    if mediainfo_audio_tracks:
        primary = next(
            (t for t in mediainfo_audio_tracks if lower_text(t.get("Default", "")) == "yes"),
            mediainfo_audio_tracks[0],
        )
        title      = normalize_text(primary.get("Title", ""))
        commercial = normalize_text(primary.get("Format_Commercial_IfAny", primary.get("Format", "")))
        bitrate    = coerce_float(primary.get("BitRate"))
        channels   = normalize_text(primary.get("ChannelLayout", primary.get("ChannelPositions", "")))
        bitrate_text = f"{round(bitrate / 1000):.0f} kbps" if bitrate else "unknown bitrate"
        label   = title or commercial or "Audio track"
        details = commercial or label
        if channels:      details = f"{details}, {channels}"
        details = f"{details}, {bitrate_text}"
        return label, details, get_audio_quality_score(label, details)
    if ffprobe_audio_streams:
        primary      = ffprobe_audio_streams[0]
        codec        = normalize_text(primary.get("codec_name", "audio")).upper()
        bitrate      = coerce_float(primary.get("bit_rate"))
        channels     = normalize_text(primary.get("channel_layout", ""))
        bitrate_text = f"{round(bitrate / 1000):.0f} kbps" if bitrate else "unknown bitrate"
        details = codec
        if channels: details = f"{details}, {channels}"
        details = f"{details}, {bitrate_text}"
        return codec, details, get_audio_quality_score(codec, details)
    return "Unknown", "Unknown audio", 0


# ── Source heuristics ────────────────────────────────────────────────────────

def guess_source(file_name: str, bitrate_mbps: float, base_profile: str, el: str) -> str:
    name = file_name.lower()
    if "remux" in name:                                            return "REMUX"
    if "uhd" in name and ("bluray" in name or "blu-ray" in name):  return "UHD BluRay"
    if "bluray" in name or "blu-ray" in name or "bdremux" in name or "bdrip" in name:
        return "BluRay Encode"
    if any(x in name for x in ("amzn", "amazon", "nf", "netflix",
                                "dsnp", "disney", "hmax", "hulu",
                                "atvp", "appletv", "pcok", "peacock",
                                "pmtp", "paramount", "web-dl", "webrip")):
        return "WEB-DL"
    if base_profile == "7" and el == "Yes": return "Likely BluRay"
    if bitrate_mbps > 60:                   return "Likely REMUX"
    if bitrate_mbps > 30:                   return "High Encode"
    if bitrate_mbps > 15:                   return "WEB/Encode"
    return "WEB/Compressed"


# ── TV-aware scoring ─────────────────────────────────────────────────────────

def score_for_tv(
    base_profile: str,
    bitrate_mbps: float,
    bit_depth: int | None,
    audio_score: int,
    container_short: str,
    source: str,
    layer_variant: str = "",
    hdr_summary: str = "",
) -> tuple[int, str]:
    """Quality score calibrated for Sony Bravia 8 Mark II direct playback.

    Key difference vs score_video: profile 7 scores 28 (not 42) because the
    TV cannot render the EL — source quality is still good, just no EL bonus.
    """
    if base_profile == "7":
        lv = layer_variant.lower()
        if lv.startswith("mel"):
            score = BRAVIA_8_II["dv_tv_score"].get("7-MEL", 30)
        elif lv.startswith("fel"):
            score = BRAVIA_8_II["dv_tv_score"].get("7-FEL", 22)
        else:
            score = BRAVIA_8_II["dv_tv_score"].get("7", 26)
    else:
        score = BRAVIA_8_II["dv_tv_score"].get(base_profile) or 10

    if bitrate_mbps > 70:    score += 18
    elif bitrate_mbps > 45:  score += 15
    elif bitrate_mbps > 30:  score += 11
    elif bitrate_mbps > 20:  score += 7
    elif bitrate_mbps > 12:  score += 3
    else:                    score -= 4

    if bit_depth is not None:
        if bit_depth >= 10:  score += 10
        elif bit_depth == 8: score -= 12

    score += min(audio_score, 10)

    if container_short == "MP4":    score += 6
    elif container_short == "MKV":  score += 3
    elif container_short in {"M2TS", "TS"}: score += 2

    source_bonus = {
        "REMUX": 10, "UHD BluRay": 8, "Likely REMUX": 7,
        "BluRay Encode": 5, "WEB-DL": 2,
    }
    score += source_bonus.get(source, 0)
    score = int(clamp(score, 0, 100))

    if score >= 80:   label = "Excellent"
    elif score >= 65: label = "Very Good"
    elif score >= 50: label = "Good"
    elif score >= 35: label = "Acceptable"
    else:             label = "Poor"

    return score, label


# ── USB compatibility ────────────────────────────────────────────────────────

def check_usb_compatibility(
    container_short: str,
    video_codec: str,
    audio_details: str,
    bit_depth: int | None,
    resolution: str,
    file_size_gb: float,
) -> dict[str, Any]:
    issues:   list[str] = []
    warnings: list[str] = []

    if container_short not in BRAVIA_8_II["usb_containers"]:
        issues.append(f"Container '{container_short}' may not be supported via USB on this TV.")

    codec_lower = video_codec.lower()
    if not any(c in codec_lower for c in BRAVIA_8_II["usb_video"]):
        issues.append(f"Video codec '{video_codec}' may not play via USB.")

    audio_lower = audio_details.lower()
    if not any(c in audio_lower for c in BRAVIA_8_II["usb_audio"]):
        warnings.append("Primary audio codec may have limited USB support — verify before copying.")

    if bit_depth and bit_depth > BRAVIA_8_II["max_depth"]:
        issues.append(f"{bit_depth}-bit video exceeds the 10-bit USB maximum on this TV.")

    if container_short == "MKV" and "dolby vision" in video_codec.lower():
        warnings.append(
            "DV in MKV can lose DV box atoms during playback — "
            "MP4 (via mp4muxer) is the safer container for Dolby Vision on this TV."
        )

    if "truehd" in audio_lower:
        warnings.append(
            "TrueHD/Atmos does not decode from USB on this TV — "
            "playback falls back to the embedded AC3 core (DD 5.1). "
            "Re-encode audio to E-AC3 Atmos for spatial audio via USB."
        )
    if "dts-hd" in audio_lower or "dtshd" in audio_lower:
        warnings.append(
            "DTS-HD MA USB support is inconsistent on this TV — "
            "may fall back to DTS core. Verify playback before copying."
        )

    if "x" in resolution:
        try:
            w, h = (int(v) for v in resolution.split("x", 1))
            mw, mh = BRAVIA_8_II["max_res"]
            if w > mw or h > mh:
                issues.append(f"Resolution {resolution} exceeds the 4K maximum ({mw}x{mh}).")
        except ValueError:
            pass

    if file_size_gb > 3.9:
        warnings.append(
            f"File is {file_size_gb:.1f} GB — use exFAT on your USB drive (FAT32 caps at ~4 GB)."
        )
    else:
        warnings.append("File fits on FAT32, but exFAT is still recommended for compatibility.")

    return {
        "compatible": len(issues) == 0,
        "issues":     issues,
        "warnings":   warnings,
    }


# ── Container compatibility (for TV heuristic) ───────────────────────────────

def get_container_compatibility(container_short: str) -> str:
    if container_short in {"MKV", "MP4"}:  return "Good"
    if container_short in {"M2TS", "TS"}:  return "Okay"
    if container_short == "RAW HEVC":      return "Poor"
    return "Unknown"


def tv_compatibility_heuristic(
    base_profile: str, profile_label: str, el: str, layer_variant: str,
    container_short: str, video_codec: str,
) -> dict[str, str]:
    codec_text  = lower_text(video_codec)
    container_fit = get_container_compatibility(container_short)

    if "hevc" not in codec_text and "h.265" not in codec_text:
        return {
            "profile_supported": "No", "el_usable": "No",
            "container_compatibility": container_fit,
            "note": "Primary video is not HEVC, so DV playback is not a sensible target.",
        }

    dv_entry     = BRAVIA_8_II["dv_support"].get(base_profile, ("Unknown", ""))
    profile_supported = dv_entry[0]

    # EL is never usable on this TV
    el_usable = "No"

    note = dv_entry[1] if len(dv_entry) > 1 else ""

    if base_profile == "7" and el == "Yes":
        note += " EL present in file but not rendered by this TV."

    return {
        "profile_supported":     profile_supported,
        "el_usable":             el_usable,
        "container_compatibility": container_fit,
        "note":                  note,
    }


# ── General quality scoring ──────────────────────────────────────────────────

def score_video(
    base_profile: str, layer_variant: str, bitrate_mbps: float, source: str,
    bit_depth: int | None, audio_score: int, container_short: str,
    tv_support: str, el: str,
    hdr_summary: str = "",
) -> int:
    score = 0
    profile_scores = {"7": 42, "8.1": 30, "5": 26, "8.4": 22, "8.2": 18, "4": 14, "8.x": 20}
    score += profile_scores.get(base_profile, 0)

    layer_text = layer_variant.lower()
    if layer_text.startswith("fel"):           score += 18
    elif layer_text.startswith("mel"):         score += 12
    elif "dual-layer" in layer_text and base_profile == "7": score += 8

    if bitrate_mbps > 70:    score += 18
    elif bitrate_mbps > 45:  score += 15
    elif bitrate_mbps > 30:  score += 11
    elif bitrate_mbps > 20:  score += 7
    elif bitrate_mbps > 12:  score += 3
    else:                    score -= 4

    if bit_depth is not None:
        if bit_depth >= 10:  score += 10
        elif bit_depth == 8: score -= 12

    score += min(audio_score, 10)

    if container_short == "MP4":    score += 6
    elif container_short == "MKV":  score += 3
    elif container_short in {"M2TS", "TS"}: score += 2

    source_scores = {"REMUX": 10, "UHD BluRay": 8, "BluRay Encode": 5, "WEB-DL": 2}
    score += source_scores.get(source, 0)

    if tv_support == "Yes":     score += 5
    elif tv_support == "Partial": score += 2
    if el == "Yes" and base_profile == "7": score += 4

    return int(clamp(score, 0, 100))


def confidence_label(score: int) -> str:
    if score >= 85: return "Very High"
    if score >= 70: return "High"
    if score >= 50: return "Medium"
    return "Low"


def score_confidence(
    mediainfo_data: dict[str, Any] | None,
    ffprobe_data: dict[str, Any] | None,
    video_track: dict[str, Any] | None,
    ffprobe_video: dict[str, Any] | None,
    dovi_scan: dict[str, Any],
    layer_variant: str,
    tv_support: str,
) -> int:
    score = 35
    if mediainfo_data: score += 12
    if ffprobe_data:   score += 12
    if video_track:    score += 6
    if ffprobe_video:  score += 6

    status = dovi_scan.get("status")
    if status in {"partial", "ok"}:    score += 18
    elif status == "error":            score -= 6
    elif status == "skipped":          score -= 8   # FIX: honest penalty for fast mode
    else:                              score -= 4

    if layer_variant != "Single-layer" and "unknown" not in layer_variant.lower():
        score += 6
    elif "unknown" in layer_variant.lower():
        score -= 2

    if tv_support == "Yes":     score += 4
    elif tv_support == "Partial": score += 2

    if mediainfo_data and ffprobe_data:
        mi_video = extract_mediainfo_track(mediainfo_data, "Video")
        if mi_video and ffprobe_video:
            mi_res = f"{normalize_text(mi_video.get('Width', ''))}x{normalize_text(mi_video.get('Height', ''))}"
            ff_res = f"{ffprobe_video.get('width', '')}x{ffprobe_video.get('height', '')}"
            if "x" in mi_res and "x" in ff_res and mi_res == ff_res:
                score += 2

    return int(clamp(score, 0, 100))


# ── Dolby Vision scan ────────────────────────────────────────────────────────

def should_run_dovi_scan(video_track: dict[str, Any] | None,
                          ffprobe_video: dict[str, Any] | None) -> tuple[bool, str]:
    codec_names = {
        lower_text(video_track.get("Format", "")) if video_track else "",
        lower_text(ffprobe_video.get("codec_name", "")) if ffprobe_video else "",
        lower_text(ffprobe_video.get("codec_long_name", "")) if ffprobe_video else "",
    }
    if "hevc" not in codec_names and "h.265" not in codec_names and "h265" not in codec_names:
        return False, "dovi_tool skipped — primary video stream is not HEVC."
    if get_ffprobe_dovi_side_data(ffprobe_video):
        return True, ""
    hdr_summary = get_hdr_summary(video_track, ffprobe_video).lower()
    if "dolby vision" not in hdr_summary:
        return False, "dovi_tool skipped — Dolby Vision not signaled by MediaInfo or ffprobe."
    return True, ""


def create_ffmpeg_video_sample(file_path: str,
                                frame_limit: int) -> tuple[str | None, subprocess.CompletedProcess[str] | None]:
    ffmpeg_bin = resolve_tool("ffmpeg")
    if not ffmpeg_bin:
        return None, None
    sample_path = make_temp_path(".hevc")
    result = None
    for args_prefix in (
        [ffmpeg_bin, "-y", "-hwaccel", "cuda", "-v", "error"],
        [ffmpeg_bin, "-y", "-v", "error"],
    ):
        result = run_command(
            args_prefix + ["-i", file_path, "-map", "0:v:0",
             "-an", "-sn", "-dn", "-c", "copy",
             "-frames:v", str(frame_limit), "-f", "hevc", sample_path],
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        if not command_failed(result):
            return sample_path, result
    remove_if_exists(sample_path)
    return None, result


def parse_dovi_summary(output: str) -> dict[str, Any] | None:
    if "Summary:" not in output:
        return None
    key_map = {
        "Frames":                       "frames",
        "Profile":                      "profile",
        "DM version":                   "dm_version",
        "Scene/shot count":             "scene_count",
        "RPU mastering display":        "rpu_mastering_display",
        "RPU content light level (L1)": "rpu_content_light_level",
        "L6 metadata":                  "l6_metadata",
        "L5 offsets":                   "l5_offsets",
        "L2 trims":                     "l2_trims",
        "L8 trims":                     "l8_trims",
        "L9 MDP":                       "l9_mdp",
    }
    parsed: dict[str, Any] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line == "Summary:" or ":" not in line:
            continue
        label, value = line.split(":", 1)
        mapped_key = key_map.get(label.strip())
        if not mapped_key:
            continue
        text_value = value.strip()
        if mapped_key in {"frames", "scene_count"}:
            number = coerce_float(text_value)
            parsed[mapped_key] = int(number) if number is not None else text_value
        else:
            parsed[mapped_key] = text_value
    return parsed or None


def run_dovi_partial_scan(
    file_path: str,
    video_track: dict[str, Any] | None,
    ffprobe_video: dict[str, Any] | None,
) -> dict[str, Any]:
    ffmpeg_bin = resolve_tool("ffmpeg")
    dovi_bin   = resolve_tool("dovi_tool")
    if not ffmpeg_bin or not dovi_bin:
        return {"status": "unavailable",
                "headline": "ffmpeg or dovi_tool is missing.",
                "details": [], "summary": {}}

    should_run, reason = should_run_dovi_scan(video_track, ffprobe_video)
    if not should_run:
        return {"status": "unavailable", "headline": reason, "details": [], "summary": {}}

    sample_path = rpu_path = None
    try:
        sample_path, sample_result = create_ffmpeg_video_sample(file_path, FFMPEG_SAMPLE_FRAMES)
        if not sample_path:
            stderr = sample_result.stderr.strip() if sample_result else ""
            return {"status": "error",
                    "headline": "FFmpeg could not prepare the DV sample stream.",
                    "details": [stderr or "FFmpeg extraction failed."], "summary": {}}

        rpu_path = make_temp_path(".rpu")
        extract_result = run_command(
            [dovi_bin, "extract-rpu", "-i", sample_path, "-o", rpu_path,
             "-l", str(FFMPEG_SAMPLE_FRAMES)],
            timeout=DOVI_TIMEOUT_SECONDS,
        )
        if command_failed(extract_result):
            stderr = extract_result.stderr.strip() if extract_result else ""
            return {"status": "error",
                    "headline": "dovi_tool could not extract RPU data.",
                    "details": [stderr or "RPU extraction failed."], "summary": {}}

        info_result = run_command([dovi_bin, "info", "-s", "-i", rpu_path],
                                   timeout=DOVI_TIMEOUT_SECONDS)
        if info_result is None or command_failed(info_result):
            stderr = info_result.stderr.strip() if info_result else ""
            return {"status": "error",
                    "headline": "dovi_tool could not summarize RPU data.",
                    "details": [stderr or "RPU info failed."], "summary": {}}

        summary = parse_dovi_summary(info_result.stdout) or {}
        frames  = summary.get("frames", FFMPEG_SAMPLE_FRAMES)
        details = [f"Stopped after {frames} frames by design."]
        if summary.get("dm_version"):    details.append(f"DM version {summary['dm_version']}")
        if summary.get("scene_count"):   details.append(f"Scene refreshes in sample: {summary['scene_count']}")
        if summary.get("rpu_content_light_level"): details.append(summary["rpu_content_light_level"])

        return {"status": "partial",
                "headline": f"Partial DV RPU scan completed on the first {frames} frames.",
                "details": details, "summary": summary}
    finally:
        remove_if_exists(sample_path)
        remove_if_exists(rpu_path)


# ── DV inspection ────────────────────────────────────────────────────────────

def flag_text(value: Any) -> str:
    if value is None: return "Unknown"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "true", "1"}:  return "Yes"
        if lowered in {"no", "false", "0"}:  return "No"
        return value
    return "Yes" if bool(value) else "No"


def inspect_dolby_vision(
    filepath: str,
    video_track: dict[str, Any] | None,
    ffprobe_video: dict[str, Any] | None,
    dovi_scan: dict[str, Any],
) -> dict[str, str]:
    hdr_summary  = get_hdr_summary(video_track, ffprobe_video)
    base_profile = parse_profile(hdr_summary)
    bl = el = rpu = "Unknown"

    if "dolby vision" in hdr_summary.lower():
        bl = rpu = "Yes"
        el = "No"

    if video_track:
        settings = normalize_text(video_track.get("HDR_Format_Settings", "")).upper()
        if "BL+RPU" in settings:     bl = rpu = "Yes"; el = "No"
        elif "BL+EL+RPU" in settings: bl = rpu = el = "Yes"

    side_data = get_ffprobe_dovi_side_data(ffprobe_video)
    if side_data:
        base_profile = normalize_text(side_data.get("dv_profile", base_profile))
        bl  = flag_text(side_data.get("bl_present_flag"))
        el  = flag_text(side_data.get("el_present_flag"))
        rpu = flag_text(side_data.get("rpu_present_flag"))

    dovi_summary = dovi_scan.get("summary", {})
    if dovi_summary.get("profile"):
        base_profile = normalize_text(dovi_summary["profile"])

    profile_label = build_profile_label(base_profile, video_track, ffprobe_video)
    tool_names: list[str] = []
    if video_track:  tool_names.append("mediainfo")
    if side_data:    tool_names.append("ffprobe")
    if dovi_scan.get("status") in {"partial", "ok"}:
        tool_names.extend(["ffmpeg", "dovi_tool"])

    layer_variant, layer_reason = detect_layer_variant(
        filepath, base_profile, el, video_track, ffprobe_video
    )

    return {
        "base_profile":  base_profile,
        "profile":       profile_label,
        "bl":            bl,
        "el":            el,
        "rpu":           rpu,
        "hdr":           hdr_summary,
        "dv_tool":       " + ".join(tool_names) if tool_names else "unknown",
        "layer_variant": layer_variant,
        "layer_reason":  layer_reason,
    }


# ── Fact builders ────────────────────────────────────────────────────────────

def build_signal_facts(dv_info: dict[str, str], dovi_scan: dict[str, Any]) -> list[dict[str, str]]:
    summary = dovi_scan.get("summary", {})
    facts = [
        {"label": "Dolby Vision Profile", "value": dv_info["profile"]},
        {"label": "Layer Variant",         "value": dv_info["layer_variant"]},
        {"label": "Base Layer",            "value": dv_info["bl"]},
        {"label": "Enhancement Layer",     "value": dv_info["el"]},
        {"label": "RPU Metadata",          "value": dv_info["rpu"]},
    ]
    if summary.get("dm_version"):               facts.append({"label": "CM Version",          "value": str(summary["dm_version"])})
    if summary.get("frames") is not None:       facts.append({"label": "Frames Sampled",       "value": str(summary["frames"])})
    if summary.get("scene_count") is not None:  facts.append({"label": "Scene Refreshes",      "value": str(summary["scene_count"])})
    if summary.get("rpu_mastering_display"):    facts.append({"label": "RPU Mastering Display", "value": str(summary["rpu_mastering_display"])})
    if summary.get("l2_trims"):                 facts.append({"label": "L2 Trims",             "value": str(summary["l2_trims"])})
    if summary.get("l8_trims"):                 facts.append({"label": "L8 Trims",             "value": str(summary["l8_trims"])})
    return facts


def build_media_facts(
    general_track: dict[str, Any] | None, video_track: dict[str, Any] | None,
    ffprobe_data: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None,
    audio_details: str, subtitle_count: int, bit_depth: int | None,
    container_short: str, tv_support: dict[str, str],
) -> list[dict[str, str]]:
    return [
        {"label": "Container",        "value": get_container_name(general_track, ffprobe_data)},
        {"label": "Container Fit",    "value": tv_support["container_compatibility"]},
        {"label": "Video Codec",      "value": get_video_codec(video_track, ffprobe_video)},
        {"label": "Resolution",       "value": get_resolution(video_track, ffprobe_video)},
        {"label": "Frame Rate",       "value": get_frame_rate(video_track, ffprobe_video)},
        {"label": "Pixel Format",     "value": get_pixel_format(video_track, ffprobe_video)},
        {"label": "Bit Depth",        "value": f"{bit_depth}-bit" if bit_depth is not None else "Unknown"},
        {"label": "Color Range",      "value": get_color_range(video_track, ffprobe_video)},
        {"label": "HDR Signaling",    "value": get_hdr_summary(video_track, ffprobe_video) or "Unknown"},
        {"label": "Mastering Display","value": get_mastering_display(video_track)},
        {"label": "Content Light",    "value": get_cll_summary(video_track)},
        {"label": "Audio",            "value": audio_details},
        {"label": "Subtitles",        "value": str(subtitle_count)},
        {"label": "Runtime",          "value": f"{format_number(get_duration_minutes(general_track, ffprobe_data), 1)} min"},
        {"label": "File Size",        "value": format_size_gb(
            general_track.get("FileSize") if general_track
            else ffprobe_data.get("format", {}).get("size", "") if ffprobe_data else ""
        )},
    ]


def build_tool_reports(
    mediainfo_data: dict[str, Any] | None, general_track: dict[str, Any] | None,
    video_track: dict[str, Any] | None, ffprobe_video: dict[str, Any] | None,
    dovi_scan: dict[str, Any], audio_details: str,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []

    if mediainfo_data:
        details = []
        hdr_summary = get_hdr_summary(video_track, ffprobe_video)
        if hdr_summary:                               details.append(hdr_summary)
        if audio_details != "Unknown audio":          details.append(audio_details)
        mastering = get_mastering_display(video_track)
        if mastering != "Unknown":                    details.append(f"Mastering display: {mastering}")
        reports.append({"name": "MediaInfo", "status": "ok",
                         "headline": f"Container metadata loaded for {get_container_name(general_track, None)}.",
                         "details": details})
    else:
        reports.append({"name": "MediaInfo", "status": "unavailable",
                         "headline": "MediaInfo unavailable — HDR and audio hints are limited.",
                         "details": []})

    side_data = get_ffprobe_dovi_side_data(ffprobe_video)
    if ffprobe_video:
        details = []
        if side_data:
            details.append(
                f"DOVI config record: profile {side_data.get('dv_profile', '?')}, "
                f"BL {flag_text(side_data.get('bl_present_flag'))}, "
                f"EL {flag_text(side_data.get('el_present_flag'))}, "
                f"RPU {flag_text(side_data.get('rpu_present_flag'))}"
            )
        details.append(f"Codec: {get_video_codec(video_track, ffprobe_video)}")
        if dovi_scan.get("status") == "partial":
            details.append(f"FFmpeg copied the first {FFMPEG_SAMPLE_FRAMES} frames for dovi_tool.")
        reports.append({"name": "FFmpeg / ffprobe", "status": "ok",
                         "headline": "Stream-level video metadata parsed successfully.",
                         "details": details})
    else:
        reports.append({"name": "FFmpeg / ffprobe", "status": "unavailable",
                         "headline": "ffprobe unavailable — stream-side DV flags could not be verified.",
                         "details": []})

    reports.append({"name": "dovi_tool",
                     "status": dovi_scan.get("status", "unavailable"),
                     "headline": dovi_scan.get("headline", "DV RPU scan not available."),
                     "details": dovi_scan.get("details", [])})
    return reports


def build_recommendation(
    quality_score: int, confidence_score: int, tv_score: int,
    tv_support: dict[str, str], profile_label: str, layer_variant: str,
    container_short: str = "", audio_label: str = "",
) -> str:
    if tv_support["profile_supported"] == "No":
        return "Not a suitable playback target for this TV."
    recs = []

    if profile_label == "7" and layer_variant.lower().startswith("fel"):
        recs.append(
            "⚙ Convert to P8.1: dovi_tool -m 2 convert --discard → mp4muxer. "
            "FEL is fully wasted on this TV."
        )
    elif profile_label == "7" and "mel" in layer_variant.lower():
        recs.append("⚙ Consider converting to P8.1 MP4 for guaranteed compatibility.")

    if container_short == "MKV":
        recs.append("⚙ Remux to MP4 with mp4muxer for safer DV playback.")

    if "truehd" in audio_label.lower():
        recs.append(
            "🔊 Re-encode audio to E-AC3 Atmos — TrueHD falls back to AC3 core via USB."
        )

    if tv_score >= 80 and confidence_score >= 70:
        base = "Top candidate for your Sony Bravia 8 Mark II."
    elif profile_label in {"8.1", "8.4", "5"}:
        base = "Good native DV candidate for this TV."
    elif profile_label == "7":
        base = "Strong source — but EL not rendered by this TV."
    else:
        base = "Playable, but not the strongest option for this TV."

    return " | ".join([base] + recs) if recs else base


def build_insights(
    profile: str, layer_variant: str, bitrate_mbps: float, source: str,
    audio_details: str, subtitle_count: int, tv_support_note: str,
    dovi_scan: dict[str, Any], confidence: int,
) -> tuple[str, str]:
    notes: list[str] = []
    if profile.startswith("8"):   notes.append("Single-layer DV with HDR-compatible delivery.")
    elif profile == "7":          notes.append("Disc-oriented DV profile — highest-end source candidate.")
    elif profile == "5":          notes.append("Streaming-first single-layer DV.")
    else:                         notes.append("No clear DV profile detected.")

    if layer_variant.lower().startswith("fel"):         notes.append("FEL detected via filename hint.")
    elif layer_variant.lower().startswith("mel"):       notes.append("MEL detected via filename hint.")
    elif "dual-layer" in layer_variant.lower():         notes.append("Dual-layer DV present; FEL vs MEL unproven.")

    if bitrate_mbps >= 30:   notes.append("Good bitrate tier.")
    elif bitrate_mbps >= 15: notes.append("Normal WEB-DL bitrate tier.")
    else:                    notes.append("Aggressively compressed bitrate.")

    if dovi_scan.get("status") == "partial":
        frames = dovi_scan.get("summary", {}).get("frames", FFMPEG_SAMPLE_FRAMES)
        notes.append(f"Bounded dovi_tool pass stopped at {frames} frames.")

    notes.append(f"Primary audio: {audio_details}")
    if subtitle_count:  notes.append(f"Subtitle tracks: {subtitle_count}")
    notes.append(f"TV compatibility: {tv_support_note}")
    notes.append(f"Confidence: {confidence_label(confidence)} ({confidence}/100)")

    summary = f"{source} | DV {profile} | {bitrate_mbps:.2f} Mbps"
    return summary, ". ".join(notes) + "."


# ── Main analysis entry point ────────────────────────────────────────────────

def analyze_file(filepath: str, skip_dovi_scan: bool = False) -> dict[str, Any] | None:
    """Wrapper with timeout protection (6 min max per file)."""
    result_holder = [None]
    error_holder  = [None]

    def _run():
        try:
            result_holder[0] = _analyze_file_inner(filepath, skip_dovi_scan)
        except Exception as e:
            error_holder[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=360)  # 6 min max per file
    if t.is_alive():
        logger.warning("analyze_file timed out for %s", filepath)
        return None
    if error_holder[0]:
        raise error_holder[0]
    return result_holder[0]


def _analyze_file_inner(filepath: str, skip_dovi_scan: bool = False) -> dict[str, Any] | None:
    mediainfo_data, ffprobe_data = probe_metadata(filepath)

    if not mediainfo_data and not ffprobe_data:
        return None

    general_track        = extract_mediainfo_track(mediainfo_data, "General")
    video_track          = extract_mediainfo_track(mediainfo_data, "Video")
    mediainfo_audio_tracks = get_mediainfo_tracks(mediainfo_data, "Audio")

    ffprobe_video_streams = get_ffprobe_streams(ffprobe_data, "video")
    ffprobe_audio_streams = get_ffprobe_streams(ffprobe_data, "audio")
    ffprobe_video = next(
        (s for s in ffprobe_video_streams if not s.get("disposition", {}).get("attached_pic")),
        ffprobe_video_streams[0] if ffprobe_video_streams else None,
    )

    if not video_track and not ffprobe_video:
        return None

    # ── Dovi scan ───────────────────────────────────────────────────────────
    if skip_dovi_scan:
        dovi_scan = {
            "status":   "skipped",
            "headline": "DV RPU scan skipped (fast mode — enable for RPU details).",
            "details":  [],
            "summary":  {},
        }
    else:
        dovi_scan = run_dovi_partial_scan(filepath, video_track, ffprobe_video)

    dv_info        = inspect_dolby_vision(filepath, video_track, ffprobe_video, dovi_scan)
    container_name = get_container_name(general_track, ffprobe_data)
    container_short = get_container_short_name(container_name)
    file_size_gb   = get_file_size_gb(general_track, ffprobe_data)
    duration_minutes = get_duration_minutes(general_track, ffprobe_data)
    bitrate_mbps   = get_bitrate_mbps(video_track, ffprobe_video, ffprobe_data,
                                       file_size_gb=file_size_gb, duration_min=duration_minutes)
    bit_depth      = get_bit_depth(video_track, ffprobe_video)
    resolution     = get_resolution(video_track, ffprobe_video)

    audio_label, audio_details, audio_score = get_primary_audio_summary(
        mediainfo_audio_tracks, ffprobe_audio_streams
    )
    subtitle_count = len(get_ffprobe_streams(ffprobe_data, "subtitle"))
    source = guess_source(os.path.basename(filepath), bitrate_mbps,
                           dv_info["base_profile"], dv_info["el"])

    tv_support = tv_compatibility_heuristic(
        base_profile=dv_info["profile"],
        profile_label=dv_info["profile"],
        el=dv_info["el"],
        layer_variant=dv_info["layer_variant"],
        container_short=container_short,
        video_codec=get_video_codec(video_track, ffprobe_video),
    )

    quality_score = score_video(
        base_profile=dv_info["profile"],
        layer_variant=dv_info["layer_variant"],
        bitrate_mbps=bitrate_mbps,
        source=source,
        bit_depth=bit_depth,
        audio_score=audio_score,
        container_short=container_short,
        tv_support=tv_support["profile_supported"],
        el=dv_info["el"],
        hdr_summary=dv_info["hdr"],
    )

    tv_score, tv_label = score_for_tv(
        base_profile=dv_info["profile"],
        bitrate_mbps=bitrate_mbps,
        bit_depth=bit_depth,
        audio_score=audio_score,
        container_short=container_short,
        source=source,
        layer_variant=dv_info["layer_variant"],
        hdr_summary=dv_info["hdr"],
    )

    usb_compat = check_usb_compatibility(
        container_short=container_short,
        video_codec=get_video_codec(video_track, ffprobe_video),
        audio_details=audio_details,
        bit_depth=bit_depth,
        resolution=resolution,
        file_size_gb=file_size_gb,
    )

    confidence_score = score_confidence(
        mediainfo_data=mediainfo_data,
        ffprobe_data=ffprobe_data,
        video_track=video_track,
        ffprobe_video=ffprobe_video,
        dovi_scan=dovi_scan,
        layer_variant=dv_info["layer_variant"],
        tv_support=tv_support["profile_supported"],
    )

    recommendation = build_recommendation(
        quality_score=quality_score,
        confidence_score=confidence_score,
        tv_score=tv_score,
        tv_support=tv_support,
        profile_label=dv_info["profile"],
        layer_variant=dv_info["layer_variant"],
        container_short=container_short,
        audio_label=audio_label,
    )

    quick_summary, insights = build_insights(
        profile=dv_info["profile"],
        layer_variant=dv_info["layer_variant"],
        bitrate_mbps=bitrate_mbps,
        source=source,
        audio_details=audio_details,
        subtitle_count=subtitle_count,
        tv_support_note=tv_support["note"],
        dovi_scan=dovi_scan,
        confidence=confidence_score,
    )

    tv_playback = (
        f"{tv_support['profile_supported']} | EL {tv_support['el_usable']} "
        f"| {tv_support['container_compatibility']}"
    )

    dv_support_entry = BRAVIA_8_II["dv_support"].get(dv_info["profile"], ("Unknown", ""))

    return {
        "file":             os.path.basename(filepath),
        "path":             os.path.abspath(filepath),
        "dv_profile":       dv_info["profile"],
        "layer_variant":    dv_info["layer_variant"],
        "layer_reason":     dv_info["layer_reason"],
        "el":               dv_info["el"],
        "bl":               dv_info["bl"],
        "rpu":              dv_info["rpu"],
        "dv_tool":          dv_info["dv_tool"],
        "hdr":              dv_info["hdr"],
        "bitrate_mbps":     bitrate_mbps,
        "bit_depth":        bit_depth,
        "color_range":      get_color_range(video_track, ffprobe_video),
        "duration_min":     get_duration_minutes(general_track, ffprobe_data),
        "source":           source,
        "audio":            audio_label,
        "audio_details":    audio_details,
        "audio_score":      audio_score,
        "file_size_gb":     file_size_gb,
        # General quality
        "score":            quality_score,
        "confidence_score": confidence_score,
        "confidence_label": confidence_label(confidence_score),
        # TV-aware scoring
        "tv_score":         tv_score,
        "tv_label":         tv_label,
        "tv_dv_support":    dv_support_entry[0],
        "tv_dv_note":       dv_support_entry[1] if len(dv_support_entry) > 1 else "",
        # USB
        "usb_compatible":   usb_compat["compatible"],
        "usb_issues":       usb_compat["issues"],
        "usb_warnings":     usb_compat["warnings"],
        # TV heuristic
        "tv_profile_supported":      tv_support["profile_supported"],
        "tv_el_usable":              tv_support["el_usable"],
        "tv_container_compatibility": tv_support["container_compatibility"],
        "tv_playback_note":          tv_support["note"],
        "tv_playback":               tv_playback,
        # Summaries
        "quick_summary":    quick_summary,
        "insights":         insights,
        "recommendation":   recommendation,
        # Structured fact blocks
        "signal_facts":     build_signal_facts(dv_info, dovi_scan),
        "media_facts":      build_media_facts(
            general_track, video_track, ffprobe_data, ffprobe_video,
            audio_details, subtitle_count, bit_depth, container_short, tv_support,
        ),
        "tool_reports":     build_tool_reports(
            mediainfo_data, general_track, video_track,
            ffprobe_video, dovi_scan, audio_details,
        ),
    }


# ── Folder scan ──────────────────────────────────────────────────────────────

def scan_folder(folder: str, skip_dovi_scan: bool = False) -> list[dict[str, Any]]:
    file_paths: list[str] = []
    for root, _, files in os.walk(folder):
        for file_name in files:
            if file_name.lower().endswith(VIDEO_EXTENSIONS):
                file_paths.append(os.path.join(root, file_name))

    if not file_paths:
        return []

    results: list[dict[str, Any]] = []
    worker = partial(analyze_file, skip_dovi_scan=skip_dovi_scan)

    with ThreadPoolExecutor(max_workers=min(4, len(file_paths))) as executor:
        future_map = {executor.submit(worker, path): path for path in file_paths}
        for future in as_completed(future_map):
            path = future_map[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as exc:
                logger.warning("scan_folder: failed on %s — %s", path, exc)

    results.sort(
        key=lambda item: (
            item["tv_score"],
            item["score"],
            item["confidence_score"],
            item["bitrate_mbps"],
            item["file"],      # deterministic tiebreaker
        ),
        reverse=True,
    )

    for idx, item in enumerate(results, start=1):
        item["batch_rank"] = idx

    return results


# ── CSV export ───────────────────────────────────────────────────────────────

def save_csv(results: list[dict[str, Any]], output: str = "results.csv") -> None:
    keys = [
        "batch_rank", "path", "file", "dv_profile", "layer_variant", "el", "bl", "rpu",
        "hdr", "bitrate_mbps", "bit_depth", "color_range", "duration_min", "file_size_gb",
        "source", "audio", "audio_score",
        "score", "confidence_score", "confidence_label",
        "tv_score", "tv_label", "tv_dv_support", "tv_dv_note",
        "usb_compatible", "usb_issues", "usb_warnings",
        "tv_profile_supported", "tv_el_usable", "tv_container_compatibility",
        "tv_playback_note", "tv_playback", "dv_tool",
        "quick_summary", "insights", "recommendation",
    ]
    with open(output, "w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)


if __name__ == "__main__":
    folder = input("Enter folder path: ").strip().strip('"')
    results = scan_folder(folder)
    if results:
        save_csv(results)
        print("Done. Saved to results.csv")
        top = results[0]
        print(f"Top candidate for Bravia 8 II: {top['file']}")
        print(f"  TV score={top['tv_score']} ({top['tv_label']}) | "
              f"Quality={top['score']} | Confidence={top['confidence_score']}")
    else:
        print("No valid files found.")