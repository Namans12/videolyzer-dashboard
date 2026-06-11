"""HDR10+ confirmation via hdr10plus_tool — multi-slice edition.

MediaInfo only reports HDR10+ when the SMPTE ST 2094-40 SEI lives where it
can see — which on a partial magnet probe often isn't true. This module is
the fallback: extract small HEVC bitstream slices from multiple positions
through the file, run hdr10plus_tool on each, return confirmed as soon as
any slice produces ≥1 HDR10+ frame.

Why multi-slice instead of one big extraction?
    The magnet pipeline downloads sparse pieces: head + tail + ~1 piece per
    10 GB. `ffmpeg -c copy` reads sequentially, so a single extraction would
    hit the sparse zero region right after the head and either error out or
    fill the temp drive with zero bytes (we cap that with `-fs`, but it's
    still wasted). Instead we do N small extractions, each seeking with
    `-ss <time>` to a position where we know libtorrent has real bytes.

Outcomes:
    "confirmed"   — hdr10plus_tool found ≥1 HDR10+ metadata frame somewhere
    "negative"    — every slice scanned cleanly, none had HDR10+
    "unavailable" — hdr10plus_tool or ffmpeg not on PATH; skip
    "error"       — every slice failed (extraction or scan); inconclusive

Returns a dict shaped like the rest of analysis.py's tool-report blocks so
build_tool_reports() can render it the same way.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from analysis import (
    resolve_tool, run_command, command_failed,
    make_temp_path, remove_if_exists,
    FFMPEG_TIMEOUT_SECONDS,
)

logger = logging.getLogger("video-analyzer.hdr10plus_scan")

# Cap on frames hdr10plus_tool will read. The default scans the whole
# bitstream which can hang on huge files. 5000 frames is ~3.5 min of
# 24 fps content — more than enough to see HDR10+ presence.
HDR10PLUS_FRAME_LIMIT = 5000
HDR10PLUS_TIMEOUT_S   = 90

# Per-slice caps live next to `_extract_slice` further down — see the
# `PER_SLICE_*` constants there. We deliberately removed the old
# whole-file caps; multi-slice extraction never needs them because each
# `-ss <time>` seek lands in a small window we explicitly bound.

# Fallback locations for hdr10plus_tool.exe when it isn't on PATH.
# These are checked in order after `shutil.which` fails. Add more
# paths here if you install the tool elsewhere.
HDR10PLUS_FALLBACK_PATHS = [
    r"C:\hdr10plustool\hdr10plus_tool.exe",
    r"C:\hdr10plus_tool.exe",
    r"C:\Tools\hdr10plus_tool.exe",
    r"C:\Program Files\hdr10plus_tool\hdr10plus_tool.exe",
]


def _resolve_hdr10plus_tool() -> str | None:
    """Find hdr10plus_tool — first via PATH, then via known install spots."""
    found = resolve_tool("hdr10plus_tool")
    if found:
        return found
    if os.name == "nt":
        for candidate in HDR10PLUS_FALLBACK_PATHS:
            if os.path.isfile(candidate):
                return candidate
    return None


# ── Slice position planner ──────────────────────────────────────────────────
# These must stay in sync with magnet.py's middle-piece schedule so the
# time positions we ask ffmpeg to seek to actually have real bytes
# (libtorrent leaves the gaps between pieces as sparse zeros). Importing
# the magnet constants directly couples them safely.
def _build_slice_positions(file_size: int,
                            duration_seconds: float | None) -> list[tuple[str, float]]:
    """Return a list of (label, time_seconds) to scan, in priority order.

    The head is always scanned first — most HDR10+ releases announce the
    SEI in the very first scene, so the common case never needs more than
    one ffmpeg pass. Middle samples + tail come after, and we only emit
    them when the file is large enough that magnet.py would have also
    fetched extra pieces (otherwise the gaps don't exist).
    """
    positions: list[tuple[str, float]] = [("head", 0.0)]

    # Without a duration we can't seek by time. Bail with just the head —
    # behaviour matches the old single-extraction path.
    if not duration_seconds or duration_seconds <= 0 or file_size <= 0:
        return positions

    try:
        from magnet import (
            HEAD_BYTES, TAIL_BYTES,
            MIDDLE_SAMPLE_INTERVAL_BYTES, MIDDLE_SAMPLE_MIN_FILE_SIZE,
        )
    except ImportError:
        # Standalone use (e.g. CLI test) — fall back to fixed thresholds.
        HEAD_BYTES = 32 * 1024 * 1024
        TAIL_BYTES = 64 * 1024 * 1024
        MIDDLE_SAMPLE_INTERVAL_BYTES = 10 * 1024 * 1024 * 1024
        MIDDLE_SAMPLE_MIN_FILE_SIZE  = 15 * 1024 * 1024 * 1024

    # Linear byte→time map. Works well for CBR/quasi-CBR HEVC; on highly
    # VBR content the actual frame at that timestamp might be ±a few seconds
    # off, but we extract a 500-frame slice from each point so a few seconds
    # of drift is irrelevant.
    bytes_to_seconds = lambda b: duration_seconds * (b / file_size)

    if file_size >= MIDDLE_SAMPLE_MIN_FILE_SIZE:
        offset = HEAD_BYTES + MIDDLE_SAMPLE_INTERVAL_BYTES
        # Stop one interval before the tail so the seek lands inside a
        # piece that libtorrent actually downloaded.
        last_offset = file_size - TAIL_BYTES - MIDDLE_SAMPLE_INTERVAL_BYTES
        i = 1
        while offset < last_offset:
            positions.append((f"mid-{i}", bytes_to_seconds(offset)))
            offset += MIDDLE_SAMPLE_INTERVAL_BYTES
            i += 1
        # And a tail sample if there's room.
        if file_size > TAIL_BYTES * 2:
            positions.append(("tail", bytes_to_seconds(file_size - TAIL_BYTES)))

    # Cap at 8 positions so the worst case stays bounded
    # (8 × ~2 s ffmpeg + ~1 s hdr10plus_tool ≈ 24 s wall time).
    return positions[:8]


# ── Per-slice helpers ───────────────────────────────────────────────────────

# Per-slice frame cap (each ffmpeg call extracts at most this many frames).
# 500 frames @ 24 fps ≈ 21 s of content — enough to span a few scene cuts
# and capture any per-scene HDR10+ SEI.
PER_SLICE_FRAME_LIMIT = 500
# Per-slice byte cap. With UHD HEVC averaging ~1 MB/frame, 500 frames ~=
# 500 MB worst case. We cap at 300 MB which truncates the noisiest scenes
# but always catches the SEI carriers (which sit on IDR frames at the top
# of each GOP — first few frames after the seek).
PER_SLICE_BYTE_LIMIT = 300_000_000


def _extract_slice(ffmpeg_bin: str, file_path: str,
                    start_seconds: float, out_path: str) -> bool:
    """Use ffmpeg to copy a short HEVC slice starting at `start_seconds`.
    Returns True if the output file looks usable (≥ 1 KB)."""
    # `-ss` BEFORE `-i` does an input-side fast seek to the nearest
    # keyframe — critical here because we don't want ffmpeg decoding
    # from the start through the sparse middle.
    args = [
        ffmpeg_bin, "-y", "-v", "error",
    ]
    if start_seconds > 0.5:
        args += ["-ss", f"{start_seconds:.3f}"]
    args += [
        "-i", file_path,
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-bsf:v", "hevc_mp4toannexb",
        "-c", "copy",
        "-frames:v", str(PER_SLICE_FRAME_LIMIT),
        "-fs", str(PER_SLICE_BYTE_LIMIT),
        "-f", "hevc",
        out_path,
    ]
    result = run_command(args, timeout=FFMPEG_TIMEOUT_SECONDS)
    if command_failed(result):
        return False
    return os.path.isfile(out_path) and os.path.getsize(out_path) >= 1024


def _scan_slice(hdr10_bin: str, hevc_path: str) -> dict | None:
    """Run hdr10plus_tool against one slice. Return:
        {"frames": int, "first_scene": dict | None}  on a clean run
        None                                         on failure (caller skips)
    """
    json_path = make_temp_path(".json")
    try:
        result = run_command(
            [
                hdr10_bin, "extract",
                "-i", hevc_path,
                "-o", json_path,
                "--limit", str(HDR10PLUS_FRAME_LIMIT),
            ],
            timeout=HDR10PLUS_TIMEOUT_S,
        )
        if command_failed(result):
            stderr = (result.stderr or "").lower() if result else ""
            # Explicit "no metadata" is a clean negative, not a failure.
            if "no metadata" in stderr:
                return {"frames": 0, "first_scene": None}
            return None
        if not os.path.isfile(json_path) or os.path.getsize(json_path) < 4:
            return {"frames": 0, "first_scene": None}
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        scene_info = data.get("SceneInfo") or []
        frames = len(scene_info) if isinstance(scene_info, list) else 0
        first_scene = scene_info[0] if frames and isinstance(scene_info[0], dict) else None
        return {"frames": frames, "first_scene": first_scene}
    finally:
        remove_if_exists(json_path)


def scan_hdr10_plus(file_path: str,
                     duration_seconds: float | None = None) -> dict[str, Any]:
    """Run ffmpeg + hdr10plus_tool against `file_path` at multiple time
    positions. Return a report dict.

    Args:
        file_path:        the probe file (head-only for MKV, head+tail for MP4)
                          or a fully-local file for non-magnet analyses.
        duration_seconds: optional duration hint, used to compute the
                          time positions for `-ss` seek. When omitted, only
                          the head is scanned (matches the old behaviour).

    Always returns a dict with `status`, `headline`, `details`, `frames`,
    `confirmed`, plus `slices_scanned` and `slices_with_hdr10_plus` so the
    UI tool-report panel can show where the confirmation came from. Never
    raises — failure modes are encoded in `status`.
    """
    ffmpeg_bin = resolve_tool("ffmpeg")
    hdr10_bin  = _resolve_hdr10plus_tool()
    if not ffmpeg_bin or not hdr10_bin:
        missing = ", ".join(
            n for n, p in [("ffmpeg", ffmpeg_bin), ("hdr10plus_tool", hdr10_bin)] if not p
        )
        return {
            "status":    "unavailable",
            "headline":  f"HDR10+ scan skipped — {missing} not installed.",
            "details":   [],
            "frames":    0,
            "confirmed": False,
            "slices_scanned": 0,
            "slices_with_hdr10_plus": 0,
        }

    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        file_size = 0

    positions = _build_slice_positions(file_size, duration_seconds)
    logger.info("HDR10+ scan plan for %s: %d slice(s) at %s",
                 file_path, len(positions),
                 [f"{lbl}@{t:.1f}s" for lbl, t in positions])

    total_frames           = 0
    slices_scanned         = 0
    slices_with_metadata   = 0
    extract_failures       = 0
    scan_failures          = 0
    consecutive_failures   = 0
    first_confirmed_scene  : dict | None = None
    first_confirmed_label  : str  | None = None
    confirmed_locations    : list[str]   = []

    # If interior extractions keep failing, the middle pieces simply aren't on
    # disk (common: partial magnet probe, or deep scan on a release with no
    # downloaded middle). Bail after this many consecutive extraction failures
    # rather than grinding through all 8 positions at ~45 s each — that runaway
    # was pushing analyze_file past its 6-min ceiling and silently dropping the
    # whole file. The head is always position 0, so a real HDR10+ release still
    # confirms before we ever hit this.
    MAX_CONSECUTIVE_EXTRACT_FAILURES = 2

    for label, start in positions:
        slice_path = make_temp_path(".hevc")
        try:
            if not _extract_slice(ffmpeg_bin, file_path, start, slice_path):
                extract_failures += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_EXTRACT_FAILURES:
                    logger.info("hdr10plus: %d consecutive extraction failures — "
                                "interior pieces unavailable; stopping scan early.",
                                consecutive_failures)
                    break
                continue
            consecutive_failures = 0

            slice_result = _scan_slice(hdr10_bin, slice_path)
            if slice_result is None:
                scan_failures += 1
                continue

            slices_scanned += 1
            frames = slice_result["frames"]
            total_frames += frames
            if frames > 0:
                slices_with_metadata += 1
                confirmed_locations.append(f"{label} (+{frames}f)")
                if first_confirmed_scene is None:
                    first_confirmed_scene = slice_result["first_scene"]
                    first_confirmed_label = label
                # Early exit on the FIRST confirming slice keeps the common
                # "HDR10+ announced in the head" case fast (≈ 4 s total).
                # Mid/tail slices only get scanned when the head didn't.
                break
        except Exception as exc:  # noqa: BLE001
            logger.exception("hdr10plus slice %s @ %.1fs failed", label, start)
            scan_failures += 1
        finally:
            remove_if_exists(slice_path)

    # ── Build the report ────────────────────────────────────────────────
    common: dict[str, Any] = {
        "slices_scanned":         slices_scanned,
        "slices_with_hdr10_plus": slices_with_metadata,
    }

    if total_frames > 0:
        details: list[str] = [
            f"Found HDR10+ across {total_frames} frame(s) in slice "
            f"'{first_confirmed_label}'.",
        ]
        if first_confirmed_scene:
            if isinstance(first_confirmed_scene.get("AverageMaxRGB"), (int, float)):
                details.append(f"First frame AvgMaxRGB: {first_confirmed_scene['AverageMaxRGB']}")
            if isinstance(first_confirmed_scene.get("MaxScl"), list) and first_confirmed_scene["MaxScl"]:
                details.append(f"MaxScl: {first_confirmed_scene['MaxScl']}")
        if len(positions) > 1:
            details.append(
                f"Scan plan: {len(positions)} slice position(s) — "
                f"confirmed by the first match, remaining were skipped."
            )
        return dict(common, status="confirmed",
                    headline=f"HDR10+ confirmed by hdr10plus_tool ({total_frames} frames).",
                    details=details, frames=total_frames, confirmed=True)

    # No frames found in anything we successfully scanned.
    if slices_scanned > 0:
        details = [
            f"Scanned {slices_scanned} slice(s); no HDR10+ frames present.",
        ]
        if extract_failures or scan_failures:
            details.append(
                f"({extract_failures} extraction failure(s), {scan_failures} scan failure(s))"
            )
        return dict(common, status="negative",
                    headline=f"hdr10plus_tool scanned {slices_scanned} slice(s) — no HDR10+ metadata.",
                    details=details, frames=0, confirmed=False)

    # Nothing scanned cleanly — everything either failed to extract or to
    # scan. Inconclusive.
    return dict(common, status="error",
                headline="HDR10+ scan inconclusive — all slice extractions or scans failed.",
                details=[f"{extract_failures} extraction failure(s), "
                          f"{scan_failures} scan failure(s) "
                          f"across {len(positions)} planned slice(s)."],
                frames=0, confirmed=False)


# The single-slice extraction body that lived here through earlier
# revisions was replaced by `scan_hdr10_plus` above. If you need to look
# at the old shape, see commit history (git blame on this file).
