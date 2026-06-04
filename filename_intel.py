"""Filename intelligence — pull release traits from a torrent / file name.

Pure-regex parser. No external deps. Extracts the signals a viewer cares
about when picking between releases:

    platform, source, release group, codec hint, container hint,
    audio codec hint, channel layout, Atmos / DTS:X flags,
    DV / HDR10+ / HDR10 / HLG flags, IMAX, hybrid, repack/proper,
    year, resolution.

The output is *hints* — every field is best-effort. The MediaInfo /
ffprobe / dovi_tool pass remains the ground truth. These hints are used
to cross-check the stream metadata (fake-DV detection lives in
verification.py) and to populate UI badges quickly even before the
partial download finishes.
"""

from __future__ import annotations

import os
import re
from typing import Any

# ── Streaming platforms ──────────────────────────────────────────────────────
# Map filename token → human-readable platform name.
PLATFORM_TOKENS: dict[str, str] = {
    "amzn":      "Amazon",
    "amazon":    "Amazon",
    "nf":        "Netflix",
    "netflix":   "Netflix",
    "dsnp":      "Disney+",
    "disney":    "Disney+",
    "hmax":      "HBO Max",
    "max":       "Max",
    "hulu":      "Hulu",
    "atvp":      "Apple TV+",
    "appletv":   "Apple TV+",
    "it":        "iTunes",
    "itunes":    "iTunes",
    "pcok":      "Peacock",
    "peacock":   "Peacock",
    "pmtp":      "Paramount+",
    "paramount": "Paramount+",
    "stan":      "Stan",
    "crav":      "Crave",
    "ma":        "Movies Anywhere",
}

# ── Source tags ──────────────────────────────────────────────────────────────
SOURCE_PATTERNS: list[tuple[str, str]] = [
    (r"\bremux\b",                 "REMUX"),
    (r"\buhd[\.\s_-]*blu-?ray\b",  "UHD BluRay"),
    (r"\bblu-?ray\b",              "BluRay"),
    (r"\bbd(rip|remux)\b",         "BluRay"),
    (r"\bweb-?dl\b",               "WEB-DL"),
    (r"\bweb-?rip\b",              "WEBRip"),
    (r"\bweb\b",                   "WEB"),
    (r"\bhdtv\b",                  "HDTV"),
    (r"\bdvd-?rip\b",              "DVDRip"),
    (r"\bdvd\b",                   "DVD"),
    (r"\bcam\b",                   "CAM"),
]

# ── Video codec hints ────────────────────────────────────────────────────────
# Note: _norm() replaces dots with spaces, so "H.265" arrives here as "h 265".
# The patterns therefore match either form ("h265" or "h 265").
CODEC_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:h\s*265|hevc|x265)\b", "HEVC"),
    (r"\b(?:h\s*264|avc|x264)\b",  "H.264"),
    (r"\bav1\b",                   "AV1"),
    (r"\bvp9\b",                   "VP9"),
    (r"\bmpeg-?2\b",               "MPEG-2"),
]

# ── Audio codec hints ────────────────────────────────────────────────────────
AUDIO_PATTERNS: list[tuple[str, str]] = [
    (r"\btruehd\b",                                "TrueHD"),
    (r"\bdts-?hd[\.\s_-]*ma\b",                    "DTS-HD MA"),
    (r"\bdts-?hd\b",                               "DTS-HD"),
    (r"\b(?:dts-?x|dts[\.\s_-]*x)\b",              "DTS:X"),
    (r"\bdts\b",                                   "DTS"),
    (r"\b(?:ddp|e-?ac-?3|eac3)\d?(?:\.\d)?\b",     "E-AC3"),
    (r"\b(?:dd|ac-?3)\d?(?:\.\d)?\b",              "AC3"),
    (r"\b(?:lpcm|pcm)\b",                          "LPCM"),
    (r"\baac\b",                                   "AAC"),
    (r"\bopus\b",                                  "Opus"),
    (r"\bflac\b",                                  "FLAC"),
]

# Channel layouts like 5.1, 7.1, 2.0 — _norm() turns "5.1" into "5 1",
# so accept both. Use lookbehind to require a non-digit before the leading
# number so "5 1" embedded in something like "h 265 1" doesn't match.
CHANNEL_RE = re.compile(r"(?<!\d)([257])[\s.]([01])(?!\d)")

# Resolutions
RES_PATTERNS: list[tuple[str, str]] = [
    (r"\b2160p\b", "2160p"),
    (r"\b1440p\b", "1440p"),
    (r"\b1080p\b", "1080p"),
    (r"\b720p\b",  "720p"),
    (r"\b576p\b",  "576p"),
    (r"\b480p\b",  "480p"),
    (r"\b4k\b",    "2160p"),
]

