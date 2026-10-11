# Video Metadata Analyzer Dashboard

[![Frontend](https://img.shields.io/badge/Frontend-React%2019-1f7a8c?logo=react\&logoColor=white)](https://react.dev)
[![Backend](https://img.shields.io/badge/Backend-FastAPI-0ea5a5?logo=fastapi\&logoColor=white)](https://fastapi.tiangolo.com)
[![Build](https://img.shields.io/badge/Build-Vite%20%2B%20TypeScript-ff7a00?logo=vite\&logoColor=white)](https://vitejs.dev)
[![Lint](https://img.shields.io/badge/Lint-ESLint-4b32c3?logo=eslint\&logoColor=white)](https://eslint.org)
[![License](https://img.shields.io/github/license/Namans12/videolyzer-dashboard)](LICENSE)

A full-stack video analysis app that compares local movie files using MediaInfo, ffprobe, ffmpeg, and dovi_tool, then ranks playback quality for Sony Bravia 8 Mark II USB playback.

## What This Project Does

- Analyzes one or many video files (.mkv, .mp4, .m4v, .ts, .m2ts, .hevc, .h265).
- Detects Dolby Vision profile details (including Profile 7 / 8 variants where possible).
- Computes quality, TV score, confidence score, and audio score.
- Shows rich comparison visuals: leaderboard, radar chart, score bars, and bitrate chart.
- Provides per-file USB compatibility hints for Bravia 8 Mark II.
- Supports background jobs with progress streaming (SSE) for long-running scans.
- Runs the installed VideoSuite (device playback scoring, quality ranking, pixel comparison) through the `/suite/...` endpoints (see `docs/SUITE_INTEGRATION.md`).

## Tech Stack

- Frontend: React 19 + TypeScript + Vite
- Backend: FastAPI (Python)
- Metadata/tooling: MediaInfo, ffprobe/ffmpeg, dovi_tool
- Optional: the installed VideoSuite (`python -m videosuite`), called as a subprocess, never imported

## Project Structure

```text
videolyzer-dashboard/
  src/                 # React UI
  scripts/             # Helper scripts for polling/SSE testing
  uploads/             # Temporary upload storage (auto-used by backend)
  docs/                # ARCHITECTURE.md, TESTING.md, SUITE_INTEGRATION.md
  tests/               # pytest suite (fakes only; one opt-in live test)
  main.py              # FastAPI app, upload/path/magnet routes, job runners
  analysis.py          # Core metadata extraction + scoring logic
  jobs.py              # In-memory job store (locked, deep-copy reads, cancel flags)
  request_guard.py     # Host/Origin/Fetch-Metadata/Content-Type guard (ASGI middleware)
  path_guard.py        # Validation of every server-local path
  suite_client.py      # Subprocess client for the VideoSuite CLI (validates videosuite-<cmd>/1 JSON)
  suite_routes.py      # /suite/devices, /suite/score, /suite/rank, /suite/compare
  magnet.py            # libtorrent head/tail download, torrent session settings, port pool
```

## Screenshots

### Landing / Analyze Screen
![Landing / Analyze Screen](docs/screenshots/hero.png)

### DV / HDR Ranking Page
![DV / HDR Ranking](docs/screenshots/hierarchy.png)

## Prerequisites

Install these before running:

- Node.js 20+
- Python 3.11+
- FFmpeg (must include ffmpeg + ffprobe on PATH)
- MediaInfo CLI (mediainfo on PATH)
- dovi_tool (optional but recommended for deeper DV parsing)

## Quick Start

### One-command local startup (recommended on Windows)

```bash
npm run dev:stack
```

This launches backend and frontend in two dedicated PowerShell windows.

### 1) Install frontend dependencies

```bash
npm install
```

If you see an import error for axios, install it once:

```bash
npm install axios
```

### 2) Install backend Python dependencies

Create a virtual environment once, then install the pinned requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

`uvicorn[standard]` is optional (adds faster extras such as `watchfiles` for `--reload`); plain `uvicorn` is pinned in `requirements.txt`. To run the tests install `requirements-dev.txt` instead (see `docs/TESTING.md`).

For the magnet-link feature you also need libtorrent:

```bash
python -m pip install libtorrent
# macOS may also need: brew install libtorrent-rasterbar
# Debian/Ubuntu may use: sudo apt install python3-libtorrent
```

The rest of the app boots fine without it — only the `/magnet/` endpoint will return an error explaining how to install it.

Optional (for helper scripts):

```bash
python -m pip install requests
```

### 3) Start the backend (FastAPI)

```bash
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

### 4) Start the frontend (Vite)

```bash
npm run dev
```

Open the app at the URL printed by Vite (usually http://127.0.0.1:5173 or http://localhost:5173).

If you need custom ports, run the script directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Host 127.0.0.1 -ApiPort 8000 -WebPort 5173
```

## How To Use

1. Drag and drop video files, browse for files, paste a server-local file/folder path, or paste a magnet link.
2. Toggle Fast mode if you want quicker scans (skips deep RPU scan path).
3. Click Analyze (or Analyze Magnet).
4. Review ranked results, DV profile details, confidence, and USB compatibility guidance.

### Magnet links

Paste a `magnet:?` URI into the magnet input. The backend will:

1. Fetch the torrent's metadata (file list) — no actual files downloaded yet.
2. Classify each entry: video files vs. junk (`.rar`, `.exe`, `sample`, etc.).
3. Download only slices of each video file: the first 32 MiB of every video, plus the last 64 MiB of `.mp4` and `.m2ts` files (the `moov` atom can sit at the end). Files larger than 15 GiB also get roughly one extra piece every 10 GiB so the HDR10+ scan can look inside the stream; on Windows this interior sampling happens only when the request carries `?hdr10plus=true`. Whole pieces are fetched, so the real amount is slightly larger.
4. Run the standard analysis on each partial file and mark which ones are good or bad to play.
5. Delete the partial download and tear down the torrent session.

The backend never downloads a large file in full and never seeds on purpose: the session lives only for the job and is removed afterwards, but while it runs it may upload the pieces it holds to other peers (capped at 50 KiB/s, see below). The temp directory is removed regardless of success or failure.

### Torrent session hardening

- UPnP, NAT-PMP and local service discovery are switched off; DHT stays on. Upload is capped at 50 KiB/s (`upload_rate_limit = 51200`), never lower: a ~1 B/s limit makes peers choke us (tit-for-tat) and downloads stall. Unchoke slots stay at the libtorrent default.
- Every torrent session gets its own listen port from one process-wide pool (`magnet.PortAllocator`, ports 6881-6999). `/magnet/` and every slot of `/compare-magnets/` take a port and always give it back, so concurrent jobs never share a port. If the pool is exhausted the job ends `error` with `error_kind: "busy"`.
- MP4/M2TS files no larger than 96 MiB (head 32 MiB + tail 64 MiB) are treated as fully downloaded instead of being reported as "tail missing".
- `files[]` entries now carry `size_gib` (size in GiB, 1024^3). `size_gb` is a DEPRECATED alias with the same GiB number (not decimal GB); the UI still reads it until Plan 3.
- Testing note: libtorrent is not installed on the development machine, so these changes are tested with fakes only (pure helpers `session_settings`, `PortAllocator`, `tail_is_present`, `file_record`, plus a fake `libtorrent` module). The owner must run ONE real magnet after this change (single `/magnet/`, ideally followed by a 3-magnet `/compare-magnets/`) to confirm metadata and pieces still arrive. If magnets stall, remove `upload_rate_limit` from `magnet.session_settings` first (it is the only new behavioural key).
- Live check (needs libtorrent; run it in the Python that has it): every key of `session_settings` must be a real libtorrent setting, otherwise `lt.session(...)` raises.

  ```python
  import libtorrent as lt
  from magnet import session_settings
  assert set(session_settings(6881)) <= set(lt.default_settings())
  ```

## API Endpoints

Every route passes the request guard; every route that takes a server-local path also passes the path guard (see Security model). Routes exposed by the backend:

- GET /health/ - service health + disk usage
- POST /analysis/ - analyze one multipart file (returns `{job_id, total}`)
- POST /analyze-multiple/ - submit multi-file batch job (returns `{job_id, total}`)
- GET /analyze-path/ - analyze a server-local file or folder as a background job (returns `{job_id}`)
- GET /job/{job_id} - poll job status/results (404 `{"detail": "Job not found."}` for an unknown id)
- GET /job/{job_id}/events - SSE stream of job events; ends with `data: __done__` when the job is `done`, `error` or `cancelled`. (There is no `/progress/{job_id}` route; older notes mention it, it never existed in this backend.)
- GET /scan-folder/ - synchronous folder scan (404 when the folder has no video file that passes the path guard)
- POST /magnet/ - submit a magnet link (JSON `{"magnet": "magnet:?..."}`); fetches metadata + head/tail slices, classifies each file, and runs ffprobe on the partial data (cleans up afterward). Optional query flags `fast` and `hdr10plus`.
- POST /compare-magnets/ - compare 1 to 8 magnets (JSON `{"magnets": ["magnet:?...", ...]}`, query `fast` (default true) and `hdr10plus`); returns `{job_id, total}`. The finished job carries `per_magnet`, `comparison`, `winner`, `mode` (`movie` or `series`), `episode_comparison` and `dropped` (magnets that produced no analysable file). Up to 2 magnets run one after the other; 3 to 8 run with at most 2 parallel sessions.
- POST /magnet/{job_id}/cancel - cancel an in-flight magnet job
- POST /compare-magnets/{job_id}/cancel - cancel an in-flight magnet comparison
- POST /job/{job_id}/cancel - cancel any job (see Job states below)
- GET /suite/devices - the VideoSuite device catalogue (cached 60 s); needs videosuite 0.3.0 or later
- POST /suite/score - device playback scoring of 1 to 50 video files (job)
- POST /suite/rank - quality + playback ranking of 1 to 50 video files using an existing folder of Comparizer reports (job)
- POST /suite/compare - pixel comparison of 1 to 50 video files into an output folder (job, one at a time)

The four `/suite/...` routes are described in `docs/SUITE_INTEGRATION.md` (request bodies, response documents, error kinds, exit codes). A `/suite` job POST answers 503 `{"detail": "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"}` and creates no job when the suite is not installed; a second `/suite/compare` (or a third `/suite/score` or `/suite/rank`) while others run answers 409.

## Job states

`GET /job/{job_id}` returns `status` `running`, `done`, `error` or `cancelled` (the last three are terminal), plus the extra keys `kind` and `error_kind`. Suite jobs add `error_detail` when the CLI wrote to stderr and `exit_code` when the CLI got as far as exiting.

- `kind`: `single`, `batch`, `path`, `magnet`, `compare`, `suite_score`, `suite_rank`, `suite_compare`.
- `error_kind` (set only while `status` is `error`, otherwise `null`):
  - Upload, path and magnet jobs: `timeout` (analysis timed out or returned nothing, or the magnet job timed out), `analysis_failed` (every file failed or the analyser raised), `no_files` (no video file in the folder), `unavailable` (libtorrent is not installed), `busy` (all 119 torrent ports, 6881-6999, are in use), `no_analyses` (a magnet comparison produced nothing comparable), `internal` (anything unexpected; the text in `error` is the exception message for magnet, magnet-comparison and folder jobs).
  - Suite jobs: the CLI client kinds `usage` (exit 2 or a rejected value), `no_files` (exit 3), `ffprobe_missing` (exit 4), `failed_pairs` (compare exit 1, results kept), `contract` (malformed, unversioned or oversized CLI JSON), `timeout`, `failed` (any other exit code), `unavailable` (process could not be started), plus `busy` (the concurrency slot was taken when the runner started) and `internal` (unexpected exception; the message is a fixed string, no exception text).
- `error_detail` (suite jobs only, additive): the last 500 characters of the CLI's stderr. It is only present when there was stderr text; HTTP error responses of the `/suite` routes never carry it.
- `exit_code` (suite jobs only, additive): the CLI's exit code when it is known: that of the failed call (`usage` 2, `no_files` 3, `ffprobe_missing` 4, `failed`, `timeout` and `contract` when the process had exited), or 1 for `failed_pairs`. It is absent when the CLI never ran to an exit (`unavailable`, `busy`, `internal`, `cancelled`).

- A job whose analysis timed out or returned nothing (`analysis timed out or failed`), whose folder holds no video files (`no video files found`) or in which every file failed ends `error` with the reason in `error`. These used to end `done` with empty results.
- The cancel routes return `{"ok": true}` for any existing job (even a finished one, which stays as it is) and 404 `{"detail": "Job not found."}` otherwise. A cancelled job stops queuing new files, finishes the analyses already running, and ends `cancelled` with `error: "cancelled"` and its partial `results`/`per_magnet`. Suite jobs (`suite_score`, `suite_rank`, `suite_compare`): cancelling a running job kills the whole CLI process tree at once and the job ends `cancelled` (no results); a suite job cancelled while it is still queued never starts the CLI and ends `cancelled` as well.
- `/job/{job_id}/events` (SSE) ends with `__done__` on `done`, `error` or `cancelled`.
- Finished jobs are kept in memory (the newest 20, never purged within 30 s of finishing); the purge happens when a new job is created.

## Security model

The backend is a local tool: it binds to loopback (`--host 127.0.0.1`), has no login, and can read any video file the user's account can read. Three layers keep a web page or a stray path from abusing that:

1. **Request guard** (`request_guard.py`, below): only requests that look like they come from the local UI or a local script get through; a web page on another site cannot start a job, read a path or start a torrent, and DNS-rebinding hosts are refused.
2. **Path guard** (`path_guard.py`, below): every server-local path (the `path` of `GET /analyze-path/` and `GET /scan-folder/`, and every `paths`, `reports_dir` and `out_dir` of the `/suite` routes) is checked before any job exists. UNC and device paths, relative paths, alternate data streams and non-video files are refused.
3. **Allowed roots** (`VIDEOLYZER_ALLOWED_ROOTS`): an optional list of folders. When set, a path must resolve (junctions and symlinks followed) to somewhere inside one of them, otherwise 403. Default: empty, which allows any local drive.

The **remote-drive rule**: a path typed with a mapped network drive letter (`Z:\videos\a.mkv`, drive type "remote") is refused with 400 `network drives are not allowed` unless `VIDEOLYZER_ALLOW_REMOTE_DRIVES` is exactly `1`. Windows resolves such a path to a UNC path (`\\server\share\...`), so the guard looks at the drive letter you TYPED: a resolved UNC path is accepted only when that letter is a network drive, the variable is `1` and the result is not a device path. A path you type as UNC (`\\host\share`, `//host/share`, `\\?\UNC\...`) and a `\\.\` device path are refused always, whatever the variable says, and so is a junction or symlink on a local drive that points at a share. This rule has been tested with a faked drive type and a faked resolver only, not against a real mapped drive: if a real mapped drive is still refused with the variable set, keep the variable unset and use a local copy.

Subprocesses never see a shell: the suite client starts `python -m videosuite` with an argument list (never a `.bat`), passes file paths in a UTF-8 list file, and rejects shell metacharacters in the two folder values.

Residual risk (stated, not hidden):

- Browsers older than Fetch Metadata (Safari < 16.4) send neither `Origin` nor `Sec-Fetch-Site` on a no-cors GET, so the job-starting GET routes (`/analyze-path/`, `/scan-folder/`) are not protected against a hostile page in such a browser. Plan 3 moves those job-starting GETs to POST.
- The guard is not authentication: any program running on the same machine (no `Origin`, no `Sec-Fetch-Site`) is treated like curl and passes.
- With no allowed roots configured, any local drive is readable, and a junction or symlink inside an allowed local folder may lead to another local folder (network shares stay refused, mapped drives only with `VIDEOLYZER_ALLOW_REMOTE_DRIVES=1`, and then a mapped drive may resolve to any network host). Set `VIDEOLYZER_ALLOWED_ROOTS` to close that, and always set it when remote drives are enabled.
- A path is validated when the request arrives and used when the job runs. A single file whose junction or symlink is swapped in between is not re-checked (folder scans do re-check every file they walk).

## Request guard (security)

The backend listens on loopback but starts jobs, reads local paths and starts torrents on plain requests, so every request passes `request_guard.py` first:

- `Host` must be `localhost`, `127.0.0.1` or `[::1]` (optional port), otherwise 400 (blocks DNS rebinding).
- `Origin`, when sent, must be an http(s) loopback origin, otherwise 403 (includes `Origin: null`). An allowed Origin is trusted even when the browser labels the request `cross-site` (the UI on `localhost:5173` calling `127.0.0.1:8000`).
- With no `Origin`, a request carrying `Sec-Fetch-Site: cross-site` or `same-site` is refused with 403 (blocks `<img>`/link/no-cors GET and cross-site navigations). `same-origin`, `none` and requests with neither header (curl, scripts, tests) pass.
- `POST /magnet/`, `POST /compare-magnets/` and every `POST /suite/...` must be `application/json` (415 otherwise). The cancel routes and the multipart upload routes are not affected.

Browsers answer CORS preflights first; the guard's refusals for an allowed origin still carry CORS headers.

Residual risk: browsers older than Fetch Metadata (Safari < 16.4) send neither `Origin` nor `Sec-Fetch-Site` on a no-cors GET, so the job-starting GET routes (`/analyze-path/`, `/scan-folder/`) are not protected against a hostile page in such a browser. Plan 3 moves those job-starting GETs to POST.

## Path guard (security)

`GET /analyze-path/` and `GET /scan-folder/` read server-local paths, so every path passes `path_guard.py` first; so do the `paths` (video FILES only, no folders), `reports_dir` (an existing folder) and `out_dir` (a folder that need not exist but whose parent must) of the `/suite` POST routes. Rejections never echo exception text or any path other than the one you sent.

- 400: empty path, NUL character, UNC (`\\host\share`, `//host/share`), `\\?\` and `\\.\` device paths, relative or drive-relative paths (`C:foo`, `\Windows`), device names (`NUL`, `CON`), alternate data streams (`file::$DATA`), a path that cannot be resolved or accessed, a file where a folder is required (and the reverse), paths on a network drive (see the remote-drive rule above), and files that are not video files. Supported extensions: `.mkv .mp4 .m4v .ts .m2ts .hevc .h265` (so `win.ini` is refused rather than handed to mediainfo). A plain long-path prefix `\\?\C:\...` is stripped and the path is then checked as `C:\...`; `\\?\UNC\...` stays refused.
- Allowed roots are resolved the same way as the path (a root given as a mapped drive letter becomes its UNC target) and a drive-only root such as `D:` is read as `D:\` (the whole drive), never as "the current folder of D:".
- 403: the path (after resolving `..` and junctions/symlinks) is outside the allowed roots.
- 404: the path does not exist. This changed from 400 (`File or folder does not exist.` / `Folder does not exist.`) to 404 `path not found` on both routes.
- Folder scans re-check every file they find, so a junction or symlink inside a folder cannot lead outside the allowed roots or to a network share; such files are skipped. A folder whose real location was already visited (a junction loop pointing back at a parent, or a second link to the same folder) is not entered again.
- Upload file names (`POST /analysis/`, `POST /analyze-multiple/`): characters that are invalid in Windows file names (`: < > " / \ | ? *` and control characters) and trailing dots and spaces are removed from the name used on disk (a `:` would otherwise create an alternate data stream); the name shown in results is unchanged.

The two path-guard environment variables are listed under Environment variables below.

## Environment variables

The backend reads these variables (all optional). The two `VIDEOLYZER_` variables are read on every request. `VIDEOSUITE_PYTHON` and `VIDEOSUITE_ROOT` are resolved once, when the first `/suite` request creates the suite client (a failed lookup is retried on the next request; after a success restart the backend to change them). `VIDEOSUITE_COMPARE_TIMEOUT_S` is read at every compare call.

| Variable | Read by | Meaning |
| --- | --- | --- |
| `VIDEOLYZER_ALLOWED_ROOTS` | `path_guard.py` | `;`-separated folders (for example `D:\Media;E:\Movies`). When set, every path must resolve inside one of them (else 403). Default empty: any local drive is allowed. |
| `VIDEOLYZER_ALLOW_REMOTE_DRIVES` | `path_guard.py` | `1` allows paths typed with a mapped network drive letter (they resolve to UNC; see the remote-drive rule under Security model). Anything else (default): refused with 400 `network drives are not allowed`. Paths typed as UNC and device paths stay refused. |
| `VIDEOSUITE_PYTHON` | `suite_client.py` | Absolute path of the suite's `python.exe` (never a `.bat`, `.cmd` or a shell). Used together with `VIDEOSUITE_ROOT`. |
| `VIDEOSUITE_ROOT` | `suite_client.py` | The suite folder that contains the `videosuite` package. It becomes the child's working folder and `PYTHONPATH`; its `tools\bin` and `ffmpeg\bin` folders are put on the child's PATH. |
| `VIDEOSUITE_COMPARE_TIMEOUT_S` | `suite_client.py` | Time limit of one `compare` call in seconds (default 21600 = 6 h). An unparsable, zero or negative value falls back to the default. |

When `VIDEOSUITE_PYTHON` and `VIDEOSUITE_ROOT` are not both set, the suite is looked up in `D:\VideoComparatorPro\setup\env.path` (its `python=` line; the root is the grandparent folder of that file). If neither finds a suite, the `/suite` routes answer 503.

The opt-in live test (`tests/test_live_suite.py`) uses `VIDEOLYZER_LIVE_SUITE=1` (read by the test, not by the app) and redirects the suite's record cache into a temporary folder with `VC_CACHE_DIR`; see `docs/TESTING.md`.

## Helper Scripts

The scripts folder includes quick diagnostics:

- scripts/run_and_poll.py: upload a file via curl and poll /job/{job_id}
- scripts/test_sse.py: trigger analyze-path and print the SSE events of `/job/{job_id}/events`

## Notes On Accuracy

- Scores are heuristic, not absolute truth.
- dovi_tool improves confidence when available.
- Fast mode is faster but may reduce certainty for edge cases.
- USB compatibility checks target Sony Bravia 8 Mark II behavior and practical constraints.

### Change note: TV scoring and playability (RB-1, RB-2, RB-6)

- **Non-DV base scores (RB-1).** A file without Dolby Vision used to get a flat base of 10 in `tv_score` (the 0 stored for `None` was dropped). `score_for_tv` now classifies it from the HDR summary and uses provisional, owner-tunable base scores in `BRAVIA_8_II["dv_tv_score"]`: **HDR10 28, HLG 24, SDR 12** (HLG = `hlg` / `arib std-b67`; HDR10 = `hdr10`, SMPTE ST 2084/2086, `pq`; anything else = SDR). Unknown DV profiles (9, 10, 20, ...) keep the flat 10. Example: MP4, 20.5 Mbps, 10-bit, DD+, WEB-DL, HDR10 scores 63 (45 before). **Owner-visible:** `tv_score` of non-DV files rises by 2 to 18 points and rankings change: with all other attributes equal, plain HDR10 (28) now outranks Dolby Vision 8.x (27), Profile 7 without a known layer (26), 8.2 (25), Profile 7 FEL (22) and Profile 4 (15), while 8.1 (35), 8.4 (32), Profile 5 (30) and Profile 7 MEL (30) still rank above it. An HDR10 REMUX with a high bitrate can now reach about 76 to 82 points, i.e. the "Very Good" and "Excellent" labels. These three numbers are provisional constants for you to tune.
- **`score` and `confidence_score` depend on `tv_support["profile_supported"]` (RB-2).** `score_video` adds +5 for "Yes" / +2 for "Partial" and `score_confidence` adds +4 / +2. Because non-DV HEVC files keep `profile_supported` = "No" (only `playable` says they play), their `score` and `confidence_score` are unchanged by this task; if the owner later makes `profile_supported` "Yes" or "Partial" for HDR10/HLG/SDR, those bonuses apply and `score`/`confidence_score` rise accordingly. `tv_score` is the value that changes now.
- **`playable` key (RB-2).** `tv_compatibility_heuristic` returns an extra `playable` ("Yes" / "No"): "No" only when the video codec is not one the TV's USB player supports (for example MPEG-2, VC-1) or the container is a known unsupported one (for example raw HEVC). `profile_supported` keeps its values (Yes, Partial, Limited, No, Unknown) and is only about Dolby Vision; for HEVC without Dolby Vision it is "No" with the note "No Dolby Vision; plays as HDR10/HLG/SDR". `build_recommendation` now tests `playable`, so HDR10/HLG/SDR HEVC and H.264 files are no longer reported as "Not a suitable playback target for this TV".
- **E-AC-3 audio token (RB-6).** `E-AC-3` / `E-AC3` now scores like Dolby Digital Plus (10) instead of falling through to AC-3 (5). DTS:X stays at the plain DTS value; the audio model itself is unchanged. **Owner-visible:** `score` and `tv_score` rise for E-AC-3 tracks that have no commercial name (such as "Dolby Digital Plus") in their metadata; `confidence_score` does not change.

### Change note: verification and record keys (RB-7, RB-8, RB-12, RB-13)

- **Additive record keys.** Analysis records (and the magnet records built from them) gain `audio_tracks` (one entry per audio track: `codec`, `channels`, `atmos`, `dtsx`, `lossless`, `language`, `default`), `has_atmos` and `has_dtsx`. Nothing was removed or renamed.
- **Fewer false verification flags.** A correct file is no longer flagged for: an MP4 whose container is reported as `MPEG-4` / `QuickTime / MOV` (false `container_mismatch`); a 2.40:1 scope picture such as 3840x1600 or 3600x1500 claimed as 2160p (false `resolution_mismatch`: the width rules are now 2160p = width >= 3600 or height >= 1960, 1080p = width >= 1800 or height >= 900, 720p = width >= 1200 or height >= 600); a release that claims Atmos or DTS:X when ANY audio track has it (for example DD+ 5.1 plus a TrueHD Atmos track). A real mismatch (an MKV that is really an MP4, 1920x1080 claimed as 2160p, Atmos claimed with no Atmos track) is still flagged. Records without `audio_tracks` (older ones) fall back to the old summary-text check.

### Change note: Dolby Vision parsing (RB-3, RB-4, RB-5, RB-15, RB-16, RB-19, RB-28)

- **DV-in-MKV warning on every Dolby Vision MKV.** The "DV in MKV can lose DV box atoms during playback" warning now fires for every MKV with a Dolby Vision profile; it used to be unreachable. Expect it on files that carried no warning before.
- **Filename FEL/MEL is a hint, not a fact.** `FEL` and `MEL` count only as whole words in the file name (so "Fellowship", "Camelot" and "Melancholia" no longer match) and the layer then carries `layer_reason` "filename hint (unverified)": it earns no +6 in `confidence_score` and counts like an unknown layer. A layer reported by dovi_tool wins over the filename. The insight text now names the source of a FEL/MEL verdict: "via filename hint" only for a filename hint, "confirmed by dovi_tool" or "suggested by the bitrate-ratio heuristic" otherwise. The recommendation "Remux to MP4 with mp4muxer for safer DV playback" is given only to files that have a Dolby Vision profile, no longer to every MKV.
- **AVC Dolby Vision.** Codec strings `dvav.` and `dva1.` are parsed like `dvhe.` / `dvh1.`, so AVC DV files get their profile.
- Smaller fixes: dovi_tool's `Profile:` line is parsed into `profile_number` and `layer`; a bare profile "8" maps to the 8.x entry; "unknown layer" no longer produces the "Consider converting to P8.1 MP4" advice by a substring accident.

### Change note: release-name parsing (RB-17, RB-18, RB-26)

- **WEBRip scores 1 lower than the old WEB-DL guess.** A name with `WEBRip` is now guessed as source "WEBRip" (source bonus 1) instead of "WEB-DL" (2), so its `tv_score` and `score` are 1 lower; `WEB` next to a platform or on its own still means WEB-DL.
- **Streaming platform no longer comes from `MA`, `Max` or `It` alone.** A platform tag counts when it is unambiguous (for example `AMZN`, `DSNP`, `ATVP`) or sits next to a web tag (`WEB`, `WEB-DL`, `WEBRip`); the DTS-HD.MA audio token, "Mad.Max" and the film "It" no longer produce a platform, and `Infinity.War` / `Inferno` are no longer guessed as WEB-DL through an "nf" substring.
- **No source tag and no web-adjacent platform: bitrate heuristics.** Such a name (for example `Movie.2160p.NF.HDR.mkv`) falls back to the bitrate-based source guess (WEB/Encode, Likely REMUX, ...) instead of WEB-DL.
- A missing `bitrate_mbps` no longer crashes the composite score or the release recommendation.

### Change note: guards, jobs and magnets for API clients

- Unsafe requests are rejected (Request guard, Path guard): a cross-site page, a foreign Host, a UNC path or a non-video path now gets 4xx instead of being served. `GET /analyze-path/` and `GET /scan-folder/` answer 404 `path not found` where they used to answer 400.
- **Accepted file extensions changed.** The old `GET /analyze-path/` accepted ANY existing single file whatever its extension (a `.mov`, `.avi`, `.webm` or even `.txt` was handed to the analyser) but walked folders for only `.mkv .mp4 .ts .m2ts .hevc .h265`; the old `/scan-folder/` used that same list. Now a single file, a file inside a folder and every `/suite` path must have one of `.mkv .mp4 .m4v .ts .m2ts .hevc .h265` (`.m4v` is new: it is an MP4-family container and the analyser reads the container with MediaInfo/ffprobe, not from the extension). A single file with any other extension, which used to be analysed, is now refused with 400 `not a supported video file`. `.mov .avi .wmv .webm .flv .mpg .mpeg` were NOT added: the analyser itself is extension-agnostic, but the Dashboard's container and compatibility rules are written for MKV, MP4, TS and M2TS, so such files would be scored with an unknown container (see Known limitations); say so if you want them accepted.
- Jobs whose analysis fails, times out or finds no files end `status: "error"` (they used to end `done` with empty results); `kind`, `error_kind` and `cancelled` are new, additive values.
- `/magnet/` and `/compare-magnets/` jobs take a listen port from one shared pool, UPnP/NAT-PMP/LSD are off, `files[]` gain `size_gib` (`size_gb` stays as a deprecated alias with the same GiB number), and MP4/M2TS files up to 96 MiB count as fully downloaded.
- New: the `/suite/...` routes and the `error_detail` job key (see `docs/SUITE_INTEGRATION.md`).

## Known limitations

- Another browser tab that is polling a cancelled magnet or comparison job keeps polling until the job is purged: the React UI treats only `done` and `error` as terminal and does not know `cancelled` yet (UI deferral to Plan 3).
- There is no cap on concurrent path and magnet jobs (`GET /analyze-path/`, `POST /magnet/`, `POST /compare-magnets/`, uploads): each running job holds one of the 40 threads of the framework's thread pool for its whole run, so many parallel jobs can use them all and make other requests wait. This predates the Plan 2 work and is deferred to Plan 3 (magnet jobs are limited by the 119 listen ports, the other kinds are not limited at all). Only the `/suite` jobs are capped (one compare, two score/rank).
- The job-starting GET routes `/analyze-path/` and `/scan-folder/` stay GET for UI compatibility; see Security model for the residual risk in old browsers. Plan 3 moves them to POST.
- The Dashboard's own TV scoring is Bravia-only and stays for magnet results until Plan 3; magnet-sourced analyses are not scored by the VideoSuite (the CLI takes local paths, not records).
- Jobs live in memory only: a restart loses them, and only the newest 20 finished jobs are kept.
- The magnet and torrent code is tested with fakes only (libtorrent is not installed on the development machine). Run one real magnet and the live check above after changing it.
- The suite's `/suite` paths accept video files only, not folders (Plan 3 may revisit).
- `playable` and `usb_compatible` can disagree for containers the Dashboard does not know (AVI, WebM, WMV), for 12-bit video and for pictures above 4K.
- Deferred by plan: 4096x2160 is still reported as "exceeds 4K"; feature bonuses are still taken from the file name; the multipart advice limit (10 GB per file) and the request-body limit (120 GB) are two different numbers (see Troubleshooting; use the path input for big files).

## Troubleshooting

- 507 disk full: clean the uploads folder and free local disk space.
- Upload too large: a multipart file that reports more than 10 GB is refused with 400 and the advice to use the path input instead; a request body above 120 GB is refused with 413. Use `GET /analyze-path/` (the path box in the UI) for big local files.
- 409 on `/suite/...`: another comparison is running (one `/suite/compare` at a time) or two `/suite/score` / `/suite/rank` jobs are running; retry when one has finished.
- 502 `VideoSuite does not support 'devices'; reinstall the Comparizer (videosuite 0.3.0 or later)` on `GET /suite/devices`: the installed suite is older than 0.3.0 and has no `devices` command; reinstall the Comparizer from a version that has it.
- 503 on `/suite/...`: the VideoSuite was not found. Set `VIDEOSUITE_PYTHON` and `VIDEOSUITE_ROOT` (see Environment variables) and restart the backend.
- No Dolby Vision details: verify ffprobe/mediainfo availability and file DV signaling.
- CORS/UI issues: use localhost/127.0.0.1 for both frontend and backend.

## Development Commands

```bash
npm run dev      # frontend dev server
npm run dev:stack # launch backend + frontend together (Windows)
npm run build    # frontend production build
npm run preview  # preview production build
npm run lint     # lint frontend code
```

## License

This project is licensed under the [MIT License](LICENSE)
