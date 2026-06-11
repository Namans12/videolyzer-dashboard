"""Multi-release comparison + recommendation engine.

Takes N enriched release records (analysis + intel + verification) and:

  1. Builds a side-by-side `comparison_matrix` — rows are features
     (Resolution, Codec, HDR, DV Profile, Audio, Bitrate, Source, …),
     columns are releases.
  2. Picks a `winner` with a reason list.
  3. Computes a `composite_score` per release that combines:
        - tv_score (from analysis.py: TV-aware quality)
        - trust_score (from verification.py: claim integrity)
        - source-tier bonus
        - HDR10+/DV/Atmos feature presence
  4. Generates a `recommendation` blurb per release.

The output is JSON-serializable and stable across runs.
"""

from __future__ import annotations

from typing import Any

from filename_intel import parse_release_name, badges_from_intel
from verification import verify_release


# ── Source tier weighting ────────────────────────────────────────────────────
SOURCE_RANK: dict[str, int] = {
    "REMUX":         100,
    "UHD BluRay":    90,
    "BluRay":        70,
    "WEB-DL":        60,
    "WEB":           50,
    "WEBRip":        40,
    "HDTV":          30,
    "DVDRip":        20,
    "DVD":           20,
    "CAM":           5,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe(d: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    value = d.get(key)
    return default if value in (None, "", "Unknown") else value


def _bitrate(release: dict[str, Any]) -> float:
    return float(release.get("analysis", {}).get("bitrate_mbps") or 0)


def _tv_score(release: dict[str, Any]) -> int:
    return int(release.get("analysis", {}).get("tv_score") or 0)


def _trust(release: dict[str, Any]) -> int:
    return int(release.get("verification", {}).get("trust_score") or 0)


# ── Enrichment: wrap an analysis dict with intel + verification ─────────────

def enrich_release(analysis: dict[str, Any], display_name: str | None = None,
                    magnet_uri: str | None = None) -> dict[str, Any]:
    """Given a single analysis_result (from analyze_file or a magnet job),
    parse the filename and run verification. Return a flat release record:

        {
          "name":          original display name,
          "magnet_uri":    source magnet URI (None for local-file analyses),
          "analysis":      the raw analysis result,
          "intel":         filename_intel.parse_release_name output,
          "verification":  verify_release output,
          "badges":        list[str] for UI display,
        }

    `magnet_uri` is threaded through verbatim so the frontend can render
    a clickable magnet link next to each release without needing a separate
    lookup by index.
    """
    name = display_name or analysis.get("file") or analysis.get("path") or ""
    intel = parse_release_name(name)
    verification = verify_release(analysis, intel)
    badges = badges_from_intel(intel)

    return {
        "name":         name,
        "magnet_uri":   magnet_uri,
        "analysis":     analysis,
        "intel":        intel,
        "verification": verification,
        "badges":       badges,
    }


# ── Composite scoring ────────────────────────────────────────────────────────

def composite_score(release: dict[str, Any]) -> tuple[int, list[str]]:
    """Combine TV quality, trust, source tier, and feature presence into a single
    0–100 score. Return (score, reason_list).
    """
    analysis     = release.get("analysis", {})
    intel        = release.get("intel", {})
    verification = release.get("verification", {})

    reasons: list[str] = []
    score = 0

    tv = int(analysis.get("tv_score") or 0)
    score += int(tv * 0.45)
    reasons.append(f"TV quality {tv}/100 (weighted)")

    trust = int(verification.get("trust_score") or 0)
    score += int(trust * 0.25)
    reasons.append(f"Trust {trust}/100")

    source = (intel.get("source") or "").upper()
    src_rank = SOURCE_RANK.get(source, 30)
    score += int(src_rank * 0.10)
    if source:
        reasons.append(f"Source tier: {source}")

    # Feature bonuses — must reflect what the *target device* can actually
    # use. Sony Bravia 8 II is the only device this stack scores against
    # right now (see analysis.BRAVIA_8_II), and that TV:
    #   - has no HDR10+ decoder → HDR10+ presence does not improve playback
    #     (it falls back to the HDR10 base layer), so no bonus
    #   - can't render Profile 7 EL → already handled in score_for_tv()
    #   - decodes Atmos via eARC, plays DV natively, etc. → bonuses kept
    # If we ever add a second device profile, lift this map into a per-
    # device feature table instead of hard-coding the absence here.
    feature_bonus = 0
    if analysis.get("bitrate_mbps", 0) >= 50: feature_bonus += 5
    elif analysis.get("bitrate_mbps", 0) >= 25: feature_bonus += 3
    if intel.get("dolby_vision"):              feature_bonus += 4
    # No HDR10+ bonus — target TV doesn't support it.
    if intel.get("atmos"):                     feature_bonus += 3
    if intel.get("dts_x"):                     feature_bonus += 2
    if intel.get("hybrid"):                    feature_bonus += 2
    if intel.get("imax"):                      feature_bonus += 1
    score += min(feature_bonus, 20)
    if feature_bonus:
        reasons.append(f"+{feature_bonus} feature bonus")

    # Strong errors should hard-cap the score.
    if verification.get("error_count", 0) > 0:
        score = min(score, 50)
        reasons.append("Capped at 50 due to verified mismatches")

    score = max(0, min(100, score))
    return score, reasons


# ── Side-by-side matrix ──────────────────────────────────────────────────────

# Each row: (label, key_path_or_callable, formatter)
def _format_yes_no(v: Any) -> str:
    if v is True:  return "Yes"
    if v is False: return "No"
    return "—"


def _format_or_dash(v: Any) -> str:
    if v in (None, "", "Unknown"):
        return "—"
    return str(v)


def _get_path(release: dict[str, Any], *path: str) -> Any:
    cur: Any = release
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


COMPARISON_ROWS = [
    ("Resolution",       ("intel", "resolution"),         _format_or_dash),
    ("Source",           ("intel", "source"),             _format_or_dash),
    ("Platform",         ("intel", "platform"),           _format_or_dash),
    ("Container",        ("analysis", "media_facts"),     None),  # special handling
    ("Video Codec",      ("analysis", "media_facts"),     None),  # special handling
    ("Bit Depth",        ("analysis", "bit_depth"),       lambda v: f"{v}-bit" if v else "—"),
    ("Bitrate",          ("analysis", "bitrate_mbps"),    lambda v: f"{v:.1f} Mbps" if v else "—"),
    ("HDR Format",       ("analysis", "hdr"),             _format_or_dash),
    ("Dolby Vision",     ("intel", "dolby_vision"),       _format_yes_no),
    ("DV Profile",       ("analysis", "dv_profile"),      _format_or_dash),
    ("DV Layer",         ("analysis", "layer_variant"),   _format_or_dash),
    ("HDR10+",           ("intel", "hdr10_plus"),         _format_yes_no),
    ("Atmos",            ("intel", "atmos"),              _format_yes_no),
    ("DTS:X",            ("intel", "dts_x"),              _format_yes_no),
    ("Audio",            ("analysis", "audio_details"),   _format_or_dash),
    ("IMAX",             ("intel", "imax"),               _format_yes_no),
    ("Hybrid",           ("intel", "hybrid"),             _format_yes_no),
    ("Release Group",    ("intel", "release_group"),      _format_or_dash),
    ("File Size",        ("analysis", "file_size_gb"),    lambda v: f"{v:.2f} GB" if v else "—"),
    ("Duration",         ("analysis", "duration_min"),    lambda v: f"{v:.1f} min" if v else "—"),
    ("Trust Score",      ("verification", "trust_score"), lambda v: f"{v}/100"),
    ("TV Score",         ("analysis", "tv_score"),        lambda v: f"{v}/100"),
]


def _row_value(release: dict[str, Any], path: tuple[str, ...], row_label: str,
               formatter: Any) -> str:
    # Special handling for Container / Video Codec — pull from media_facts list.
    if row_label in ("Container", "Video Codec"):
        for fact in release.get("analysis", {}).get("media_facts", []) or []:
            if fact.get("label") == row_label:
                return _format_or_dash(fact.get("value"))
        return "—"
    raw = _get_path(release, *path)
    if formatter is None:
        return _format_or_dash(raw)
    try:
        return formatter(raw)
    except Exception:
        return _format_or_dash(raw)


def build_comparison_matrix(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a side-by-side comparison matrix.

    Returns:
        {
          "columns": [ {name, badges, composite_score}, ... ],
          "rows":    [ {label, values: [str per column], highlight: int|None}, ... ],
        }

    `highlight` marks the winning column index for that row (or None if tied).
    """
    columns = [
        {
            "name":              r["name"],
            "magnet_uri":        r.get("magnet_uri"),
            "badges":            r["badges"],
            "composite_score":   composite_score(r)[0],
            "trust_score":       _trust(r),
            "tv_score":          _tv_score(r),
            "verification_summary": r["verification"]["summary"],
            "error_count":       r["verification"]["error_count"],
            "warn_count":        r["verification"]["warn_count"],
        }
        for r in releases
    ]

    rows: list[dict[str, Any]] = []
    for label, path, formatter in COMPARISON_ROWS:
        values = [_row_value(r, path, label, formatter) for r in releases]
        highlight = _pick_row_winner(label, releases, values)
        rows.append({"label": label, "values": values, "highlight": highlight})

    return {"columns": columns, "rows": rows}


def _pick_row_winner(label: str, releases: list[dict[str, Any]],
                      values: list[str]) -> int | None:
    """Decide which column 'wins' a given comparison row.

    For Yes/No rows: any "Yes" beats "No". Ties → None.
    For numeric rows: highest wins (bitrate, scores, size).
    For categorical rows (Source): rank via SOURCE_RANK.
    For everything else: no highlight.

    NOTE: HDR10+ is intentionally NOT in the winner-highlight set. The target
    device (Bravia 8 II) can't decode HDR10+ — it falls back to HDR10 — so
    highlighting an HDR10+ release as "winning" that row would be misleading.
    The row still displays Yes/No (it's true, useful info), just without the
    green "this one is better" highlight. Same rationale as dropping the
    HDR10+ bonus from composite_score.
    """
    if label in ("Dolby Vision", "Atmos", "DTS:X", "IMAX", "Hybrid"):
        yes_idx = [i for i, v in enumerate(values) if v == "Yes"]
        if len(yes_idx) == 1: return yes_idx[0]
        return None

    if label == "Bitrate":
        nums = [(_bitrate(r), i) for i, r in enumerate(releases)]
        nums = [(b, i) for b, i in nums if b > 0]
        if not nums: return None
        nums.sort(reverse=True)
        if len(nums) > 1 and nums[0][0] == nums[1][0]: return None
        return nums[0][1]

    if label in ("Trust Score", "TV Score"):
        key = "trust_score" if label == "Trust Score" else "tv_score"
        scores = [(int(r.get("verification" if key == "trust_score" else "analysis", {})
                    .get(key, 0)), i) for i, r in enumerate(releases)]
        scores.sort(reverse=True)
        if len(scores) > 1 and scores[0][0] == scores[1][0]: return None
        return scores[0][1]

    if label == "Source":
        ranks = [(SOURCE_RANK.get((r["intel"].get("source") or "").upper(), 0), i)
                 for i, r in enumerate(releases)]
        ranks.sort(reverse=True)
        if ranks[0][0] == 0: return None
        if len(ranks) > 1 and ranks[0][0] == ranks[1][0]: return None
        return ranks[0][1]

    if label == "File Size":
        sizes = [(float(r["analysis"].get("file_size_gb") or 0), i) for i, r in enumerate(releases)]
        sizes.sort(reverse=True)
        if sizes[0][0] == 0: return None
        if len(sizes) > 1 and sizes[0][0] == sizes[1][0]: return None
        return sizes[0][1]

    return None


# ── Winner selection ─────────────────────────────────────────────────────────

def pick_winner(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the recommended release. Return {winner_index, winner_name, reasons}.

    TV-SCORE FIRST. This stack scores against one device (Sony Bravia 8 II), so
    the recommendation must be the best release *for that TV* — not the highest
    raw-quality source. A P7 REMUX has a huge composite (source tier + bitrate +
    trust) but its enhancement layer is wasted on this TV, so its tv_score is
    lower than an ideal P8.1; tv_score must therefore decide the winner.
    Composite/trust/bitrate only break TV-score ties. (See memory:
    bravia-ranking-priorities — ranking is TV-score-driven for this device.)
    """
    if not releases:
        return {"winner_index": None, "winner_name": None, "reasons": []}

    scored = [(composite_score(r), i, r) for i, r in enumerate(releases)]
    # TV score first; tiebreak by composite, then trust, then bitrate.
    scored.sort(key=lambda t: (_tv_score(t[2]), t[0][0], _trust(t[2]), _bitrate(t[2])),
                reverse=True)
    (best_score, best_reasons), best_idx, best = scored[0]

    reasons: list[str] = []
    reasons.append(
        f"Best TV score: {_tv_score(best)}/100 for the Bravia 8 II "
        f"(of {len(releases)} releases). Composite {best_score}/100."
    )

    # If a higher-RAW-quality release exists but lost on TV score, say so —
    # this is the P7-REMUX-vs-P8.1 case the user hit. Surfaces *why* the
    # bigger/higher-composite file wasn't recommended.
    higher_q = [r for (sc, _i, r) in scored[1:] if sc[0] > best_score]
    if higher_q:
        hq = higher_q[0]
        reasons.append(
            f"A higher raw-quality release exists "
            f"({hq['intel'].get('source') or 'source'} · DV {hq['analysis'].get('dv_profile') or '—'}, "
            f"composite {composite_score(hq)[0]}/100) but it scores lower for this "
            f"TV (TV {_tv_score(hq)} vs {_tv_score(best)}) — e.g. an unused DV "
            f"enhancement layer or a less compatible container."
        )

    # Add feature-specific reasons relative to runner-up if any.
    if len(scored) > 1:
        runner_up = scored[1][2]
        # HDR10+ is detected and reported, but it's NOT a deciding factor on
        # Bravia 8 II (the TV doesn't support HDR10+ — it falls back to HDR10).
        # We surface a neutral informational note instead of treating it as
        # a winning advantage. If you add a device that does support HDR10+,
        # promote this back to a positive reason for that device.
        if best["intel"].get("hdr10_plus") and not runner_up["intel"].get("hdr10_plus"):
            reasons.append("Carries HDR10+ metadata (Bravia 8 II will fall back to HDR10).")
        if best["intel"].get("dolby_vision") and not runner_up["intel"].get("dolby_vision"):
            reasons.append("Only release with Dolby Vision.")
        if best["intel"].get("atmos") and not runner_up["intel"].get("atmos"):
            reasons.append("Only release with Atmos audio.")
        if _bitrate(best) > _bitrate(runner_up) * 1.2:
            reasons.append(f"Higher bitrate: {_bitrate(best):.1f} vs {_bitrate(runner_up):.1f} Mbps.")
        if _trust(best) > _trust(runner_up) + 10:
            reasons.append(f"Higher trust score: {_trust(best)} vs {_trust(runner_up)}.")
        best_source_rank = SOURCE_RANK.get((best["intel"].get("source") or "").upper(), 0)
        ru_source_rank   = SOURCE_RANK.get((runner_up["intel"].get("source") or "").upper(), 0)
        if best_source_rank > ru_source_rank:
            reasons.append(f"Better source tier: {best['intel'].get('source')} > {runner_up['intel'].get('source')}.")

    if best["verification"]["error_count"] > 0:
        reasons.append(f"⚠ Even the winner has {best['verification']['error_count']} verification error(s).")

    return {
        "winner_index": best_idx,
        "winner_name":  best["name"],
        "composite_score": best_score,
        "reasons":      reasons,
    }


# ── Per-release recommendation blurb ─────────────────────────────────────────

def build_release_recommendation(release: dict[str, Any]) -> str:
    """One-line recommendation for a single release."""
    intel = release["intel"]
    analysis = release["analysis"]
    verification = release["verification"]

    parts: list[str] = []

    if verification["error_count"] > 0:
        parts.append("⚠ Avoid — claim mismatches detected.")
    else:
        # Feature summary
        feats: list[str] = []
        if intel.get("dolby_vision"):  feats.append("DV")
        if intel.get("hdr10_plus"):    feats.append("HDR10+")
        if intel.get("atmos"):         feats.append("Atmos")
        if intel.get("dts_x"):         feats.append("DTS:X")
        if intel.get("imax"):          feats.append("IMAX")
        feats_str = "+".join(feats) if feats else "SDR"
        bitrate = analysis.get("bitrate_mbps", 0)
        parts.append(f"{intel.get('source') or 'Unknown source'} · {feats_str} · {bitrate:.1f} Mbps")

    rec = analysis.get("recommendation")
    if rec:
        parts.append(rec)

    return " — ".join(parts)


# ── Main entry point ─────────────────────────────────────────────────────────

def compare_releases(releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Top-level: take enriched releases, return the full comparison payload.

    Each input release should already be the dict from enrich_release().
    """
    if not releases:
        return {
            "releases":           [],
            "comparison_matrix":  {"columns": [], "rows": []},
            "winner":             {"winner_index": None, "winner_name": None, "reasons": []},
        }

    # Attach composite scores + per-release recommendations to each record.
    for r in releases:
        score, reasons = composite_score(r)
        r["composite_score"]    = score
        r["score_reasons"]      = reasons
        r["release_recommendation"] = build_release_recommendation(r)

    matrix = build_comparison_matrix(releases)
    winner = pick_winner(releases)

    return {
        "releases":          releases,
        "comparison_matrix": matrix,
        "winner":            winner,
    }


# ── Convenience: build from raw analysis results ─────────────────────────────

def compare_from_analyses(
    analyses: list[dict[str, Any]],
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Convenience: take raw analyze_file() outputs, enrich + compare in one call."""
    enriched = []
    for i, analysis in enumerate(analyses):
        name = names[i] if names and i < len(names) else None
        enriched.append(enrich_release(analysis, display_name=name))
    return compare_releases(enriched)
