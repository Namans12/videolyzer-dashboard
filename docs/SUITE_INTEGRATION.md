# VideoSuite integration

The Dashboard runs the installed VideoSuite (the Video Comparizer's `videosuite` package) as a
subprocess and exposes it through four `/suite/...` routes. This page is the contract: what is
run, what comes back, how failures are named. The code is `suite_client.py` (process and
document handling) and `suite_routes.py` (HTTP, guards, jobs). Examples below are taken from the
real fixtures in `tests/fixtures/suite/` (output of the CLI, tool_version `0.2.0`, run on three
generated 1-second clips; paths shortened).

## 1. Principles

- The Dashboard never imports the Comparizer engine. It starts `python -m videosuite <command> --json`.
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
| `devices` | `devices --json` | `devices` (list) | 30 s |
| `facts` (client only, no route) | `facts --list F --json [--deep-dv]` | `records` (list) | 60 s + 45 s per file (+ 900 s per file with deep DV) |
| `score` | `score --list F --json --device= --player= [--fs=] --lang= [--deep-dv]` | `records` (list) | 60 s + 45 s per file (+ 900 s per file with `deep_dv`) |
| `rank` | `rank --list F --json --device= --player= [--fs=] --lang= --reports=DIR --wq= --wp=` | `quality`, `same_picture_groups`, `not_compared`, `missing_pairs` (lists), `weights`, `devices` (objects) | as `score` |
| `compare` | `compare --list F --json --device= --player= [--fs=] --lang= --out=DIR --mode= --top=N` | `mode` (`fast`/`accurate`), `estimate` (object), `shortlist`, `dropped`, `pairs` (lists of objects), `failed` (int >= 0) | 6 h, or `VIDEOSUITE_COMPARE_TIMEOUT_S` seconds |

Every document is one JSON object `{"schema": "videosuite-<command>/1", "tool_version": "<text>", ...}`.
A document is rejected (`contract`) when stdout is empty, larger than 20 MB, not UTF-8, not valid JSON (the constants `NaN`/`Infinity` count as invalid), not an object, has another `schema` (for example `videosuite-score/9`), has no string `tool_version`, or lacks a required key of the right type. A `devices` entry also needs a string `id` and a `players` list of objects with string `id`.

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

Status codes of the three job routes: **200** `{"job_id": "<hex>"}`; **400** path guard (empty, NUL, UNC and device paths, relative paths, alternate data streams, folders where files are required, non-video files, network drives) and shell characters in a folder value; **403** a path outside `VIDEOLYZER_ALLOWED_ROOTS`; **404** a path that does not exist (for `out_dir`: whose parent does not exist); **409** `{"detail": "a comparison is already running"}` or `{"detail": "too many suite jobs are running"}`; **415** not JSON; **422** a body or value rule above; **503** the suite is not installed. The path guard messages are fixed strings: no exception text and no foreign path is ever echoed.

### `GET /suite/devices`

Returns the CLI document as it is, cached for 60 seconds (errors are never cached). Requires videosuite 0.3.0 or later (the `devices` command arrived with it). Errors: 503 (`unavailable`), 504 (`timeout`), 502 (`contract` or any other failure); the body is always a fixed `{"detail": ...}`, never stderr text. When the CLI answers with the usage exit code 2 (an installed suite older than 0.3.0 does not know `devices`) the status stays 502 and the detail is `VideoSuite does not support 'devices'; reinstall the Comparizer (videosuite 0.3.0 or later)`; the other 502 details are `VideoSuite returned an unusable response.` (`contract`) and `VideoSuite failed.`.

```json
{
 "schema": "videosuite-devices/1",
 "tool_version": "0.2.0",
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

### `POST /suite/score`

```json
{"paths": ["D:\\Media\\clip_a_crf18.mkv", "D:\\Media\\clip_b_crf34.mkv"], "device": "sony_bravia_8_ii", "lang": "eng"}
```

The finished job holds the CLI document in `results` (an object, not a list, for suite jobs). One record per file under `records`, each with `playback` keyed `<device>/<player>`:

```json
{
 "schema": "videosuite-score/1",
 "tool_version": "0.2.0",
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
 "tool_version": "0.2.0",
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
 "tool_version": "0.2.0",
 "mode": "fast",
 "estimate": {"pairs": 1, "minutes": 4},
 "shortlist": ["clip_a_crf18.mkv", "clip_b_crf34.mkv"],
 "dropped": [],
 "pairs": [{"a": "clip_a_crf18.mkv", "b": "clip_b_crf34.mkv", "report": "C:\\fixtures\\suite\\clip_a_crf18_vs_clip_b_crf34_fast_3e0d8f7455.json"}],
 "failed": 0
}
```

## 5. Job results and errors

Suite jobs (`kind` `suite_score`, `suite_rank`, `suite_compare`) answer HTTP 200 on `GET /job/{id}` whatever happened; read `status`. (The two job examples below show the key layout with invented timestamps and file names; the `results` document is the real CLI output shown in section 4.)

Done:

```json
{"kind": "suite_score", "status": "done", "progress": "1/1", "current": "", "total": 2,
 "results": {"schema": "videosuite-score/1", "tool_version": "0.2.0", "records": ["..."]},
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

One `suite_compare` at a time and at most two `suite_score`/`suite_rank` jobs at once, process-wide. The route takes the slot atomically and hands it to the runner; the runner releases it in `finally` whatever happens. A route call over the limit answers 409 and creates no job.

## 7. Testing

- `tests/test_suite_client.py` and `tests/test_suite_routes.py` use fake processes and the fixtures; no real subprocess, no taskkill.
- `tests/test_live_suite.py` (opt-in, `VIDEOLYZER_LIVE_SUITE=1`) runs the real `devices` and a real `score` on one generated clip; see `docs/TESTING.md`.
- Refreshing the fixtures: run the CLI by hand with `--json` (and `--cache-dir` pointing to a temporary folder) on generated clips and store the stdout bytes unchanged; replace the temporary path in `score.json`/`compare.json` by `C:\fixtures\suite`. `devices.json` is a plain `python -m videosuite devices --json`.
