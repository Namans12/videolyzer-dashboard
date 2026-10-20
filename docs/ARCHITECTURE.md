# Architecture

A reference for future Claude Code sessions. Skim this before touching the
backend; it'll save you from re-deriving the data flow.

## Packaged Part 1 boundary

The primary shortcut uses private Python 3.11.9 `pythonw.exe` and stable
`setup\launch_app.pyw`. L3 validates atomic current/previous pointers and a
hash-checked `release.json` for `app/releases/<build_id>/{suite,dashboard,models,tools}`.
Final lock-keyed `runtimes/<lock_sha256>/{suite,dashboard}` are separate and immutable;
only an exactly verified legacy suite env may be reused without modification.
`app_host.py` binds one socket on 127.0.0.1:8000 or :0 and serves built dist with
relative API/SSE/output URLs. There is no runtime npm/Vite or first-run acquisition.
The payload is VideoSuite 0.6.0 / `offline_payload`, fixed RTX 3050 Ti / CUDA 12.6.

`app_preflight.py` probes explicit private runtimes, model/tool hashes, required
CUDA and cp311 libtorrent with restricted DLL search. `suite_client.py` repeats
required Full admission before its worker starts; metadata paths remain available.
All preflight child probes use `proc_guard.run_tool`, including its stopping gate
and owned cancellation/wait callbacks. `app_lifecycle.py` owns Quit admission and
draining; `app_routes.py` supplies token/Origin/JSON-guarded controls and status.
Browser closure is not Quit; reconnect uses server state. An uncooperative owned
worker keeps the instance alive with an error. Job Object assignment failure
leaves crash cleanup best effort, without adopting foreign processes.

Offline activation holds the per-user launcher lease and Calibration write lock,
verifies the staged release, switches pointers and stable launchers/tools/shortcuts,
and restores saved bytes on failed health checks. It never mutates a reused venv
or owner data. `tools/activate_app_release.py` is the installed rollback entry;
README.md documents the expected-current guard, logs and manual fallbacks. Native
installed health and owner-only real-TV tests require separate validation.

## One-paragraph mental model

A FastAPI backend wraps three offline tools (MediaInfo, ffprobe, dovi_tool) and
one online tool (libtorrent). Every user-facing request becomes a **job** stored
in the in-process job store (`jobs.py`, exposed as `main._jobs`) and runs in a
`BackgroundTasks` thread. The frontend polls `/job/{job_id}` until `status` is
`"done"`, `"error"` or `"cancelled"`, then
renders the result. There is no database — restart the server and jobs vanish.

## Module map

```
main.py             FastAPI routes, job runners (upload/path/magnet/compare), SSE streaming.
jobs.py             Job store: one lock, deep-copy reads, TERMINAL states, cancel flags, retention.
request_guard.py    ASGI middleware: Host / Origin / Sec-Fetch-Site / JSON Content-Type rules.
path_guard.py       validate_user_path(): every server-local path, allowed roots, remote drives.
suite_client.py     SuiteClient: runs `python -m videosuite <cmd> --json` as a subprocess,
                    validates the videosuite-<cmd>/1 document, kills the process tree.
suite_routes.py     /suite/version|devices|settings|score|rank|compare: bodies, guards, jobs, heavy slots.
calibration_routes.py /suite/calibration/*: strict bodies, raw output-link guards, sync calls and slotted jobs.
suite_version.py    Numeric version handshake, 300 s definitive/10 s failure cache, startup check.
magnet_suite.py     Nonfatal score_partial calls on retained probes: 8 files, 300 s stage budget.
proc_guard.py       Windows kill-on-close Job Object, assigned per real suite child.
analysis.py         analyze_file() — MediaInfo + ffprobe + dovi_tool fusion.
magnet.py           libtorrent magnet metadata + head/tail piece download, port pool.
filename_intel.py   Pure-regex parser for release names → traits dict.
verification.py     Cross-check filename traits vs stream metadata → flag list.
comparison.py       Multi-release side-by-side matrix + winner selection.
```

Layering: `main.py` adds the middlewares (CORS outermost, then `RequestGuard`, then the
body-size middleware) and calls `suite_routes.install(app)`. `jobs.py`, `path_guard.py`,
`request_guard.py` and `suite_client.py` import nothing from `main.py`; `suite_routes.py`
imports only those. The Dashboard never imports the Comparizer engine: the suite is a
subprocess (see `docs/SUITE_INTEGRATION.md`).

