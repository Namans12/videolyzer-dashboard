# VideoSuite integration

The Dashboard runs the installed VideoSuite (the Video Comparizer's `videosuite` package) as a
subprocess and exposes it through fourteen `/suite/...` routes. This page is the contract: what is
run, what comes back, how failures are named. The code is `suite_client.py` (process and
document handling), `suite_routes.py` (core HTTP, guards, jobs) and `calibration_routes.py`
(calibration HTTP and output-link guards). Examples below are taken from the
real fixtures in `tests/fixtures/suite/` (output of the CLI, tool_version `0.4.0`, run on three
generated 10-second clips; paths shortened). JSON examples are abbreviated key layouts;
the fixtures are authoritative for full current documents. Additive keys retain schema `/1`.

## Packaged offline contract (Part 1)

Production ships VideoSuite 0.6.0 with `offline_payload`, Python 3.11.9, the approved
RTX 3050 Ti / CUDA 12.6 profile and both pinned private package closures. Historical
fixture/minimum versions below describe backward compatibility, not the shipped
version. No later 0.7.0 unified API is implied. Stable `setup\launch_app.pyw`
validates `app/current.json`, its `release.json`, code/assets/locks/source hashes
and relative private runtime paths; it supplies explicit VIDEOSUITE_PYTHON/ROOT
and release tool directories. The legacy env.path discovery below is a source/
migration fallback, never permission to import another checkout into production.

In offline mode the client refuses pixels before creating a worker whenever any
required Full-engine check fails (model missing/corrupt, CUDA unavailable or required
tool absent). The plain unavailable message directs repair with the matching
offline installer; Quick/metadata remain available within their own requirements.
L5 also verifies required model hashes before pixel work, with no downloads or
substitute metrics. Preflight probes use the tracked/stopping-gated run_tool helper;
Stop cannot admit a later probe and running probes participate in cancellation.
Optional magnet-only OpenSSL loss keeps file checks available; a missing private
cp311 libtorrent binary blocks release. The approved wheel installs it as the
package file `Lib\site-packages\libtorrent\__init__.cp311-win_amd64.pyd`; a flat
`libtorrent.cp311-win_amd64.pyd` is the only other accepted layout
(`app_preflight.packaged_libtorrent`). System32 OpenSSL is checked,
never bundled/installed, and restricted disposable cp311 imports prove the payload.
Packaged magnet actions report exactly: "Magnet links are unavailable on this PC
(OpenSSL 1.1 files not found); file comparison still works". They provide no
online package-manager advice. Source/developer guidance remains available.
The sealed build_channel is release or qa; QA is never a distributable release.
QA has separate uninstall/shortcut identity and never runs Python's installer.
Release has no QA root dependency and verifies install-volume space directly.

`GET /app/status` exposes preflight checks, suite_handshake, active jobs and Quit
state. `POST /app/bootstrap`, `/app/quit` and `/app/open-log` use the packaged
Origin/JSON/token rules in README.md. Closing a tab leaves work running; use
Keep running, Quit when finished or Stop jobs and quit. Rollback uses installed
`tools\activate_app_release.py --expected-previous CURRENT_BUILD` after Quit,
with the current private Dashboard interpreter and verified previous release ID.
No runtime npm or online repair; owner data remains outside immutable releases.
Job Object failure leaves crash cleanup best effort. Installer/bootstrap and
backend log paths and the full build pipeline are documented in README.md.

## 1. Principles

- The Dashboard never imports the Comparizer engine. It starts `python -B -m videosuite <command> --json` with an explicit `PYTHONDONTWRITEBYTECODE=1` environment override to preserve the sealed release.
- Never a `.bat`: `cmd.exe` would re-parse the arguments (`&`, `%`, `^`, `"` in a path would reach the shell) and killing it would leave the real process running. A launcher that is a `.bat`/`.cmd`, `cmd`, `powershell`, `pwsh`, `wscript` or `cscript` (in any disguise: trailing dots or spaces, `::$DATA`) is refused.
- File paths never appear in the argument list: they travel in a UTF-8 `--list` file (one path per line) inside a private temporary folder that is removed afterwards. The remaining values are validated first; the two folder values (`reports_dir`, `out_dir`) may not contain `& ^ % ! " ' < > |` or control characters.
- The child runs in the suite root with `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `PYTHONNOUSERSITE=1`, `PYTHONSAFEPATH=1`, `PYTHONPATH=<root>`; every other inherited `PYTHON*` variable is dropped. The suite's `tools\bin` and `ffmpeg\bin` (if present) are put in front of PATH.
- On cancel, timeout or an error in a callback the whole process tree is killed (`taskkill /F /T /PID`).

## 2. Finding the suite

First match wins:

1. the constructor argument `argv_prefix` (tests);
2. the environment variables `VIDEOSUITE_PYTHON` (absolute path of a real `python.exe`) and `VIDEOSUITE_ROOT` (folder that contains the `videosuite` package), both set;
3. the `python=` line of `D:\VideoComparatorPro\setup\env.path` (root = the grandparent folder of that file; `kind=conda` with `prefix=` adds the conda folders to PATH).

Nothing found, a Python that does not exist, or a root folder that does not exist: `SuiteUnavailable`. The routes answer **503** `{"detail": "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"}` and, for the job routes, create no job. A process that cannot be started later gives `SuiteError(kind="unavailable")`: the `devices` route answers 503 as well, a job ends `error` / `unavailable`.

## 3. Commands, exit codes, error kinds

| Command | Arguments the client builds | Required document keys | Time limit |
| --- | --- | --- | --- |
| `version` | `version --json` | `capabilities` (list), `schemas` (object), `facts_version` (int) | 30 s |
| `devices` | `devices --json` | `devices` (list) | 30 s |
| `facts` (client only, no route) | `facts --list F --json [--deep-dv]` | `records` (list) | 60 s + 45 s per file (+ 900 s per file with deep DV) |
| `score` | `score --list F --json --device= --player= [--fs=] --lang= [--deep-dv]` | `records` (list) | 60 s + 45 s per file (+ 900 s per file with `deep_dv`) |
| `rank` | `rank --list F --json --device= --player= [--fs=] --lang= --reports=DIR --wq= --wp=` | `quality`, `same_picture_groups`, `not_compared`, `missing_pairs` (lists), `weights`, `devices` (objects) | as `score` |
| `compare` | `compare --list F --json --device= --player= [--fs=] --lang= --out=DIR --mode= --top=N` | `mode` (`fast`/`accurate`), `estimate` (object), `shortlist`, `dropped`, `pairs` (lists of objects), `failed` (int >= 0) | pair-scaled formula below, or exact positive `VIDEOSUITE_COMPARE_TIMEOUT_S` |

`score_partial()` is a client method, not another HTTP route or schema. It builds
`score --list F --json --real-size=N --real-name=NAME --coverage=head|head_tail`
for one file, without deep DV; timeout 105 s and a collective 5 s cleanup budget.
The client accepts an integer size from 1 to `2**50`; the CLI additionally requires
it to be at least as large as the probe on disk. The
name must be a basename, at most 255 characters, without controls or separators.
Partial source keys are `partial`, `real_name`, `probed_bytes`, `caveats`,
alongside `path`, `magnet`, `probe_coverage` (`head`/`head_tail`). The original
size is used in `container.size_bytes`.
Partial records are not cached; `--deep-dv` is refused for partial CLI inputs.

Unset/invalid compare timeout: `max(21600, pairs * per_pair_budget + 600)` s,
`n = min(len(paths), 2 * top)`, `pairs = n * (n - 1) / 2`; per-pair budget
1800 s (`fast`) or 9000 s (`accurate`). A valid positive environment override is used exactly.

Every document is one JSON object `{"schema": "videosuite-<command>/1", "tool_version": "<text>", ...}`.
A document is rejected (`contract`) when stdout is empty, larger than 20 MB, not UTF-8, not valid JSON (the constants `NaN`/`Infinity` count as invalid), not an object, has another `schema` (for example `videosuite-score/9`), has no string `tool_version`, or lacks a required key of the right type. A `devices` entry also needs a string `id` and a `players` list of objects with string `id`.

Deep validation checks consumer fields while accepting unknown additive fields:

- `version`: string capabilities, nullable string `engine_tag` when present.
- `facts`/`score`: record string `id`, object `source` with string `path`, boolean
  `source.partial` when present. `score` requires a playback object; each row has
  a known verdict (`PLAYS_WELL`, `PLAYS_DEGRADED`, `RISKY`, `WONT_PLAY`), numeric
  score, two numeric score-range values, string evidence/effective HDR, nullable
  string effective audio, issue objects with string severity/message, string hints.
  Optional `best_player` is an object whose values have string player/reason.
- `rank`: quality rows have string id/label, integer rank/games and nullable numeric
  quality score. Device blocks have ranking/will-not-play/not-compared lists and
  nullable crossing note. Ranking rows have string label, integer device rank,
  known verdict, numeric playback score, nullable numeric quality score, nullable
  string why, string reasons. Optional stale/legacy reports and head-to-head conflicts are lists.
- `devices`: string device id/label, player objects with string id/label, boolean
  default, object rule counts with string keys and integer values.
- `compare`: known mode, nonnegative integer failed count, object pairs.

Numeric and integer consumer fields reject booleans. The handshake additionally
checks `schemas` has string keys/values and nullable string version/engine tag.

The 0.4.0 additive contract includes playback `effective_hdr_class` (the class
actually displayed), `usb_filesystem`, `usb_filesystem_source`, `audio_output`,
and record `best_player` recommendations by device. `rank` keeps top-level
`quality` device-neutral; device blocks have `quality_basis`,
`format_credit_removed`, and rows with `neutral_quality_score`/
`neutral_quality_rank` beside the device-adjusted `quality_score`. Top-level
`head_to_head_conflicts` exposes a ranking reversal when a third file changes
the set mean. `stale_reports` and `legacy_reports` identify report provenance.
`settings` metadata includes path, loaded, errors and applied_count; bad settings
produce warnings and `settings.errors`, preserving CLI exit codes.

The `videosuite-version/1` document includes `tool_version: "0.4.0"`, `schemas`,
`facts_version`, `engine_tag`, `engine_files`, `rules_versions`, `python`,
`capabilities`, `settings`. Capabilities are `devices`, `version`, `device_quality`,
`best_player`, `guards`, `settings`, `score_partial`, `pair_progress`.

### Exit codes

| Exit code | Meaning | Client result |
| --- | --- | --- |
| 0 | ok | the document |
| 1 | `compare` only: some pair failed | the document is still returned (`failed > 0`); the route turns it into `failed_pairs` and keeps the results. Any other command with exit 1 is `failed` |
| 2 | bad usage or bad profile | `usage` |
| 3 | no readable file | `no_files` |
| 4 | ffprobe not found | `ffprobe_missing` |
| anything else | | `failed` |

A `compare` that exits 1 without a usable document is `failed`. Timeout is `timeout` whatever the exit code was. Output over 20 MB kills the process and is `contract`.

### Error kinds

`suite_client.ERROR_KINDS` = `usage`, `no_files`, `ffprobe_missing`, `failed_pairs`, `contract`, `timeout`, `failed`, `unavailable`. A job additionally knows `busy` (the concurrency slot was taken when the runner started; over HTTP that is a 409 instead) and `internal` (unexpected exception inside the runner; the job message is the fixed text "internal error"). The client itself never raises `failed_pairs`: the route sets it.

A failed call carries `exit_code` and `stderr_tail` (the last 200 stderr lines, at most 100 000 characters). Real example (`score` on a file that does not exist, exit code 3):

```text
kind=no_files  exit_code=3  message="videosuite score exited with code 3."
stderr_tail="not found: C:\\...\\missing.mkv\nno readable files"
```

## 4. HTTP routes

All routes sit behind the request guard (Host/Origin/Sec-Fetch-Site rules); the three POSTs must be `application/json` (415 otherwise). Bodies are pydantic models with `extra="forbid"` (an unknown field is 422). Value rules are the client's own checks, so a rule changes in one place:

| Field | Rule |
| --- | --- |
| `paths` | 1 to 50 strings of at most 4096 characters. Each is a video FILE (`.mkv .mp4 .m4v .ts .m2ts .hevc .h265`), no folder, passing the path guard. One bad path rejects the whole request. |
| `device`, `player` | `[A-Za-z0-9_.-]{1,64}`; omitted = `all` |
| `fs` | `fat32`, `exfat` or `ntfs`; omitted = not passed |
| `lang` | `^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$`; omitted = `eng` |
| `deep_dv` | `score` only: a real JSON boolean (`true`/`false`; `"true"`, `1` and `0` are 422) |
| `reports_dir` | `rank`: an existing folder, path guard, no shell metacharacters; at most 4096 characters in the body (422), at most 1000 once resolved (400) |
| `wq`, `wp` | `rank`: numbers 0 to 1000, not both 0; omitted = 0.5 |
| `out_dir` | `compare`: a folder that need not exist but whose parent must; same rules and limits as `reports_dir` |
| `mode` | `compare`: `fast` or `accurate` (required) |
| `top` | `compare`: integer 1 to 6; omitted = 4 |

Status codes of the three job routes: **200** `{"job_id": "<hex>"}`; **400** path guard (empty, NUL, UNC and device paths, relative paths, alternate data streams, folders where files are required, non-video files, network drives) and shell characters in a folder value; **403** a path outside `VIDEOLYZER_ALLOWED_ROOTS`; **404** a path that does not exist (for `out_dir`: whose parent does not exist); **409** `{"detail": "a comparison is already running"}`, `{"detail": "too many suite jobs are running"}` or, when a heavy job (a comparison or a deep Dolby Vision score) is running and another one is requested, `{"detail": "a comparison is already running; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time"}` (the first words are `a deep Dolby Vision scan is already running` when that is the running one); **415** not JSON; **422** a body or value rule above; **503** the suite is not installed. The path guard messages are fixed strings: no exception text and no foreign path is ever echoed.

### `GET /suite/version`

Always HTTP 200: `status`, `tool_version`, `minimum` (`0.3.0`), `capabilities`,
`schemas`, `engine_tag`, `message`, `checked_at`. Statuses: `ok`, `too_old`,
`unavailable`, `error`, `unchecked` (client without `version`). `?refresh=true`
forces a check. `ok`/`too_old` cache for 300 s; every failure/unchecked result
for 10 s, serialized under one lock. Startup checks run in a daemon thread unless
`VIDEOLYZER_SUITE_STARTUP_CHECK=0` (default `1`). Version comparison is numeric
and accepts development suffixes. The reinstall hint is
`Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard.`
A 0.3.0 suite without `version` is detected through `devices`, with capabilities
`["devices"]`, empty schemas and null engine tag. Its magnet files report
`The installed VideoSuite cannot score partial downloads (needs 0.4.0 or later); reinstall the Comparizer.`

### `GET /suite/devices`

Returns the CLI document as it is, cached for 60 seconds (errors are never cached).
Requires videosuite 0.3.0 or later. The handshake runs first; `too_old` returns
502 with its version/reinstall message. Otherwise errors are 503 (`unavailable`),
504 (`timeout`), 502 (`contract` or other failure), with fixed detail and no stderr.
A subsequent `devices` usage exit 2 returns the compatibility message
`VideoSuite does not support 'devices'; reinstall the Comparizer (videosuite 0.3.0 or later)`.
Other 502 details: `VideoSuite returned an unusable response.` (`contract`) or `VideoSuite failed.`.

```json
{
 "schema": "videosuite-devices/1",
 "tool_version": "0.4.0",
 "devices": [
  {
   "id": "sony_bravia_8_ii",
   "label": "Sony BRAVIA 8 II 65\" (K-65XR80M2 / K-65XR8M2), USB drive",
   "rules_version": 2,
   "verified_by_owner": false,
   "display": {"type": "QD-OLED", "width": 3840, "height": 2160, "hdr": ["hdr10", "hlg", "dolby_vision"], "peak_nits": [1400, 1800]},
   "players": [
    {"id": "builtin_usb", "label": "Sony USB media player (built-in)", "default": true,
     "rule_counts": {"official": 19, "owner": 0, "reviewed": 5, "forum": 5, "inferred": 5, "unknown": 10}},
    {"id": "kodi", "label": "Kodi on the TV (Dolby Vision compatibility mode on)", "default": false,
     "rule_counts": {"official": 0, "owner": 0, "reviewed": 1, "forum": 8, "inferred": 26, "unknown": 5}}
   ]
  }
 ]
}
```

(The real document lists three devices: `galaxy_tab_s9_ultra`, `sony_bravia_8_ii`, `vivobook_pro_16x`.)

### `GET /suite/settings`

Read-only owner settings, derived from the `devices --json` document (the Dashboard never reads the settings file itself). Same version gate, 60 s cache and error mapping as `GET /suite/devices` (503 not installed, 504 timeout, 502 too old or other failure; never stderr text). Simultaneous `/suite/devices` and `/suite/settings` requests share one CLI read.

Both routes accept the optional boolean query flag `?refresh=true`. It invalidates the shared document under the cache fill lock and immediately re-reads VideoSuite's settings/device document. Failed reads are never cached. Normal loads retain the 60 s cache; both calibration recheck buttons request a refresh.

```json
{"tool_version": "0.4.0", "supported": true, "path": "C:\\fixtures\\suite\\no_settings.json", "loaded": false,
 "errors": [], "applied_count": 0,
 "devices": {"sony_bravia_8_ii": {"label": "Sony BRAVIA 8 II 65\" (K-65XR80M2 / K-65XR8M2), USB drive",
             "usb_filesystem": null, "audio_output": null, "overrides": []}}}
```

`supported` is false when the suite's devices document has no `settings` block (a suite older than 0.4.0); `overrides` lists the applied rule overrides per player (`player`, `rule`, `source`, `reason`, `confidence`). Values of the wrong type are reported as `null`, wrong list types as empty lists, never as a 500.

### `POST /suite/score`

```json
{"paths": ["D:\\Media\\clip_a_crf18.mkv", "D:\\Media\\clip_b_crf34.mkv"], "device": "sony_bravia_8_ii", "lang": "eng"}
```

The finished job holds the CLI document in `results` (an object, not a list, for suite jobs). One record per file under `records`, each with `playback` keyed `<device>/<player>`:

```json
{
 "schema": "videosuite-score/1",
 "tool_version": "0.4.0",
 "records": [
  {
   "schema": "media-record/1",
   "facts_version": 2,
   "id": "ef7e05bf6a269dc8865169eba9e28f72b8be62a9",
   "source": {"path": "C:\\fixtures\\suite\\clip_a_crf18.mkv", "magnet": null, "probe_coverage": "full"},
   "container": {"format": "matroska", "size_bytes": 131133, "duration_s": 1.0, "overall_bitrate_kbps": 1049},
   "video": {"codec": "h264", "profile": "High", "width": 640, "height": 360, "fps": 24.0, "bit_depth": 8},
   "hdr": {"formats": [], "dolby_vision": {"status": "unknown", "profile": null}},
   "audio": [], "subtitles": [],
   "verification": {"trust_score": 100, "flags": [], "vetoes": []},
   "playback": {
    "sony_bravia_8_ii/builtin_usb": {
     "device": "sony_bravia_8_ii", "player": "builtin_usb",
     "verdict": "PLAYS_DEGRADED", "score": 58.0, "score_range": [58.0, 58.0],
     "evidence": "official", "effective_hdr": "SDR", "effective_audio": null,
     "issues": [{"code": "no_audio_tracks", "severity": "warn", "message": "the file has no audio track", "confidence": "n/a", "rule": ""}],
     "hints": []
    }
   }
  }
 ]
}
```

(Fields are shortened here; the fixture `score.json` has the whole record.)

### `POST /suite/rank`

```json
{"paths": ["D:\\Media\\clip_a_crf18.mkv", "D:\\Media\\clip_b_crf34.mkv", "D:\\Media\\clip_c_crf26.mp4"],
 "reports_dir": "D:\\Reports", "wq": 0.5, "wp": 0.5}
```

`reports_dir` holds Comparizer pair reports (`*.json`, e.g. made by `/suite/compare`); files without a report are listed under `not_compared`.

```json
{
 "schema": "videosuite-rank/1",
 "tool_version": "0.4.0",
 "quality": [
  {"id": "ef7e05bf...", "games": 1, "margin": 0.124002, "quality_score": 55.5067, "rank": 1, "label": "clip_a_crf18.mkv"},
  {"id": "95fb25df...", "games": 1, "margin": -0.124002, "quality_score": 44.4933, "rank": 2, "label": "clip_b_crf34.mkv"},
  {"id": "e5180c94...", "games": 0, "margin": 0.0, "quality_score": null, "rank": 3, "label": "clip_c_crf26.mp4"}
 ],
 "same_picture_groups": [["clip_b_crf34.mkv"], ["clip_c_crf26.mp4"], ["clip_a_crf18.mkv"]],
 "not_compared": ["clip_c_crf26.mp4"],
 "missing_pairs": [["clip_a_crf18.mkv", "clip_c_crf26.mp4"], ["clip_b_crf34.mkv", "clip_c_crf26.mp4"]],
 "weights": {"quality": 0.5, "playback": 0.5},
 "devices": {
  "sony_bravia_8_ii/builtin_usb": {
   "ranking": [{"label": "clip_a_crf18.mkv", "quality_rank": 1, "playback_score": 58.0, "verdict": "PLAYS_DEGRADED", "device_score": 56.8, "ranking_score": 56.7533, "device_rank": 1}],
   "will_not_play": [], "not_compared": [{"label": "clip_c_crf26.mp4", "quality_note": "no comparison data"}],
   "crossing": 11.0, "crossing_note": "..."
  }
 }
}
```

### `POST /suite/compare`

```json
{"paths": ["D:\\Media\\clip_a_crf18.mkv", "D:\\Media\\clip_b_crf34.mkv"], "out_dir": "D:\\Reports\\run1", "mode": "fast", "top": 4}
```

The pixel comparison runs for minutes to hours. The job's `current` shows the last stderr line of the CLI (at most 200 characters) and its `events` hold the stderr lines (at most 2000, each cut at 500 characters, then one note that further lines are not recorded). Cancel with `POST /job/{job_id}/cancel` (all three suite job kinds): the process tree is killed and the job ends `cancelled`.

```json
{
 "schema": "videosuite-compare/1",
 "tool_version": "0.4.0",
 "mode": "fast",
 "estimate": {"pairs": 1, "minutes": 4},
 "shortlist": ["clip_a_crf18.mkv", "clip_b_crf34.mkv"],
 "dropped": [],
 "pairs": [{"a": "clip_a_crf18.mkv", "b": "clip_b_crf34.mkv", "report": "C:\\fixtures\\suite\\clip_a_crf18_vs_clip_b_crf34_fast_3e0d8f7455.json"}],
 "failed": 0
}
```

### Calibration routes (VideoSuite 0.5.0)

Eight routes under `/suite/calibration/` drive the Bravia calibration kit: plan the tests, build the test files,
record what the owner saw, then preview and apply the answers as owner settings. They need VideoSuite 0.5.0
(capability `calibration`); an older suite answers 502 `suite_too_old` on the synchronous routes. The CLI documents
(`videosuite-calibrate-<plan|build|session|record|status|apply|report>/1`) are returned unchanged; the client's
commands are `calibrate_plan/build/status/session/record/apply/undo/report` (undo is `apply --undo`).
Strict bodies (`extra="forbid"`); a 422 body lists only `type`, `loc` and `msg` (no `input`, no `ctx`).

| Route | Body / query | Answer |
| --- | --- | --- |
| `POST /suite/calibration/plan` | `{"paths": [...], "players": [...], "deep_dv": false}`; `paths` 0 to 50, `players` optional (all when empty), `deep_dv` optional boolean | the plan document (200) when `deep_dv` is not true; `{"job_id"}` (kind `suite_calibration_plan`, cancellable, heavy) when it is |
| `GET /suite/calibration/status` | `?kit=<id>` optional, `[A-Za-z0-9_.-]{1,128}` | the status document |
| `POST /suite/calibration/session` | `{"out": "<kit folder>", "tv_software": "...", "audio_output": "tv_speakers\|soundbar\|avr", "usb_filesystem": "fat32\|exfat\|ntfs"}`; the last two optional (unknown) | the session document; the route resolves `<out>\kit.json` |
| `POST /suite/calibration/record` | `{"kit", "test", "player", "outcome", "note"}`; `note` optional, at most 2000 characters, one line | the record document |
| `POST /suite/calibration/build` | `{"paths": [...], "out": "<folder>", "players": [...], "tests": [...], "deep_dv": false}`; `tests` empty = every test | `{"job_id"}` (kind `suite_calibration_build`, heavy, progress lines as events) |
| `POST /suite/calibration/apply` | `{"kit", "preview": true}` or `{"kit", "preview": false, "revision": "<from the preview>", "confirm_veto": ["<change id>", ...]}` | the apply document (`revision`, `changes`, `conflicts`, `skipped`) |
| `POST /suite/calibration/undo` | `{"kit"}` | the apply document (`action: "undo"` / `"left"` rows) |
| `POST /suite/calibration/report` | `{"kit", "paths": [...]}`; `paths` 1 to 50 | `{"job_id"}` (kind `suite_calibration_report`, playback only, read-only, takes a score/rank slot) |

Rules:

- **Source paths** are video files that need NOT exist, but their folder must (a missing folder, for example an
  unplugged drive, is 404). The suite SKIPS a missing source: it contributes no record, and an excerpt test that
  needs it is reported `needs_real_file`; nothing is planned from cached records of an offline file. The path guard's raw checks (UNC and device paths, alternate data streams, reserved
  names, `VIDEOLYZER_ALLOWED_ROOTS`) and the client's checks (no control or line-separator characters, no surrounding
  white space) apply: 400 / 403 / 404 as for `score`, no job is created. `out` follows the `compare` folder policy
  (no `& ^ % ! " ' < > |`, control characters or more than 1000 characters; 400); for `build` it need not exist but its
  parent must; for `session` it must be the existing kit folder. For `build` and `session`, `out` is also refused
  (400 `out cannot be, or lie inside, a link or junction`) when it or any existing ancestor is a symlink or Windows
  junction/reparse point; this is checked on the path as sent, before canonicalization (which would hide the link
  from the suite's own output-safety check), and no job is created.
- **Sync vs job.** Plan without `deep_dv`, `status`, `session`, `record`, `apply` and `undo` answer in the request
  (the calibration sync client time limit is 60 s, exceeding the CLI's 30 s lock wait; plan uses the `score` formula: 60 s plus 45 s per file). Plan
  with `deep_dv`, `build` and `report` are jobs (`GET /job/{id}`, cancel with `POST /job/{id}/cancel`: the process tree
  is killed and the job ends `cancelled`; a job cancelled while queued never starts the CLI). The job `results` is the
  CLI document. Time limits: plan/report `60 s + 45 s per file` (+ 900 s per file for a deep plan); build
  `600 s + 120 s per test` (16 when `tests` is empty) (+ 900 s per file with `deep_dv`).
- **Concurrency.** `build` and a `deep_dv` plan take the `calibration` slot: one heavy job at a time together with
  `compare` and a deep-DV `score`, in both directions (409; texts in section 6). `report` takes a score/rank slot. The
  synchronous routes take no slot; the suite's own OS lock serialises settings and store writers (a busy lock is 409
  `calibration_busy`).
- **Settings cache.** After `undo` and after a non-preview `apply` (also when it failed or timed out, because the
  settings file may have changed) the cached `/suite/devices` + `/suite/settings` document is dropped under its fill
  lock, so the next read shows the new overrides; it never frees job slots. A preview does not touch it.
- **Errors of the synchronous routes** never carry stderr text: 503 (`{"detail": "VideoSuite is not installed ..."}`),
  504 timeout, 502 `VideoSuite returned an unusable response.` / `VideoSuite failed.` / `suite_too_old`, 400
  `{"detail": "VideoSuite refused the request.", "code": "refused"}` (for example an unknown kit, an answer for a test
  that is not in the kit, no session recorded, a veto id that is not part of the preview), and 409 with a `code` for
  the store's own refusals: `store_damaged` (the store was left unchanged), `recovery_required` (an interrupted apply
  left settings matching neither state), `calibration_busy`, `stale_revision` (preview again), `settings_changed`
  ("The settings file changed while applying. Preview and apply again."), `settings_refused`
  (`settings.json` is not a usable settings document). A 200 document with `recovery_required: true` is data: the next
  apply or undo resolves the pending transaction first.
- A job's failure is an ordinary suite job error (`error_kind`, `error_detail`, `exit_code`).

### Calibration screen and persistent writes

The third tab, **Calibration (TV)**, requires the `calibration` capability from
VideoSuite 0.5.0. An installed 0.3/0.4 suite shows the update message only in this tab;
the core Suite tab keeps working. The screen loads version first and sends no
calibration requests without that capability. Its modules live in
`src/suite/calibration/` (see `ARCHITECTURE.md`).

The flow is prepare kit → confirm TV conditions → checklist table and guided card →
preview/apply/undo → **What changed**. Predictions are frozen in the kit descriptor;
saved answers retain their measured conditions. The UI's optional note is single-line,
limited to 500 characters; TV software is single-line, limited to 100 characters.
These are frontend limits: the HTTP models allow 2000/200 characters respectively,
with the client refusing control characters and line separators. Audio output and
USB filesystem come from owner settings and are read-only in the conditions bar.

VideoSuite owns these writes; the Dashboard invokes its commands rather than editing
the files itself:

| File | Written content / safety |
| --- | --- |
| `calibration.json` | Kit descriptors, confirmed sessions, answers and embedded apply/undo transactions; damaged stores are refused. |
| `settings.json` | Scoped `rule_overrides` entries from eligible decisive answers; other settings retain their content and order semantically. Missing settings may be created; unusable settings are refused. |
| `calibration.lock` | OS-managed exclusive lock beside settings for store/settings mutations; released when the process exits. |
| `settings.json.bak` | Exact original settings bytes before replacing an existing settings file. |

The kit output holds clips and `kit.json`, published last, plus build lock/staging
files during construction. The existing folder policy rejects `&` and `%` (and the
other forbidden folder characters listed above). Build/session refuse an output
folder that is, or has an existing ancestor that is, a link, symlink or junction,
before canonicalization. VideoSuite additionally checks source/package/repository
separation and existing planned names.

Preview, status and report are read-only. Apply uses the displayed revision and
individually confirmed veto ids; stale revisions require another preview. Unknown,
unsure, skipped, informational, conflicting or differently measured answers cannot
be promoted to owner evidence. Undo removes only entries still equal to this kit's
writes. The What changed report is playback only, not a quality ranking, never
writes the record cache and does not attribute later changes to an inactive kit.

## 5. Job results and errors

Suite jobs (`kind` `suite_score`, `suite_rank`, `suite_compare`, and the calibration kinds above) answer HTTP 200 on `GET /job/{id}` whatever happened; read `status`. (The two job examples below show the key layout with invented timestamps and file names; the `results` document is the real CLI output shown in section 4.)

Done:

```json
{"kind": "suite_score", "status": "done", "progress": "1/1", "current": "", "total": 2,
 "results": {"schema": "videosuite-score/1", "tool_version": "0.4.0", "records": ["..."]},
 "error": null, "error_kind": null,
 "events": [{"msg": "Scoring 2 file(s)", "ts": 1760000000.0}, {"msg": "Done.", "ts": 1760000003.0}]}
```

Error (`results` stays the empty list; a `compare` with `failed > 0` keeps the document in `results`):

```json
{"kind": "suite_score", "status": "error", "progress": "0/1", "total": 1, "results": [],
 "error": "videosuite score exited with code 3.", "error_kind": "no_files",
 "error_detail": "not found: D:\\Media\\gone.mkv\nno readable files",
 "events": [{"msg": "Scoring 1 file(s)", "ts": 1760000000.0}, {"msg": "Error: videosuite score exited with code 3.", "ts": 1760000001.0}]}
```

- `error` is a short fixed message; `error_kind` one of the kinds in section 3; `error_detail` (additive) the last 500 characters of the CLI's stderr, present only when there was any. It can contain file paths from the CLI's own messages. HTTP error responses never carry it.
- `exit_code` (additive, an integer) is stored next to `error_detail` when it is known: the exit code of the failed call (`SuiteError.exit_code`), or 1 for `failed_pairs` (the compare document's `exit_code`). Example: `"error_kind": "no_files", "exit_code": 3`. It is absent when the CLI never ran to an exit (`unavailable`, `busy`, `internal`, `cancelled`) and on `done` jobs.
- Statuses: `running`, `done`, `error`, `cancelled` (see README, "Job states"). A job with the cancel flag set ends `cancelled` (`error` = `"cancelled"`) instead of `error`.
- `compare` with `failed > 0` (exit code 1): `status: "error"`, `error_kind: "failed_pairs"`, `error` "N comparison pair(s) failed", `exit_code` 1, `progress` `"1/1"` (the run is finished), the document kept in `results`.
- Cancel works for all three kinds: a running `suite_score`, `suite_rank` or `suite_compare` has its whole CLI process tree killed at once (the client's `score()`, `rank()` and `compare()` take a `cancel` callable that the runner points at the job's cancel flag; they return `None` when cancelled) and the job ends `cancelled` with no results. A job cancelled while it is still queued never starts the CLI.

## 6. Concurrency

One heavy job (`suite_compare` or `suite_score` with `deep_dv: true`) at a time,
and at most two score/rank jobs, process-wide. Slots are taken atomically and
released in `finally`; rejection creates no job. Capacity has precedence:
409 `too many suite jobs are running`. Two compares get 409
`a comparison is already running`. Other heavy conflicts name the active job:
`a comparison is already running; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time`
or `a deep Dolby Vision scan is already running; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time`.

The `calibration` slot (a calibration build or a deep-DV calibration plan) is heavy too and exclusive with both of
those, in both directions, and with itself: 409 `a calibration kit job is already running` for a second one; when
the other side is involved the running job is named and the text ends
`; only one heavy job (a picture comparison, a deep Dolby Vision scan or a calibration kit job) runs at a time`.
It takes no score/rank capacity, is not blocked by a full score/rank cap, and is released in `finally`; a calibration
report takes one score/rank slot and no heavy slot. Rejection creates no job.

Magnets are not heavy. `magnet_suite.py` scores at most 8 retained probes
sequentially alongside a heavy job. The torrent is paused first. Its 300 s clock
starts before client construction/version check; a last 105 s call plus one
collective 5 s cleanup deadline (tree killer, direct kill, wait, reader joins,
pipe closes) gives 300 + 105 + 5 = 410 s < the 420 s ceiling. Each analysis has
`suite.status` `ok` (record/best_player/version/partial), `unavailable` (missing
suite/capability), `error` (kind/message) or `skipped` (limit/budget/cancel);
suite failure never makes the magnet job fail.

## Process lifetime

`proc_guard.py` assigns each real Windows child immediately after `Popen` to a
kill-on-close Job Object (the primary guarantee). Fake processes are never
assigned. Backend shutdown also calls `kill_live_processes()` for registered
children (the fallback). `scripts/start-local.ps1` starts uvicorn with
`--timeout-graceful-shutdown 3`, so one Ctrl+C reaches that cleanup after at
most 3 s even while a suite job runs; without the flag uvicorn waits for running
jobs without limit and a second Ctrl+C skips the cleanup. Job Object API/nested
job failure is logged, nonfatal, and leaves only the shutdown cleanup; crash cleanup
is best effort in that case. Under thread exhaustion, injected killers must
return promptly when run inline. With failed termination and no available cleanup
thread, an active daemon reader's pipe may remain pending EOF/garbage collection.

## Unified comparison runs (Plan 4B)

The Compare screen drives backend-owned runs through `/comparison/plans`
(`RunRequest` in, `{job_id, run_id}` out; the run waits in `awaiting_confirmation`, or
`selection_required` when a magnet link has several videos), `/comparison/runs`
(confirm `{run_id, plan_revision}` exactly once; `GET` lists public snapshots),
`/comparison/runs/{run_id}` (`{run, result}`) and `/comparison/runs/{run_id}/cancel`.
The result is `videosuite-comparison-run/1`; its nested `overall`/`device` documents
are the untouched suite documents: a QuickOrder or `videosuite-quick/1` for Quick and
`videosuite-rank/1` for Full. The frontend narrows them by the result's measurement and
never treats Quick as a rank document. Suite calls receive the owner's chosen player
as `--primary-player` and the evaluation selection as `--player` (`all` with
alternatives); Overall passes neither and loads no profiles or settings.

Public snapshots never contain `reports_dir`; the frontend guards refuse it anywhere.
Nested suite documents may carry `settings.path` (the run-owned settings copy); the UI
never displays it or links to it, and reports are reached only through their opaque
`/comparison/runs/{run_id}/reports/{report_id}` URLs. Provenance `build_id` comes from
the launcher's verified installed `release.json` identity, or is null without it.
Changed settings or device rules end a run `settings_changed`: no device answer,
verified pair reports kept, and a fresh run uses the new rules. Both retains its
independent Overall answer and complete coverage when all evidence is complete,
even though the run/job is in error for the device answer. Cancellation, missing
evidence and tool/engine/facts-version changes still suppress a whole-set answer.

All `/comparison/` POSTs require JSON content type. Before re-checking edited
choices, cancel that screen's previous pending plan with the existing cancel route
and await its response; no global pending-plan cancellation affects other tabs.
`estimate.pairs` counts scheduled readable pairs; `coverage.expected_pairs` counts
all submitted pairs. See README for bounded, marked-only 30-day orphan retention.

Both unified magnet stages report per-link reasons for discovery timeout, invalid link/entry,
torrent-engine failure, insufficient temporary space, folder-access failure and busy connections.
Cancellation stops the run; missing libtorrent retains the existing unavailable handling. Each
failure logs its stage, hash, exception type and sanitized message through `video-analyzer.comparison`
(the installed backend log); full magnet/tracker URLs and credentials are redacted, including causes.
Native fatal metadata/storage alerts are no longer discarded and `add_torrent` runtime errors are
not rewritten as invalid links. The metadata deadline uses monotonic time; its 90-second budget,
session discovery settings and legacy endpoint defaults are unchanged.

The opt-in **public, trackerless loopback** proof uses real ut_metadata (no injected torrent_info)
and selected-file facts/device Quick on generated content. `upload_mode` permits that metadata
exchange with libtorrent 2.0.11 while preventing payload during planning. This does not establish
that a real public swarm has reachable seeds or prove the cause of an installed failure.

## 7. Testing

- `tests/test_suite_client.py` and `tests/test_suite_routes.py` use fake processes and the fixtures; no real subprocess, no taskkill.
- `tests/test_live_suite.py` (opt-in, `VIDEOLYZER_LIVE_SUITE=1`) runs real `devices`, `version`, `score` and `score_partial` on generated clips; see `docs/TESTING.md`.
- `tests/test_magnet_public_loopback.py` (opt-in, `PLAN4B_PUBLIC_LOOPBACK_PYTHON` names an existing binding interpreter) tests real public ut_metadata and selected-file Quick strictly on loopback; see `docs/TESTING.md`.
- Regenerate with `.\.venv\Scripts\python.exe scripts/make_suite_fixtures.py`; the script isolates settings/cache and normalizes paths in generated 0.4.0 fixtures. See `docs/TESTING.md` for configuration.
