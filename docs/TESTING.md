# Testing & Verification

## Offline Part 1 verification

Current packaged identity is VideoSuite 0.6.0 / `offline_payload`, Python 3.11.9,
RTX 3050 Ti / CUDA 12.6. Validate the actual `release.json`, Dashboard component
manifest, both lock hashes, source snapshot identities and every file hash before
installed smoke; do not satisfy checks from a sibling checkout or developer PATH.
Use existing dependencies only: no installs/downloads/servers are authorized by
a routine task. The source setup examples below are separate manual development.

Every invocation needs a fresh owned scratch child and explicit overrides:
`VIDEOSUITE_SETTINGS`, `VIDEOSUITE_CALIBRATION`, `VC_CACHE_DIR`,
`VIDEOLYZER_DATA_ROOT`, `TORCH_HOME`, `HF_HOME`, `CLIP_CACHE_DIR`;
set `PIP_NO_INDEX=1`, `PIP_DISABLE_PIP_VERSION_CHECK=1`, `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`, `PYTHONDONTWRITEBYTECODE=1`, and
`VIDEOLYZER_SUITE_STARTUP_CHECK=0` for unit tests. For pytest the scratch `VIDEOLYZER_DATA_ROOT` must not exist yet (a smoke test asserts import creates nothing); for `npm test` (AppControls) it must exist. A missing
`VIDEOSUITE_SETTINGS` lets the owner's real settings leak into fixture pins. Never use real owner settings, Never use real owner settings,
calibration, reports, model caches or movies, including E: movie folders.
Comparizer commands always use
`C:\Users\naman\AppData\Local\Programs\Python\Python310\python.exe`;
Dashboard commands use `D:\Videolyzer\videolyzer-dashboard\.venv\Scripts\python.exe`.

L6 focused tests are `tests/test_app_payload.py tests/test_app_preflight.py
tests/test_app_host.py tests/test_suite_client.py`; run the full Dashboard backend
gate, `npm test`, `npm run lint`, `npm run build` too. Current frontend gate is
496 tests in 32 files; the earlier calibration baseline was 422 tests in 30 files.
Probe regressions prove Stop admits no child and running probes are cancellable.
Full admission regressions inject missing/corrupt models, required CUDA and tool
failures, assert no pixel worker/list/output creation, and keep metadata available.
Export with the fresh source snapshot and explicit build/revision/destination,
then Comparizer compiles with that exact artifact and finite acquisitions.json.
No runtime npm or online repair; production requires an owner-committed source pair.

L7 is compile/synthetic only. V-L3 separately authorizes a clean install, upgrade,
rollback and installed browser/CUDA/model tests in the owned
a fresh explicitly selected owned QA root. First verify
E: is fixed local with at least 35 GiB, then enforce actual expansion + 24 GiB QA
+ 3 GiB margin; remeasure before each phase. C: keeps 3 GiB; D: base budgets are
unchanged and measured copies/backups can raise them. Do not delete retained
rollback evidence to fit. Only this owned E: root may be touched, then removed
after the later QA tasks; NTFS app/IPC scratch stays separate. Public torrents
and owner media are not QA fixtures: use generated tiny files and the authorized
private local torrent fixture. No live tests/server until the task authorizes them.
Check all three Quit labels, tab-close/relaunch continuity, foreign-process
preservation and rollback before declaring installed success. A failed Job Object
assignment leaves documented best-effort crash cleanup; unit fakes do not prove
native health. Real-TV validation is owner-only, after dated backups of protected
owner suite/app data and the existing production install. README.md gives launch,
rollback and bootstrap/backend log paths.

Compile-channel regression coverage checks actual Inno output: release retains
the production AppId and normal per-user temp extraction, without a QA root;
QA has a different AppId, QA ONLY identity and separate shortcuts. Manifest,
compiler seal and provision channel must agree. QA's Python bootstrap never
executes an installer; pass a verified QA-owned copy via /QAPythonDir. Registry
preservation still needs the separate installed QA run; synthetic compile passes
do not claim that run. Rollback uses a fresh VIDEOLYZER_DATA_ROOT and the real
verifier, including failure preservation of pointers, runtime and shortcut bytes.

The backend has an automated pytest suite (`tests/`) plus the manual
end-to-end recipes below. The frontend has Vitest tests (see "Frontend tests").

## Frontend tests

### Private loopback libtorrent proof (Plan 4B final wave)

The Dashboard Python 3.11 venv has no libtorrent. Opt in with
`PLAN4B_LOOPBACK_PYTHON` naming an existing interpreter with the binding, such as
the system Python 3.10 with libtorrent 2.0.11; this installs nothing. From the
Dashboard checkout, after setting all scratch/offline overrides listed above:

```powershell
$env:PLAN4B_LOOPBACK_PYTHON = 'C:\Users\naman\AppData\Local\Programs\Python\Python310\python.exe'
& 'D:\Videolyzer\videolyzer-dashboard\.venv\Scripts\python.exe' -m pytest tests/test_private_loopback_swarm.py -q -s
```