`src/App.tsx` owns the Analyze view and the three-tab shell. Suite and Calibration
live in `src/suite/`; each mounts on its first visit and stays mounted while hidden.

## Data flow: single magnet

```
POST /magnet/  { magnet }
    ↓                                  (background task)
_run_magnet_job
    → magnet.fetch_magnet_metadata     # libtorrent session, head/tail pieces
    → analysis.analyze_file × each video
        → mediainfo + ffprobe (parallel)
        → dovi_tool (if HEVC + DV signaled)
    → pause torrent → magnet_suite.score_partials before probe cleanup
        → each analysis.suite: ok | unavailable | error | skipped
    → comparison.enrich_release × each result
        → filename_intel.parse_release_name
        → verification.verify_release
    → store in _jobs[id]
    ↓
Client polls /job/{id} or SSE /job/{id}/events
```

## Data flow: multi-magnet compare

```
POST /compare-magnets/  { magnets: [...] }
    ↓
_run_compare_job
    ├─ ≤2 magnets: sequential, one libtorrent session at a time
    └─ 3-8 magnets: ThreadPoolExecutor(max=2), two sessions at a time
    (every session takes its own listen port from magnet.port_allocator,
     6881-6999, shared with /magnet/, and gives it back in `finally`)
    → per magnet: fetch_magnet_metadata + analyze best file
    → comparison.compare_releases([enriched, ...])
       → side-by-side matrix + winner with reasons
    → store on _jobs[id].comparison
```

## Data flow: suite jobs

```
POST /suite/score|rank|compare  { paths, ... }
    ↓ RequestGuard (JSON only) → pydantic body (extra="forbid") → get_suite_client()
    ↓                             (SuiteUnavailable → 503, no job)
    ↓ path guard on every path / reports_dir / out_dir (400/403/404)
    ↓ concurrency slot (one heavy compare/deep-DV score; two score/rank → else 409)
jobs.create("suite_score" | "suite_rank" | "suite_compare") → {job_id}
    ↓                                        (background task, slot handed to the runner)
run_score_job / run_rank_job / run_compare_job
    → SuiteClient.<cmd>(...): python -m videosuite <cmd> --list <file> --json ...
        stdout → validated JSON document, stderr → progress lines (compare) + stderr tail
    → jobs.finish(results=<document>)  or  jobs.fail(error, error_kind) + error_detail
GET /suite/devices → SuiteClient.devices(), cached 60 s, errors mapped to 503/504/502
GET /suite/version → suite_version.ensure(), cached handshake, always HTTP 200
```

The startup daemon checks the suite unless `VIDEOLYZER_SUITE_STARTUP_CHECK=0`.
The minimum is 0.3.0; capabilities gate partial scoring (`score_partial`, 0.4.0).
Magnet suite calls are sequential and can run alongside a heavy job. The torrent
stays paused during scoring; the budget starts before client/version discovery.
The bound is 300 + 105 + 5 = 410 s < 420 s, with tree killer, wait, reader joins
and pipe closes sharing the 5 s cleanup deadline. Compare uses the pair-scaled
timeout documented in `SUITE_INTEGRATION.md` unless a positive override is set.

Each real Windows suite child is assigned to the Job Object after `Popen`;
the Job Object is the primary guarantee. Shutdown also kills registered live
process trees (the fallback); `start-local.ps1` runs uvicorn with
`--timeout-graceful-shutdown 3`, so one Ctrl+C triggers that cleanup after at
most 3 s even during a running suite job. Job Object failure is logged
and leaves crash cleanup best effort. If cleanup workers cannot start, injected
killers must return promptly inline. Failed termination plus thread exhaustion
can leave a daemon reader pipe pending EOF/garbage collection.

## Job record shape

```python
# jobs.py JobStore: every read is a deep copy taken under a lock.
# TERMINAL = ("done", "error", "cancelled"); a finished job never changes again.
jobs.get(job_id) == {
    "kind":       "single" | "batch" | "path" | "magnet" | "compare"
                  | "suite_score" | "suite_rank" | "suite_compare",
    "error_kind": str | None,        # only for status "error": "timeout", "no_files",
                                     # "analysis_failed", "unavailable", "busy", "internal",
                                     # "no_analyses", or a suite_client.ERROR_KINDS value
    "error_detail": str,             # suite jobs only: last 500 chars of stderr (absent otherwise)
    "status":   "running" | "done" | "error" | "cancelled",
    "progress": "i/total",
    "current":  "currently-processing filename",
    "total":    int,
    "results":  list[VideoData],     # raw analyze_file outputs
    "enriched": list[EnrichedRelease],  # only for /magnet/
    "error":    str | None,
    "events":   [ {msg, ts}, ... ],  # SSE timeline
    # Magnet-only fields:
    "magnet_files":   list[MagnetFile],
    "magnet_torrent": {name, info_hash} | None,
    # Compare-only fields:
    "per_magnet": list[PerMagnetRecord],
    "comparison": ComparisonPayload | None,
    "winner":     {winner_index, winner_name, reasons} | None,
}
```

