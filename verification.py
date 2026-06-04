"""Verification engine — cross-check filename claims against stream metadata.

Given an `analysis_result` dict (from analysis.py:analyze_file) and a parsed
`intel` dict (from filename_intel.parse_release_name), return a list of
flags describing where the filename's claims do or don't match reality.

Each flag has:
    code:     short stable identifier (e.g. "fake_dv")
    severity: "info" | "warn" | "error"
    message:  human-readable explanation
    evidence: short technical detail (what we saw vs what we expected)

The frontend can render these as colored badges, and the recommendation
engine can use them to penalize a release's quality score.
"""

from __future__ import annotations

from typing import Any


# ── Severity helpers ─────────────────────────────────────────────────────────

def _flag(code: str, severity: str, message: str, evidence: str = "") -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message, "evidence": evidence}


# ── Individual checks ────────────────────────────────────────────────────────

def _check_fake_dv(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Filename says DV, but stream has no DV profile / no RPU."""
    flags: list[dict[str, str]] = []
    if not intel.get("dolby_vision"):
        return flags
    dv_profile = (analysis.get("dv_profile") or "").lower()
    rpu        = (analysis.get("rpu") or "").lower()
    bl         = (analysis.get("bl") or "").lower()
    hdr        = (analysis.get("hdr") or "").lower()

    profile_missing = dv_profile in {"", "none", "unknown"}
    rpu_missing     = rpu != "yes"
    dv_in_hdr       = "dolby vision" in hdr

    if profile_missing and rpu_missing and not dv_in_hdr:
        flags.append(_flag(
            "fake_dv", "error",
            "Filename advertises Dolby Vision but stream has no DV profile or RPU.",
            f"dv_profile={analysis.get('dv_profile')}, rpu={analysis.get('rpu')}, bl={bl}",
        ))
    elif profile_missing and dv_in_hdr:
        flags.append(_flag(
            "dv_partial", "warn",
            "DV signalled in HDR metadata but profile could not be determined.",
            f"hdr='{analysis.get('hdr')}'",
        ))
    return flags


def _check_fake_hdr10_plus(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Filename claims HDR10+. Cross-check three sources of truth, in order:

      1. hdr10plus_tool scan result (`hdr10_plus_confirmed`)
            True  → confirmed, no flag.
            False → real "no HDR10+ present" — promote to *error*.
            None  → scan unavailable; fall back to MediaInfo.
      2. MediaInfo `hdr` string for SMPTE ST 2094 / HDR10+ markers.
      3. Otherwise: partial-probe-aware warning.
    """
    flags: list[dict[str, str]] = []
    if not intel.get("hdr10_plus"):
        return flags

    confirmed = analysis.get("hdr10_plus_confirmed")
    if confirmed is True:
        # hdr10plus_tool saw SEI frames — filename is honest, no flag.
        return flags
    if confirmed is False:
        # hdr10plus_tool ran cleanly and the bitstream genuinely has no
        # HDR10+ metadata. This is a hard "fake HDR10+" — promote to error.
        return [_flag(
            "fake_hdr10_plus", "error",
            "Filename claims HDR10+ but hdr10plus_tool found zero SEI frames in the bitstream.",
            f"hdr10plus_frames=0, status={analysis.get('hdr10_plus_status')}",
        )]

    # `confirmed is None` — hdr10plus_tool unavailable or errored.
    # Fall back to MediaInfo string check.
    hdr = (analysis.get("hdr") or "").lower()
    hdr10_plus_signals = ("hdr10+", "smpte st 2094 app 4", "smpte 2094-40")
    if any(sig in hdr for sig in hdr10_plus_signals):
        return flags

    is_partial = bool(analysis.get("magnet_partial"))
    flags.append(_flag(
        "fake_hdr10_plus", "info" if is_partial else "warn",
        ("Filename claims HDR10+ but no HDR10+ marker in the parsed metadata "
         "(partial-file probe — may be hidden in middle bitstream)."
         if is_partial else
         "Filename claims HDR10+ but no SMPTE ST 2094 App 4 metadata found."),
        f"hdr='{analysis.get('hdr')}'{' (partial)' if is_partial else ''}",
    ))
    return flags


def _check_fake_atmos(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Filename says Atmos, but no JOC / object-audio signal in audio metadata."""
    flags: list[dict[str, str]] = []
    if not intel.get("atmos"):
        return flags
    audio = (analysis.get("audio_details") or "").lower()
    audio_label = (analysis.get("audio") or "").lower()
    combined = f"{audio} {audio_label}"
    # MediaInfo / ffprobe expose Atmos via:
    #   - "Atmos" or "JOC" in commercial-format field
    #   - "Joint Object Coding" in additional features
    atmos_signals = ("atmos", "joc", "joint object")
    if not any(sig in combined for sig in atmos_signals):
        flags.append(_flag(
            "fake_atmos", "warn",
            "Filename claims Atmos but no JOC / object-audio metadata in stream.",
            f"audio='{analysis.get('audio_details')}'",
        ))
    return flags


def _check_fake_dts_x(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Filename says DTS:X, but no DTS:X / XLL X marker in audio metadata."""
    flags: list[dict[str, str]] = []
    if not intel.get("dts_x"):
        return flags
    audio = (analysis.get("audio_details") or "").lower()
    audio_label = (analysis.get("audio") or "").lower()
    combined = f"{audio} {audio_label}"
    if "dts:x" not in combined and "dts x" not in combined and "xll x" not in combined:
        flags.append(_flag(
            "fake_dts_x", "warn",
            "Filename claims DTS:X but no DTS:X object marker in stream.",
            f"audio='{analysis.get('audio_details')}'",
        ))
    return flags


def _check_codec_mismatch(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    claimed = (intel.get("codec") or "").lower()
    actual  = (analysis.get("hdr") or "").lower()  # HDR string sometimes carries codec; weak
    # Better: pull from media_facts.
    actual_codec = ""
    for fact in analysis.get("media_facts", []) or []:
        if fact.get("label") == "Video Codec":
            actual_codec = (fact.get("value") or "").lower()
            break
    if not claimed or not actual_codec:
        return flags
    if claimed == "hevc" and "hevc" not in actual_codec and "h.265" not in actual_codec:
        flags.append(_flag(
            "codec_mismatch", "error",
            f"Filename claims {claimed.upper()} but stream codec is '{actual_codec}'.",
            f"claimed={claimed}, actual={actual_codec}",
        ))
    elif claimed == "h.264" and "avc" not in actual_codec and "h.264" not in actual_codec:
        flags.append(_flag(
            "codec_mismatch", "error",
            f"Filename claims H.264 but stream codec is '{actual_codec}'.",
            f"claimed={claimed}, actual={actual_codec}",
        ))
    return flags


def _check_low_bitrate(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Suspiciously low bitrate for the claimed source tier."""
    flags: list[dict[str, str]] = []
    bitrate = analysis.get("bitrate_mbps") or 0
    source  = (intel.get("source") or "").upper()
    resolution = (intel.get("resolution") or "")

    if bitrate <= 0:
        return flags

    # Tier thresholds (Mbps) for 2160p — relax for 1080p.
    is_uhd = resolution == "2160p"
    if source in {"REMUX", "UHD BLURAY"} and is_uhd and bitrate < 40:
        flags.append(_flag(
            "low_bitrate_remux", "warn",
            f"Bitrate {bitrate:.1f} Mbps is unusually low for a 2160p REMUX/BluRay.",
            f"bitrate={bitrate}, expected>=40 Mbps for UHD REMUX",
        ))
    elif source == "BLURAY" and is_uhd and bitrate < 25:
        flags.append(_flag(
            "low_bitrate_bluray", "info",
            f"Bitrate {bitrate:.1f} Mbps is low for a 2160p BluRay encode.",
            f"bitrate={bitrate}",
        ))
    elif source == "WEB-DL" and is_uhd and bitrate < 12:
        flags.append(_flag(
            "low_bitrate_web", "info",
            f"Bitrate {bitrate:.1f} Mbps is low even for 2160p WEB-DL.",
            f"bitrate={bitrate}",
        ))
    return flags


def _check_container_mismatch(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    claimed = (intel.get("container") or "").upper()
    actual  = ""
    for fact in analysis.get("media_facts", []) or []:
        if fact.get("label") == "Container":
            actual = (fact.get("value") or "").upper()
            break
    if not claimed or not actual:
        return flags
    short_map = {"MKV": "MATROSKA", "MP4": "MP4", "M2TS": "BDAV", "TS": "MPEG-TS"}
    actual_norm = short_map.get(claimed, claimed)
    if actual_norm.lower() not in actual.lower():
        flags.append(_flag(
            "container_mismatch", "info",
            f"Filename suggests {claimed}, MediaInfo reports '{actual}'.",
            f"claimed={claimed}, actual={actual}",
        ))
    return flags


def _check_resolution_mismatch(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    claimed = (intel.get("resolution") or "")
    if not claimed:
        return flags
    actual_res = ""
    for fact in analysis.get("media_facts", []) or []:
        if fact.get("label") == "Resolution":
            actual_res = fact.get("value") or ""
            break
    if not actual_res or "x" not in actual_res:
        return flags
    try:
        _, h = actual_res.split("x", 1)
        h = int(h)
    except ValueError:
        return flags
    expected_h = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
                  "576p": 576, "480p": 480}.get(claimed)
    if expected_h is None:
        return flags
    # Allow ±200 (anamorphic crop, e.g. 2160→1648 letterboxed)
    if abs(h - expected_h) > 200 and h < expected_h - 200:
        flags.append(_flag(
            "resolution_mismatch", "warn",
            f"Filename claims {claimed} but stream is {actual_res}.",
            f"claimed_h={expected_h}, actual_h={h}",
        ))
    return flags


def _check_dv_profile_compat(analysis: dict[str, Any], intel: dict[str, Any]) -> list[dict[str, str]]:
    """Profile-7 FEL on streaming-platform source is suspicious (P5/P8 expected)."""
    flags: list[dict[str, str]] = []
    profile = (analysis.get("dv_profile") or "").lower()
    platform = intel.get("platform") or ""
    layer    = (analysis.get("layer_variant") or "").lower()
    if profile == "7" and platform in {"Amazon", "Netflix", "Disney+", "iTunes", "Apple TV+", "Hulu"}:
        flags.append(_flag(
            "unusual_profile", "info",
            f"Profile 7 DV from {platform} is unusual — streaming usually delivers P5 or P8.",
            f"profile={profile}, platform={platform}, layer={layer}",
        ))
    return flags


# ── Public API ───────────────────────────────────────────────────────────────

def verify_release(analysis: dict[str, Any], intel: dict[str, Any]) -> dict[str, Any]:
    """Run all verification checks. Return a structured report.

    Returns:
        {
          "flags":         [ {code, severity, message, evidence}, ... ],
          "error_count":   int,
          "warn_count":    int,
          "info_count":    int,
          "trust_score":   0–100  (100 = filename claims fully validated),
          "summary":       short headline string,
        }
    """
    checks = [
        _check_fake_dv,
        _check_fake_hdr10_plus,
        _check_fake_atmos,
        _check_fake_dts_x,
        _check_codec_mismatch,
        _check_container_mismatch,
        _check_resolution_mismatch,
        _check_low_bitrate,
        _check_dv_profile_compat,
    ]
    flags: list[dict[str, str]] = []
    for check in checks:
        try:
            flags.extend(check(analysis, intel) or [])
        except Exception as exc:  # noqa: BLE001 — never let a check break the pipeline
            flags.append(_flag(
                f"check_error:{check.__name__}", "info",
                f"Verification check {check.__name__} crashed: {exc}", "",
            ))

    error_count = sum(1 for f in flags if f["severity"] == "error")
    warn_count  = sum(1 for f in flags if f["severity"] == "warn")
    info_count  = sum(1 for f in flags if f["severity"] == "info")

    # Trust score: start at 100, subtract per flag by severity.
    trust = 100 - (error_count * 30) - (warn_count * 10) - (info_count * 3)
    trust = max(0, min(100, trust))

    if error_count:
        summary = f"⚠ {error_count} mismatch(es) — claims don't match stream metadata."
    elif warn_count:
        summary = f"{warn_count} warning(s) — some claims could not be verified."
    elif info_count:
        summary = f"{info_count} minor note(s)."
    else:
        summary = "All filename claims verified against stream metadata."

    return {
        "flags":       flags,
        "error_count": error_count,
        "warn_count":  warn_count,
        "info_count":  info_count,
        "trust_score": trust,
        "summary":     summary,
    }
