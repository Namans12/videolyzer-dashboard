# Testing & Verification

The backend has an automated pytest suite (`tests/`) plus the manual
end-to-end recipes below. The frontend has Vitest tests (see "Frontend tests").

## Frontend tests

Run `npm test` for the frontend suite, or `npm run test:watch` during development. Vitest uses jsdom and Testing Library for `src/**/*.test.{ts,tsx}`, through the same Vite/React Compiler plugins as the production build. Exact dev-tool versions are pinned in `package.json`; the installed jsdom requires Node 22.22.2 or a compatible newer supported version.

Tests import real suite fixtures from `tests/fixtures/suite/` only through `src/suite/testing/fixtures.ts`, which returns fresh clones. Never edit those JSON fixtures by hand. Fetch is always mocked through `src/suite/testing/fetchMock.ts`; an unexpected request fails the test even when application code catches it. Polling tests use fake timers, with timer advances wrapped in React `act`; scheduled handoffs settle through separate render/effect flushes. `src/security.test.ts` scans frontend sources for HTML injection, evaluated code and non-loopback URLs.

Run `npm run lint` and `npm run build` after frontend changes. The real-browser verification also checks the guarded backend against the installed suite, generated clips and the owner's explicitly authorized read-only file; unit tests never access owner media or start backend processes.

## Automated backend tests

One-time setup (from the repo root, Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Notes:

- `pytest.ini` sets `testpaths = tests`, `pythonpath = .`, `addopts = -ra`.
- Tests use fakes only (`analyze_file`, `run_magnet_job_threaded`); no real
  torrent, ffmpeg, MediaInfo or dovi_tool is started and libtorrent is not
  needed. Files are created under pytest's `tmp_path`; the real `uploads/`
  folder is never touched (`tests/conftest.py` redirects `main.UPLOAD_DIR`).
- The `client` fixture is `TestClient(main.app, base_url="http://localhost")`.
  FastAPI's TestClient runs background tasks to completion before the response
  returns, so HTTP tests assert final job states only.
- `tests/test_contract_shapes.py` freezes the current job-dict keys, SSE line
  format, route response keys and `rank_results` ordering. Later changes may
  only ADD keys; changing a frozen value must be a deliberate, documented change.

- `tests/test_docs_contract.py` keeps the README honest: every route of the app
  (the lazily included `/suite` router flattened, framework docs routes
  excluded) must be mentioned in `README.md`, and every environment variable
  read by the top-level `*.py` files (found by AST: `os.environ.get/[]`,
  `os.getenv`, `ENV_*` constants; never `.venv`, `tests`, `scripts`) must be
  named there. It also requires the frozen legacy section and every `/suite/`
  route in `docs/SUITE_INTEGRATION.md`. Adding an undocumented route or variable fails the suite.
- `tests/fixtures/suite/*.json` are real outputs of `python -m videosuite
  <cmd> --json` (tool_version 0.4.0) on generated 10-second clips; the paths in
  them were replaced by `C:\fixtures\suite`. `devices.json` was captured through
  `SuiteClient` against the Comparizer working tree.

### Live suite tests (opt-in)

`tests/test_live_suite.py` runs REAL `devices`, `version`, `score`, `score_partial` on
one tiny clip it generates under `tmp_path` with ffmpeg (PATH, or the suite's own
`ffmpeg\bin`/`tools\bin`; skipped when none is found). It is skipped unless
`VIDEOLYZER_LIVE_SUITE` is `1`. The suite's record cache is redirected into
`tmp_path` (`VC_CACHE_DIR`), and `VIDEOSUITE_SETTINGS` points at a nonexistent
settings file under `tmp_path`; no owner media or settings are touched. The four
live tests are skipped by default. No torrent is started.

```powershell
$env:VIDEOLYZER_LIVE_SUITE = "1"
# only when the suite is not the one in D:\VideoComparatorPro\setup\env.path:
$env:VIDEOSUITE_PYTHON = "D:\VideoComparatorPro\.venv\Scripts\python.exe"
$env:VIDEOSUITE_ROOT   = "D:\Video Comparizer"
.\.venv\Scripts\python.exe -m pytest tests/test_live_suite.py -q
```

### Regenerating suite fixtures

With the suite interpreter/root configured as above, run:

```powershell
.\.venv\Scripts\python.exe scripts/make_suite_fixtures.py
```

This generates three 10-second clips plus a partial probe and captures `devices`,
`version`, `score`, `score_partial`, `compare`, `rank` into `tests/fixtures/suite/`.
It isolates `VC_CACHE_DIR` and `VIDEOSUITE_SETTINGS` in a temporary directory,
normalizes paths to `C:\fixtures\suite` and removes its temporary files.
FFmpeg lookup: `FFMPEG` (existing file), PATH, then the suite's `ffmpeg\bin` or
`tools\bin`. Regeneration runs the real CLI/engine; regular tests use fakes.
Tests disable the startup check and forbid real `taskkill`; fake PIDs never reach
the real Job Object. Real Windows process-lifetime checks remain owner validation.

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
uvicorn main:app --host 127.0.0.1 --port 8000

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

1. `.\.venv\Scripts\python.exe -m pytest -q` — all tests pass.
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