## Critical invariants

1. **`analyze_file()` is the single source of truth for stream metadata.**
   Anything filename-derived is a *hint*; the verification engine treats
   stream metadata as ground truth when they disagree.

2. **libtorrent's listen port must be unique per concurrent session.**
   See `magnet.py:fetch_magnet_metadata(listen_port=...)`. If you spawn
   parallel magnet jobs without distinct ports, only the first one binds
   and the rest silently fail to receive metadata. Always take the port from
   `magnet.port_allocator.acquire()` and release it in `finally`.

3. **The probe file uses `truncate()` + sparse flag.** See `magnet.py`
   near `fsutil sparse setflag`. On NTFS, `truncate()` alone allocates
   the full file size and fills the temp drive for big REMUXes.

4. **MediaInfo's exit code is unreliable on partial files.** We parse stdout
   as JSON regardless of returncode. `analysis.py:run_mediainfo` documents this.

5. **DV profile detection has three sources of truth.** In priority order:
   dovi_tool RPU scan → ffprobe `side_data_list` → MediaInfo `HDR_Format`.
   See `analysis.py:inspect_dolby_vision`.

6. **No server-local path reaches a job unvalidated.** A route that accepts a
   path calls `path_guard.validate_user_path` (via `main._validated_path` or
   `suite_routes._guarded_path`) before creating the job, and a route that
   starts work or reads state is behind `RequestGuard`. A new route that takes
   a path or starts a job needs both, plus a line in the README endpoint list
   (`tests/test_docs_contract.py` fails when a route is not mentioned there).

7. **The suite is a subprocess, never an import, never a `.bat`.** User text
   never reaches a shell: file paths travel in a `--list` file, other values
   are validated first, the process tree is killed on cancel or timeout.

## Common pitfalls

- **Don't share libtorrent sessions across threads.** Create one session per
  job; tear it down in `finally`.
- **`os.path.splitext()` on a name containing dots is sharp.** A name like
  `Movie.2024.x265.MKV.mkv` returns `(".mkv", ...)` as expected, but be
  careful with custom truncation.
- **Sparse files only work on NTFS/ReFS.** If a user puts their temp on
  FAT32/exFAT, the truncate fallback will allocate full size and fail
  with ENOSPC.
- **The frontend caches the last result set in `localStorage`** under the
  key `last_results`. Stale data after a code change → tell the user to
  clear it from devtools.

## Where to add things

| You want to…                              | Edit                          |
|-------------------------------------------|-------------------------------|
| Detect a new HDR format                   | `analysis.py:get_hdr_summary` + `filename_intel.py` HDR section |
| Add a new release-name token              | `filename_intel.py:PLATFORM_TOKENS` etc. |
| Add a fake-release check                  | New `_check_*` in `verification.py`, then list in `verify_release` |
| Change device-aware scoring               | Comparizer VideoSuite; Dashboard `BRAVIA_8_II`/`score_for_tv`/`composite_score` are frozen legacy until Plan 3 |
| Add a new comparison row                  | `comparison.py:COMPARISON_ROWS` |
| Add a new API route                       | `main.py`, follow `/magnet/` pattern; add path guard / request guard rules and the README endpoint list |
| Add a VideoSuite command or option        | `suite_client.py` (validation + document keys), `suite_routes.py` (body model, runner), `docs/SUITE_INTEGRATION.md` |
| Change an allowed Host/Origin or content type | `request_guard.py` |
| Change which paths are accepted           | `path_guard.py` (and its README section) |
| Add a UI panel                            | `src/App.tsx`, find a sibling section by greping `dashboard-section` |

## `src/suite/` frontend

The Suite tab stays mounted after its first visit; inactive panels use `hidden` so inputs and active jobs survive switching tabs. Analyze stays mounted and retains the frozen Bravia scoring. Every Suite verdict uses `VerdictBadge`, which owns the label, evidence word and visible unverified notice.

