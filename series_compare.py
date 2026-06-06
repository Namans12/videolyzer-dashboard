"""Series-aware comparison.

The movie path collapses each torrent to one representative file. For TV /
anime season packs that throws away the point: you want to know, episode by
episode, which release is better, and which torrent wins the season overall.

This module:
  1. Detects whether the torrents being compared are episodic season packs.
  2. Indexes each torrent's files by episode key (SxxExx) via filename_intel.
  3. Runs the existing `compare_releases` engine PER EPISODE across the
     torrents that contain that episode.
  4. Aggregates a season summary: per-torrent win counts + average composite
     score, an overall winner, and notes on episodes only one torrent has.

Output is JSON-serializable and shaped to sit alongside the movie-mode
`comparison` payload in a /compare-magnets/ job record.
"""

from __future__ import annotations

from typing import Any

from filename_intel import parse_episode, episode_key
from comparison import enrich_release, compare_releases


# ── Detection ────────────────────────────────────────────────────────────────

def torrent_episode_keys(analyses: list[dict[str, Any]]) -> set[str]:
    """Distinct episode keys among a torrent's analyzed files."""
    keys: set[str] = set()
    for a in analyses:
        k = episode_key(parse_episode(a.get("file") or a.get("path") or ""))
        if k:
            keys.add(k)
    return keys


def is_series_comparison(per_magnet: list[dict[str, Any]]) -> bool:
    """True when at least two torrents look like episodic season packs.

    A torrent qualifies as episodic when ≥2 of its files resolve to distinct
    episode keys. Requiring two such torrents avoids flipping into series mode
    for a lone multi-part movie or a single stray SxxExx false-positive.
    """
    episodic = sum(1 for rec in per_magnet
                   if rec.get("status") == "done"
                   and len(torrent_episode_keys(rec.get("analyses", []))) >= 2)
    return episodic >= 2


# ── Per-torrent episode index ────────────────────────────────────────────────

