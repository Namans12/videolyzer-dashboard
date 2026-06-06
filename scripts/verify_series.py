"""End-to-end series comparison on two real magnets (The Boys S05).

Calls fetch_magnet_metadata DIRECTLY (synchronous — no hard-timeout daemon
thread that would orphan probing and drop results). Each torrent's result is
cached to JSON so a heavy pack can be completed across multiple runs without
re-downloading: delete scripts/_cache_*.json to force a fresh fetch.

Temp is redirected to D: for disk safety. A short piece-timeout means BTM
(huge ~11 GB MP4 episodes) probes only the episodes whose moov tail arrived —
enough overlap with Vyndros to exercise the per-episode comparison.
"""
from __future__ import annotations
import os, sys, json, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SAFE_TMP = r"D:\vlztmp"
os.makedirs(SAFE_TMP, exist_ok=True)
tempfile.tempdir = SAFE_TMP

import magnet                                          # noqa: E402
magnet.METADATA_TIMEOUT_S = 90
magnet.PIECE_TIMEOUT_S = 180   # download cutoff; probing then runs untimed
from magnet import fetch_magnet_metadata               # noqa: E402
from series_compare import is_series_comparison, build_series_comparison  # noqa: E402

A = ("magnet:?xt=urn:btih:9D8C082CE1DEA6FA5E4F8FE12E657541A25CB3B5"
     "&dn=The.Boys.S05.2160p.10bit.AMZN.WEBRip.DDP5.1.HEVC.x265-Vyndros")
B = ("magnet:?xt=urn:btih:215A94273CF32220B7991AF0E9D0573AA7B8F2BC"
     "&dn=The.Boys.S05.2160p.WEB-DL.DV.HDR10%2B.MULTi.LAT.ITA.FRE.HINDI.Atmos.H265.MP4-BTM")


def get_one(i: int, m: str) -> dict:
    cache = os.path.join(HERE, f"_cache_{i}.json")
    if os.path.isfile(cache):
        with open(cache, encoding="utf-8") as f:
            rec = json.load(f)
        print(f"  (loaded {len(rec.get('analyses', []))} analyzed file(s) from cache)", flush=True)
        return rec
    t0 = time.time()
    try:
        out = fetch_magnet_metadata(
            m, skip_dovi_scan=True,
            emit=lambda msg: print("  [emit]", msg, flush=True),
            cancel_check=lambda: False,
            listen_port=6881 + i, enable_hdr10plus=False,
        )
        rec = {"index": i, "magnet": m, "status": "done",
               "torrent": {"name": out.get("torrent_name"), "info_hash": out.get("info_hash")},
               "files": out.get("files", []), "analyses": out.get("analyses", [])}
        print(f"  -> {len(rec['analyses'])} analyzable file(s) in {time.time()-t0:.0f}s", flush=True)
    except Exception as e:
        print("  ERROR:", e, flush=True)
        rec = {"index": i, "magnet": m, "status": "error", "error": str(e), "analyses": []}
    if rec.get("analyses"):                 # only cache useful results
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    return rec


per_magnet = []
for i, m in enumerate([A, B]):
    print(f"\n========== MAGNET #{i+1} ==========", flush=True)
    rec = get_one(i, m)
    per_magnet.append(rec)
    for a in rec.get("analyses", []):
        cont = next((f.get("value") for f in a.get("media_facts", [])
                     if f.get("label") == "Container"), "?")
        print(f"     {a.get('file')}", flush=True)
        print(f"        dvp={a.get('dv_profile')} cont={cont} br={a.get('bitrate_mbps')} "
              f"hdr10+={a.get('hdr10_plus_status')} size={a.get('file_size_gb')}GB", flush=True)

print("\n========== COMPARISON ==========", flush=True)
print("series mode:", is_series_comparison(per_magnet), flush=True)
if is_series_comparison(per_magnet):
    ec = build_series_comparison(per_magnet)
    print("\nTORRENTS:")
    for k, t in enumerate(ec["torrents"]):
        print(f"  #{k+1} idx{t['index']} eps={t['episode_count']} wins={t['win_count']} "
              f"avg={t['avg_score']}  {(t['name'] or '')[:55]}")
    print("\nPER-EPISODE:")
    for e in ec["episodes"]:
        scores = {}
        for j, tidx in enumerate(e["present_in"]):
            col = e["comparison"]["comparison_matrix"]["columns"][j]
            scores[tidx] = col["composite_score"]
        print(f"  {e['key']:10} present={e['present_in']} missing={e['missing']} "
              f"contested={e['contested']} winner_torrent={e['winner_torrent_index']} scores={scores}")
    s = ec["summary"]
    print("\nSEASON SUMMARY:")
    print(f"  winner_index={s['winner_index']}  name={(s['winner_name'] or '')[:60]}")
    for r in s["reasons"]:
        print("   -", r)
    print(f"  total_episodes={s['total_episodes']} contested={s['contested_count']}")
else:
    print("Not series mode — need ≥2 episodes analyzed in ≥2 torrents.")