| Module | Responsibility |
| --- | --- |
| `types.ts` | Additive read shapes for suite version, devices, jobs, score/rank/compare and settings documents. |
| `config.ts` | Shared fixed loopback API base used by both App and Suite. |
| `api.ts` | Typed HTTP calls, sanitized errors, exact JSON request bodies, and job-envelope validation. |
| `useJob.ts` | Effect-owned polling session per job: single flight, abort, response-relative backoff, terminal states and honest cancel. |
| `messages.ts` | Plain status/error text, CLI progress parsing and retained-log summaries. |
| `verdict.ts` | Evidence notices, score ranges, issue order/folding, player advice and prototype-safe labels. |
| `paths.ts` | Path/folder/language checks, chooser defaults and omission of default request options. |
| `compare.ts` | Compare/rank document guards, eligibility and immutable run snapshot type. |
| `screen.ts` | Abort-safe version/devices/settings loader and all-or-nothing chooser gating. |
| `SuiteScreen.tsx` | Composition, guarded score starts and completion-ordered settings publication. |
| `SuiteStatusBanner.tsx`, `EvidenceLegend.tsx` | Suite readiness/recheck and exact evidence legend. |
| `VerdictBadge.tsx`, `BestPlayerHint.tsx`, `JobMessage.tsx` | Consistent evidence, advice and job notices; raw failure detail stays in closed details. |
| `FileChooser.tsx` | Controlled paths and device/player/filesystem/language inputs. |
| `DeviceCard.tsx`, `ScoreResults.tsx` | Full record names, per-device playback rows, issues, partial caveats and best-player evidence. |
| `ComparePanel.tsx`, `RankResult.tsx` | Guarded compare-to-rank chain, progress/cancel/retry and device-neutral/per-player rankings. |
| `SettingsPanel.tsx`, `MagnetSuiteBlock.tsx` | Read-only settings facts and runtime-narrowed saved/live magnet recommendations. |
| `suite.css` | Theme-variable styles, visible notices and complete wrapping paths. |
| `testing/fixtures.ts`, `testing/fetchMock.ts` | Cloned real documents and strictly mocked HTTP for frontend tests. |

`src/jobStatus.ts` defines the terminal job statuses shared with the legacy pollers. The new read-only `/suite/settings` route derives facts from the suite's devices document; its dedicated fill lock shares one 60-second cache fill with `/suite/devices` and never caches failures or reads the owner's settings file itself.

## `src/suite/calibration/` frontend

The third tab is **Calibration (TV)**. `src/App.tsx` provides three-way arrow/Home/End
keyboard navigation and a labelled `SuiteBoundary`. Calibration checks version first;
only an `ok` version advertising `calibration` loads settings and mounts the workspace.
An installed 0.3/0.4 suite gets the update-to-0.5.0 message here while Suite stays usable.

| Module | Responsibility |
| --- | --- |
| `CalibrationScreen.tsx` | Capability gate, settings recheck, remembered kit id, shared selection and one shared hook; supplies a newly built descriptor until its first session. |
| `KitPanel.tsx` | Source/output validation, plan estimates and frozen predictions, prepare/cancel and retained build logs. |
| `ConditionsBar.tsx` | Read-only audio/USB settings, single-line TV software (100 characters), explicit session confirmation and answer readiness. |
| `ChecklistTable.tsx` | Built-in-first player groups, frozen predictions/evidence, saved status/progress and accessible test selection. |
| `GuidedCard.tsx` | Selected test instructions/outcomes, Not sure/Skip, single-line note (500 characters), Back/Next and saved conditions. |
| `ApplyPanel.tsx` | Preview/revision, individual veto confirmations, apply/undo reasons and read-only What changed playback report through `VerdictBadge`. |
| `useCalibration.ts` | Store-backed answers, guarded mutations, stale-preview invalidation and independent plan/build/report polling through `useJob`. |
| `types.ts` | Additive calibration documents and runtime guards, with prototype-safe keyed lookups. |
| `api.ts` | Exact JSON requests through the shared API base, response guards and single-line note normalization. |
| `messages.ts` | Capability check, evidence-bearing prediction text and plain notices. |
| `calibration.css` | Theme-token layout: checklist/card columns at 900 px, one column below. |

Session and answer state lives in VideoSuite's store, not browser storage; the browser
remembers only the kit id (`calibration_kit`). Conditions changes invalidate previews;
answers retain immutable measured conditions. Build/session output paths are checked for
links/junctions before canonicalization can hide them. The write/backup/lock contract
and the eight backend routes are in `docs/SUITE_INTEGRATION.md`.