def _index_episodes(rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """episode_key -> enriched release for one torrent's analyses.

    On duplicate keys within a torrent (rare — e.g. a proper + repack of the
    same episode), keep the larger file.
    """
    name = (rec.get("torrent") or {}).get("name") or rec.get("magnet") or ""
    out: dict[str, dict[str, Any]] = {}
    for a in rec.get("analyses", []):
        ep = parse_episode(a.get("file") or a.get("path") or "")
        key = episode_key(ep)
        if not key:
            continue
        rel = enrich_release(a, display_name=a.get("file") or name,
                             magnet_uri=rec.get("magnet"))
        rel["episode_key"] = key
        rel["season"] = ep["season"]
        rel["episode"] = ep["episode"]
        prev = out.get(key)
        if prev is None or _size(rel) > _size(prev):
            out[key] = rel
    return out


def _size(rel: dict[str, Any]) -> float:
    return float(rel.get("analysis", {}).get("file_size_gb") or 0)


# ── Main entry point ─────────────────────────────────────────────────────────

def build_series_comparison(per_magnet: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the per-episode + season-summary comparison payload.

    `per_magnet` entries are the job's per-magnet records (only those with
    status == "done" and a non-empty `analyses` list are used).

    Returns:
        {
          "torrents":  [ {index, name, magnet_uri, episode_count,
                          win_count, avg_score}, ... ],
          "episodes":  [ {key, season, episode, present_in[], missing[],
                          contested(bool), winner_torrent_index,
                          comparison{...}}, ... ],
          "summary":   {winner_index, winner_name, reasons[],
                        total_episodes, contested_count},
        }
    `present_in` / `missing` / `winner_torrent_index` use the ORIGINAL
    per-magnet `index` so the frontend can line columns up with its torrent
    list regardless of which torrents produced episodes.
    """
    done = [rec for rec in per_magnet
            if rec.get("status") == "done" and rec.get("analyses")]

    torrents: list[dict[str, Any]] = []
    maps: list[dict[str, dict[str, Any]]] = []
    for rec in done:
        emap = _index_episodes(rec)
        torrents.append({
            "index":         rec["index"],
            "name":          (rec.get("torrent") or {}).get("name") or rec.get("magnet"),
            "magnet_uri":    rec.get("magnet"),
            "episode_count": len(emap),
            "win_count":     0,
            "avg_score":     0.0,
        })
        maps.append(emap)

    # Union of episode keys across all torrents, ordered by season/episode.
    all_keys: set[str] = set()
    for emap in maps:
        all_keys.update(emap.keys())

    def _sort_key(k: str) -> tuple[int, int, str]:
        rel = next((maps[i][k] for i in range(len(maps)) if k in maps[i]), None)
        s = rel.get("season") if rel else None
        e = rel.get("episode") if rel else None
        return (s if s is not None else 99, e if e is not None else 0, k)

    ordered_keys = sorted(all_keys, key=_sort_key)

    episodes: list[dict[str, Any]] = []
    score_sum = [0.0] * len(torrents)
    score_cnt = [0] * len(torrents)

    for key in ordered_keys:
        present_pos = [i for i in range(len(maps)) if key in maps[i]]
        present_idx = [torrents[i]["index"] for i in present_pos]
        missing_idx = [torrents[i]["index"] for i in range(len(torrents))
                       if key not in maps[i]]

        rels = [maps[i][key] for i in present_pos]
        cmp = compare_releases(rels)  # mutates rels with composite_score

        # Accumulate average score for every torrent that has this episode.
        for local, i in enumerate(present_pos):
            score_sum[i] += float(rels[local].get("composite_score") or 0)
            score_cnt[i] += 1

        contested = len(present_pos) >= 2
        winner_torrent_index: int | None = None
        wi = cmp["winner"]["winner_index"]
        if contested and wi is not None:
            win_pos = present_pos[wi]
            torrents[win_pos]["win_count"] += 1
            winner_torrent_index = torrents[win_pos]["index"]

        episodes.append({
            "key":                  key,
            "season":               rels[0].get("season"),
            "episode":              rels[0].get("episode"),
            "present_in":           present_idx,
            "missing":              missing_idx,
            "contested":            contested,
            "winner_torrent_index": winner_torrent_index,
            "comparison":           cmp,
        })

    for i, t in enumerate(torrents):
        t["avg_score"] = round(score_sum[i] / score_cnt[i], 1) if score_cnt[i] else 0.0

    summary = _season_summary(torrents, episodes)
    return {"torrents": torrents, "episodes": episodes, "summary": summary}


def _season_summary(torrents: list[dict[str, Any]],
                    episodes: list[dict[str, Any]]) -> dict[str, Any]:
    contested = [e for e in episodes if e["contested"]]
    if not torrents:
        return {"winner_index": None, "winner_name": None, "reasons": [],
                "total_episodes": len(episodes), "contested_count": 0}

    # Rank torrents by head-to-head wins, then average composite score.
    order = sorted(range(len(torrents)),
                   key=lambda i: (torrents[i]["win_count"], torrents[i]["avg_score"]),
                   reverse=True)
    best = order[0]
    best_t = torrents[best]
    reasons: list[str] = []

    if contested:
        reasons.append(
            f"Wins {best_t['win_count']} of {len(contested)} head-to-head "
            f"episode(s)."
        )
    else:
        reasons.append("No episodes are present in more than one torrent — "
                       "no head-to-head winner; compare coverage instead.")

    if len(order) > 1:
        runner = torrents[order[1]]
        if best_t["avg_score"] > runner["avg_score"]:
            reasons.append(
                f"Higher average composite score: {best_t['avg_score']} vs "
                f"{runner['avg_score']}."
            )
        if best_t["episode_count"] != runner["episode_count"]:
            reasons.append(
                f"Episode coverage: {best_t['episode_count']} vs "
                f"{runner['episode_count']} episode(s)."
            )

    # Flag torrents that are missing episodes the season as a whole has.
    season_total = len({e["key"] for e in episodes})
    for t in torrents:
        if t["episode_count"] < season_total:
            reasons.append(
                f"'{_short(t['name'])}' is missing "
                f"{season_total - t['episode_count']} of {season_total} episode(s)."
            )

    # Winner only meaningful if there's at least one contested episode OR a
    # clear coverage/score lead.
    winner_index = best_t["index"] if (contested or best_t["avg_score"] > 0) else None
    return {
        "winner_index":    winner_index,
        "winner_name":     best_t["name"] if winner_index is not None else None,
        "reasons":         reasons,
        "total_episodes":  season_total,
        "contested_count": len(contested),
    }


def _short(name: str | None, n: int = 48) -> str:
    name = name or ""
    return name if len(name) <= n else name[: n - 1] + "…"
