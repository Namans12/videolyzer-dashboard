# Testing & Verification

The project has no formal test suite. This doc documents the manual recipes
that exercise the system end-to-end, plus the cheap module-level smoke tests
each new module ships with.

## Module smoke tests

Every parsing/scoring module has an `if __name__ == "__main__":` block that
prints output for known-good inputs. Run them before opening a PR:

```bash
python filename_intel.py       # parses 2 Avatar samples, prints intel dict + badges
python -c "from verification import verify_release; ..."  # see below
python -c "from comparison import compare_from_analyses; ..."
```

### Verification engine — good vs bad case

```python
from filename_intel import parse_release_name
from verification import verify_release

intel = parse_release_name("Avatar.2025.2160p.iT.WEB-DL.DV.Atmos.H.265-X.mkv")

good = {
    "dv_profile": "8.1", "rpu": "Yes", "bl": "Yes", "el": "No",
    "hdr": "Dolby Vision | dvhe.08.06 | HDR10",
    "bitrate_mbps": 22.5,
    "audio_details": "E-AC3 JOC, 5.1, 768 kbps",
    "media_facts": [
        {"label": "Video Codec", "value": "HEVC Main 10"},
        {"label": "Container", "value": "Matroska"},
        {"label": "Resolution", "value": "3840x2160"},
    ],
}
print(verify_release(good, intel))  # → trust_score 100, no flags

bad = dict(good, dv_profile="None", rpu="No", hdr="HDR10",
           audio_details="AAC stereo 192 kbps")
print(verify_release(bad, intel))   # → flags: fake_dv (error), fake_atmos (warn)
```

## End-to-end: single magnet

```bash
# Start server
uvicorn main:app --reload --port 8000

# Trigger
curl -X POST http://127.0.0.1:8000/magnet/ \
  -H 'Content-Type: application/json' \
  -d '{"magnet":"magnet:?xt=urn:btih:..."}'
# → {"job_id": "abc123..."}

# Poll
curl http://127.0.0.1:8000/job/abc123...
# Or stream:
curl -N http://127.0.0.1:8000/job/abc123.../events
```

**Expected timeline (typical 4K REMUX magnet):**

```
0s    Job started, libtorrent session up
0-30s Metadata handshake via DHT
30-60s Header pieces downloading (32-96 MB)
60-90s MediaInfo + ffprobe parse
90s+  dovi_tool RPU scan (if HEVC + DV signalled)
done  ~90-180s total
```

If you don't see "Metadata received" within 90s, the torrent has no live
peers — try a different magnet.

## End-to-end: multi-magnet compare

```bash
curl -X POST http://127.0.0.1:8000/compare-magnets/ \
  -H 'Content-Type: application/json' \
  -d @magnets.json
# magnets.json: {"magnets": ["magnet:?...", "magnet:?..."]}
```

The job record's `comparison` field has the side-by-side matrix. Pretty-print:

```bash
curl -s http://127.0.0.1:8000/job/<id> | python -m json.tool | less
```

## Test fixtures

The two Avatar magnets in `CLAUDE.md` are good benchmarks:

| Magnet | Group | Source     | DV | HDR10+ | Atmos | Container |
|--------|-------|------------|----|--------|-------|-----------|
| BYNDR  | BYNDR | iT WEB-DL  | Y  | N      | Y     | MKV       |
| BTM    | BTM   | AMZN WEB-DL | Y | Y      | Y     | MP4       |

Expected comparison winner: **BTM** — same DV profile, similar bitrate,
plus HDR10+ + MP4 container. Trust score should be 100/100 for both
(claims match stream metadata).

## What to check after a backend change

1. `python -c "from main import app; print(len(app.routes))"` — imports clean.
2. Hit `GET /health/` — server boots.
3. Run the module's `__main__` smoke test if you touched a parser/scorer.
4. Run a single-magnet job end-to-end (use a small public-domain torrent
   if you don't want to wait on a REMUX).

## What to check after a frontend change

1. `npx tsc --noEmit -p tsconfig.app.json` — TS compiles.
2. `npm run dev` — Vite starts, no console errors on page load.
3. Hit each of: single file upload, path input, single magnet, compare
   magnets, results render.

## Known flake sources

- **libtorrent DHT cold start.** First magnet of a new server process can
  take 30s+ for DHT bootstrap. Subsequent magnets reuse the routing table
  via lt internals and warm up faster.
- **Windows Defender on `uploads/`.** Real-time scanning can slow large
  writes. Adding `uploads/` to AV exclusions makes uploads 2-3× faster.
- **MediaInfo on truncated files.** Exit code 1, but stdout is valid JSON.
  We handle this — see `run_mediainfo`.
- **dovi_tool on non-HEVC.** Crashes — guarded by `should_run_dovi_scan`.
  If you see "ffmpeg could not prepare the DV sample stream" on an HEVC
  file, the head bytes likely don't include enough frames; try without
  fast mode.