The test starts an isolated helper with generated tiny MKV/MP4 files in a private
multi-video torrent. Seeder/downloader bind and connect only to `127.0.0.1`, with
an IP filter blocking every other address, DHT/bootstrap/LSD/UPnP/NAT-PMP/uTP off,
and no trackers or web seeds. Private torrents do not advertise ut_metadata, so
the helper supplies its generated metadata at the parse seam. It also admits
tiny clips past the 50 MiB sample guard and shrinks probe bounds to 64/32 KiB;
sizes, offsets, storage, pieces and transfer remain real. No public torrent
network, downloads, owner media or installed app are involved.

The actual threaded downloader and partial_facts path must retain seed-identical
head/tail bytes, request only selected probe pieces, leave all other files at zero
priority, emit real facts and Quick documents with Just Player primary and optional
alternatives, preserve facts on suite-budget exhaustion, and clean session/workdirs
on success, cancellation and piece timeout. It uses the real CLI through
`VIDEOSUITE_ROOT`/the selected existing interpreter. The helper pauses/removes all
sessions before exit. Normal pytest skips this proof unless explicitly opted in;
a skip is never integration evidence. Native dialog exit is separately tested in
a short-lived subprocess with a blocked fake dialog; installed native UI and EXE
QA remain separate.

Final backend regressions also cover JSON-only comparison POSTs, shared magnet
port leases on success/failure, cancel-before-replan reconnect, independent Overall
answers after device-settings changes, installed build provenance and bounded
marked-only orphan cleanup with a real NTFS junction refusal.

Run `npm test` for the frontend suite, or `npm run test:watch` during development. Vitest uses jsdom and Testing Library for `src/**/*.test.{ts,tsx}`, through the same Vite/React Compiler plugins as the production build. Exact dev-tool versions are pinned in `package.json`; the installed jsdom requires Node 22.22.2 or a compatible newer supported version.

The Plan 4A documentation verification run has **422 tests in 30 files**. Calibration
coverage under `src/suite/calibration/` includes document guards, exact requests,
store-backed answers, stale preview invalidation, kit planning/build polling, the
checklist and guided card, session confirmation and input limits, individual veto
confirmation, apply/undo and playback reports. App tests cover (since Plan 4B) the
four main tabs and the Advanced sub-tabs with keyboard navigation, lazy mounting with hidden-panel retention, stored-tab restoration,
the calibration capability gate on older suites and independent error boundaries.
Fetch-gated tests cover terminal polling and request guards; jsdom does not establish
real-browser layout, focus traversal or live installed-suite behavior. Those require
the separate real-browser QA pass.

Plan 4B adds `src/compare/` and `src/devices/` tests: the fixed TV/laptop/tablet order without management controls, default choices without hidden prior state, inline candidate validation, Full disabled for magnets, explicit magnet file choice, stale-plan refusal and a single confirmation on double click, reconnect of a real Just Player run with its alternatives, public-snapshot guards that refuse a leaked `reports_dir`, Quick labels without measured scores, requested panels only, coverage that blocks a whole-set winner, evidence words on every verdict, sandboxed reports and the Compare-first tabs with legacy tools under Advanced. They consume the real HTTP envelopes in `tests/fixtures/comparison` (emitted by the actual routes and workers; regenerate with `PLAN4B_REGEN_FIXTURES=1`) only through `src/compare/testing/fixtures.ts`, and the real suite documents in `tests/fixtures/suite4b`: never fabricated near-matches. A test that needs a state the emitters do not produce (a running job, a Full Device-only answer, an HTML report) derives it from a real envelope and says so.

Tests import real suite fixtures from `tests/fixtures/suite/` only through `src/suite/testing/fixtures.ts`, which returns fresh clones. Never edit those JSON fixtures by hand. Fetch is always mocked through `src/suite/testing/fetchMock.ts`; an unexpected request fails the test even when application code catches it. Polling tests use fake timers, with timer advances wrapped in React `act`; scheduled handoffs settle through separate render/effect flushes. `src/security.test.ts` scans frontend sources for HTML injection, evaluated code and non-loopback URLs.

Run `npm run lint` and `npm run build` after frontend changes. Separately authorized
real-browser verification checks the guarded backend against the isolated installed
suite and generated clips; it never uses owner media. Unit tests start no servers;
process-ownership tests may start and cancel only their own disposable children.

## Automated backend tests

Legacy source-only one-time setup (not authorized by routine task execution;
skip when the existing dependencies are present, never run in a packaged runtime):

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
- Analysis/torrent tests use fakes (`analyze_file`, `run_magnet_job_threaded`); no real
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

## End-to-end: single magnet (separately authorized private fixture only)

Historical source API examples below do not authorize server startup or a public
magnet. Substitute only the private local fixture approved for installed QA.

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

If metadata does not arrive, inspect the owned fixture server/seed logs; never
substitute a public torrent or the owner's files to make QA pass.

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

Use generated tiny synthetic clips and the approved private local torrent fixture.
Historical film/public magnets are not test fixtures. Pin expected parser facts
from generated content; metadata compatibility never establishes measured pixel
quality. Tests must not read owner media or fetch replacement model assets.

## What to check after a backend change

1. `.\.venv\Scripts\python.exe -m pytest -q` — all tests pass.
2. Hit `GET /health/` — server boots.
3. Run the module's `__main__` smoke test if you touched a parser/scorer.
4. Only in a separately authorized live QA task, run the private local fixture
   end-to-end; routine gates use fakes/synthetic inputs without a server.

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