# Container hints — we still trust MediaInfo when available
CONTAINER_PATTERNS: list[tuple[str, str]] = [
    (r"\.mkv$",   "MKV"),
    (r"\.mp4$",   "MP4"),
    (r"\.m2ts$",  "M2TS"),
    (r"\.ts$",    "TS"),
    (r"\.hevc$",  "RAW HEVC"),
    (r"\.h265$",  "RAW HEVC"),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase, replace dots/underscores with spaces. Keep extension off."""
    base = os.path.basename(text or "")
    return base.replace(".", " ").replace("_", " ").lower()


def _first_match(haystack: str, table: list[tuple[str, str]]) -> str | None:
    for pattern, value in table:
        if re.search(pattern, haystack):
            return value
    return None


def _has(haystack: str, pattern: str) -> bool:
    return bool(re.search(pattern, haystack))


# ── Public API ───────────────────────────────────────────────────────────────

def parse_release_name(name: str) -> dict[str, Any]:
    """Extract release intelligence from a torrent / file name.

    All fields are best-effort hints. Cross-check against stream metadata.
    """
    if not name:
        return _empty()

    raw       = os.path.basename(name)
    norm      = _norm(raw)              # spaces, lowercase, basename
    raw_lower = raw.lower()             # for extension matching

    # ── Platform ────────────────────────────────────────────────────────────
    platform: str | None = None
    for token, label in PLATFORM_TOKENS.items():
        # Word-boundary match against the normalized name.
        # `it` is short — require it to appear next to web-dl or as a clear tag.
        if token == "it":
            if re.search(r"\bit\b[\.\s]*web-?dl", norm) or re.search(r"\bitunes\b", norm):
                platform = "iTunes"
                break
            continue
        if re.search(rf"\b{re.escape(token)}\b", norm):
            platform = label
            break

    # ── Source ──────────────────────────────────────────────────────────────
    source = _first_match(norm, SOURCE_PATTERNS)

    # ── Resolution ──────────────────────────────────────────────────────────
    resolution = _first_match(norm, RES_PATTERNS)

    # ── Codec / Container ───────────────────────────────────────────────────
    codec     = _first_match(norm, CODEC_PATTERNS)
    container = _first_match(raw_lower, CONTAINER_PATTERNS)

    # ── Audio ───────────────────────────────────────────────────────────────
    audio_codec = _first_match(norm, AUDIO_PATTERNS)
    channel_match = CHANNEL_RE.search(norm)
    channels = f"{channel_match.group(1)}.{channel_match.group(2)}" if channel_match else None

    atmos    = _has(norm, r"\batmos\b")
    dts_x    = _has(norm, r"\b(?:dts-?x|dts\s*x)\b")
    # If audio codec didn't match DTS:X but the explicit token did, prefer it.
    if dts_x and audio_codec not in {"DTS:X"}:
        audio_codec = "DTS:X"

    # ── HDR / DV flags ──────────────────────────────────────────────────────
    # HDR10+ must be checked before HDR10 because the +/p suffix is the
    # signal — match the literal "+" or "p" / "plus" right after HDR10.
    hdr10_plus = _has(norm, r"\bhdr10\s*(?:\+|plus|p)\b") or "hdr10+" in raw_lower
    hdr10      = (not hdr10_plus) and _has(norm, r"\bhdr10\b")
    hdr_generic = _has(norm, r"\bhdr\b") and not hdr10 and not hdr10_plus
    hlg        = _has(norm, r"\bhlg\b")
    dolby_vision = _has(norm, r"\b(?:dv|dolby\s*vision|dovi)\b")

    # ── Special flags ───────────────────────────────────────────────────────
    imax      = _has(norm, r"\bimax\b")
    hybrid    = _has(norm, r"\bhybrid\b")
    repack    = _has(norm, r"\brepack\b")
    proper    = _has(norm, r"\bproper\b")
    extended  = _has(norm, r"\bextended\b")
    directors = _has(norm, r"\bdirector'?s?\b")
    remastered = _has(norm, r"\bremaster(ed)?\b")
    open_matte = _has(norm, r"\bopen[\s-]*matte\b")

    # ── Year ────────────────────────────────────────────────────────────────
    year = None
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", norm)
    if year_match:
        year = int(year_match.group(1))

    # ── Release group ───────────────────────────────────────────────────────
    # Convention: trailing "-GROUP" on the stem, right before the container
    # extension. e.g. "...H.265-BYNDR.mkv" → "BYNDR".
    #
    # Caveat: os.path.splitext can't be used here. On a name like
    # "...H.265-BYNDR" (no real extension) it splits on the last dot and
    # treats ".265-BYNDR" as the extension, which destroys the group token.
    # We strip only known *video* extensions, then match the trailing -GROUP.
    KNOWN_EXTS = (".mkv", ".mp4", ".m2ts", ".ts", ".hevc", ".h265")
    stem = raw
    for ext_candidate in KNOWN_EXTS:
        if stem.lower().endswith(ext_candidate):
            stem = stem[: -len(ext_candidate)]
            break
    group_match = re.search(r"-([A-Za-z0-9_\.]{2,})$", stem)
    group = group_match.group(1) if group_match else None
    # Guard: if the captured "group" is actually a codec/resolution token,
    # reject it (e.g. "DL", "265" from "WEB-DL" or "H-265").
    if group and group.lower() in {"hd", "dl", "rip", "ma", "265", "264", "h.265", "h.264"}:
        group = None

    # ── Title (best-effort) ─────────────────────────────────────────────────
    title = None
    if year_match:
        # Everything before the year token, dots → spaces.
        before_year = raw[:year_match.start()].rstrip(". _-")
        title = before_year.replace(".", " ").replace("_", " ").strip()

    return {
        "platform":     platform,
        "source":       source,
        "resolution":   resolution,
        "codec":        codec,
        "container":    container,
        "audio_codec":  audio_codec,
        "channels":     channels,
        "atmos":        atmos,
        "dts_x":        dts_x,
        "hdr10":        hdr10,
        "hdr10_plus":   hdr10_plus,
        "hdr_generic":  hdr_generic,
        "hlg":          hlg,
        "dolby_vision": dolby_vision,
        "imax":         imax,
        "hybrid":       hybrid,
        "repack":       repack,
        "proper":       proper,
        "extended":     extended,
        "directors_cut": directors,
        "remastered":   remastered,
        "open_matte":   open_matte,
        "year":         year,
        "release_group": group,
        "title":        title,
    }


def _empty() -> dict[str, Any]:
    return {
        "platform":      None, "source":       None, "resolution":  None,
        "codec":         None, "container":    None, "audio_codec": None,
        "channels":      None, "atmos":        False, "dts_x":       False,
        "hdr10":         False, "hdr10_plus":  False, "hdr_generic": False,
        "hlg":           False, "dolby_vision": False, "imax":       False,
        "hybrid":        False, "repack":      False, "proper":      False,
        "extended":      False, "directors_cut": False, "remastered": False,
        "open_matte":    False, "year":        None,  "release_group": None,
        "title":         None,
    }


# ── Convenience: build display badges ────────────────────────────────────────

def badges_from_intel(intel: dict[str, Any]) -> list[str]:
    """Return a stable, ordered list of UI badge strings for an intel dict."""
    out: list[str] = []
    if intel.get("resolution"):     out.append(intel["resolution"])
    if intel.get("source"):         out.append(intel["source"])
    if intel.get("platform"):       out.append(intel["platform"])
    if intel.get("dolby_vision"):   out.append("DV")
    if intel.get("hdr10_plus"):     out.append("HDR10+")
    elif intel.get("hdr10"):        out.append("HDR10")
    elif intel.get("hlg"):          out.append("HLG")
    elif intel.get("hdr_generic"):  out.append("HDR")
    if intel.get("atmos"):          out.append("Atmos")
    if intel.get("dts_x"):          out.append("DTS:X")
    if intel.get("imax"):           out.append("IMAX")
    if intel.get("hybrid"):         out.append("Hybrid")
    if intel.get("repack"):         out.append("REPACK")
    if intel.get("proper"):         out.append("PROPER")
    if intel.get("extended"):       out.append("Extended")
    if intel.get("directors_cut"): out.append("Director's Cut")
    if intel.get("remastered"):     out.append("Remastered")
    if intel.get("open_matte"):     out.append("Open Matte")
    return out


if __name__ == "__main__":
    # Quick sanity check against the two Avatar magnets.
    samples = [
        "Avatar.Fire.and.Ash.2025.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-BYNDR.mkv",
        "Avatar.Fire.And.Ash.2025.2160p.AMZN.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM.mp4",
    ]
    import json as _json
    for s in samples:
        print(s)
        print(_json.dumps(parse_release_name(s), indent=2))
        print("BADGES:", badges_from_intel(parse_release_name(s)))
        print()
