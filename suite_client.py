"""Client for the installed VideoSuite CLI (``python -B -m videosuite``).

The Dashboard never imports the Comparizer engine and never calls a ``.bat``: cmd.exe
re-parses the arguments of a batch file, so ``&``, ``%``, ``^`` and ``"`` in a path would
reach the shell, and terminating a batch file kills only cmd.exe. Instead the client
starts the venv's ``python.exe -B -m videosuite ...`` with an argument LIST (never a shell
string) and the same environment ``setup\\_env.bat`` + ``run_suite.bat`` build.

* Resolution (first match wins): the constructor's ``argv_prefix``; the environment
  variables ``VIDEOSUITE_PYTHON`` + ``VIDEOSUITE_ROOT``; the ``python=`` line of
  ``D:\\VideoComparatorPro\\setup\\env.path`` (root = the grandparent of that file).
  Nothing found -> ``SuiteUnavailable``. A launcher that is a ``.bat``/``.cmd`` is refused.
* Every file path travels in a UTF-8 ``--list`` file (one path per line) in a private
  temp folder that is removed in ``finally``; no user path is ever an argv item. The
  other values are validated first (``SuiteArgumentError``), and the two folders
  (``reports_dir``/``out_dir``) additionally may not contain shell metacharacters.
* Output: every command runs with ``--json``. Both pipes are drained by reader threads
  (no deadlock). ``compare`` forwards stderr (where progress lives with ``--json``)
  line by line to ``on_line``; the last 200 stderr lines are kept as ``stderr_tail``.
* Exit codes: 0 ok; ``compare`` 1 = some pair failed (the document is still returned,
  ``failed > 0``); 2 ``usage``; 3 ``no_files``; 4 ``ffprobe_missing``; else ``failed``.
* The JSON is validated (UTF-8, parses, <= 20 MB, ``schema == videosuite-<cmd>/1``,
  ``tool_version``, the required keys of the command): any violation is
  ``SuiteError(kind="contract")``.
* On cancel, timeout or an exception in a callback the WHOLE process tree is killed with
  ``taskkill /F /T /PID`` (the injectable ``killer``) and the call returns/raises.
* The child starts in the suite root (``cwd=root``) with ``PYTHONSAFEPATH=1`` and ``PYTHONPATH=<root>`` only: every
  other inherited ``PYTHON*`` variable is dropped, and the Dashboard's own modules cannot shadow the suite's imports.

Caller contract (D11 and anything else that uses the client):

* ``compare()``, ``score()`` and ``rank()`` return ``None`` when they were cancelled via ``cancel``: test ``result is None``, never truthiness
  (a returned document is a dict, an empty-looking one is still not a cancellation).
* The client NEVER raises ``SuiteError(kind="failed_pairs")``. ``compare`` exit code 1 returns the document
  normally (``doc["failed"] > 0``, ``doc.exit_code == 1``); the caller maps ``failed > 0`` to ``failed_pairs``
  and keeps the results.
* "Unavailable" has two forms the caller must both handle: ``SuiteUnavailable`` (from ``get_suite_client()`` or the
  constructor: the suite is not installed or misconfigured) and ``SuiteError(kind="unavailable")`` (from a call:
  the process could not be started).
* ``SuiteArgumentError`` (a ``ValueError``) is raised before any process starts; its message never contains the
  offending value.
"""
from __future__ import annotations

import collections
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import proc_guard

_LIVE: set[Any] = set()
_LIVE_LOCK = threading.Lock()


def live_processes() -> List[Any]:
    with _LIVE_LOCK:
        return list(_LIVE)


def kill_live_processes(killer: Optional[Callable[[int], None]] = None) -> int:
    killer = killer or taskkill_tree
    processes = live_processes()
    for proc in processes:
        try:
            killer(proc.pid)
        except Exception:
            pass
        try:
            proc.wait(5)
        except Exception:
            pass
    return len(processes)

DEFAULT_ENV_PATH = Path(r"D:\VideoComparatorPro\setup\env.path")
ENV_PYTHON = "VIDEOSUITE_PYTHON"
ENV_ROOT = "VIDEOSUITE_ROOT"
ENV_COMPARE_TIMEOUT = "VIDEOSUITE_COMPARE_TIMEOUT_S"

MAX_STDOUT_BYTES = 20 * 1024 * 1024
STDERR_TAIL_LINES = 200
_MAX_STDERR_LINE = 64 * 1024
_MAX_TAIL_CHARS = 100_000
_POLL_SECONDS = 0.25
_JOIN_SECONDS = 10.0

DEVICES_TIMEOUT_S = 30
# Allow the CLI's 30-second calibration lock wait to return its own busy refusal.
CALIBRATION_SYNC_TIMEOUT_S = 60
SCORE_BASE_S, SCORE_PER_FILE_S, DEEP_DV_PER_FILE_S = 60, 45, 900
COMPARE_TIMEOUT_S = 6 * 3600
COMPARE_PER_PAIR_S = {"fast": 1800, "accurate": 9000}

ERROR_KINDS = ("usage", "no_files", "ffprobe_missing", "failed_pairs", "contract", "timeout", "failed", "unavailable")
_EXIT_KIND = {2: "usage", 3: "no_files", 4: "ffprobe_missing"}

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_LANG_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
_FS_VALUES = ("fat32", "exfat", "ntfs")
_MODES = ("fast", "accurate")
_DIR_FORBIDDEN = set('&^%!"\'<>|')
_MAX_DIR_LEN = 1000
_MAX_WEIGHT = 1000.0


# ── exceptions ───────────────────────────────────────────────────────────────────

class SuiteUnavailable(Exception):
    """The suite cannot be located (D11 answers 503)."""


class SuiteArgumentError(ValueError):
    """A value failed validation before any process was started (D11 answers 400/422)."""


class SuiteError(Exception):
    """A suite call failed. ``kind`` is one of ``ERROR_KINDS``; ``stderr_tail`` the last stderr lines."""

    def __init__(self, kind: str, message: str, *, exit_code: Optional[int] = None, stderr_tail: str = "") -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown suite error kind {kind!r}")
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class SuiteDocument(dict):
    """A validated CLI document (a plain dict) that also carries ``exit_code`` and ``stderr_tail``."""

    exit_code: int = 0
    stderr_tail: str = ""


# ── resolution ───────────────────────────────────────────────────────────────────

_SHELL_LAUNCHERS = ("cmd", "powershell", "pwsh", "wscript", "cscript")


def _is_unsafe_launcher(path: str) -> bool:
    """True for a batch file in any disguise and for shell interpreters.

    Windows ignores trailing spaces and dots when it opens a file and treats ``name::$DATA`` as the same file, so
    ``fake.bat `` would still run as a batch file. Any ``:`` after the drive letter is therefore refused, trailing
    spaces/dots are removed before the extension is checked, and cmd/powershell/pwsh/wscript/cscript are refused."""
    text = str(path)
    if ":" in text[2:]:
        return True
    base = os.path.basename(text.replace("/", os.sep).rstrip(" ."))
    stem, ext = os.path.splitext(base)
    if ext.lower() in (".bat", ".cmd"):
        return True
    return (stem if ext.lower() in (".exe", ".com") else base).lower() in _SHELL_LAUNCHERS


def _build_env(environ: Mapping[str, str], root: str, kind: str = "venv", prefix: str = "") -> Dict[str, str]:
    """What ``setup\\_env.bat`` and ``run_suite.bat`` set, as environment overrides."""
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1",
           "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"}
    parts: List[str] = []
    for sub in (("tools", "bin"), ("ffmpeg", "bin")):          # _env.bat prepends ffmpeg\bin first, then tools\bin
        d = os.path.join(root, *sub)
        if os.path.isdir(d):
            parts.append(d)
    if kind.lower() == "conda" and prefix:
        parts += [prefix, os.path.join(prefix, "Library", "mingw-w64", "bin"), os.path.join(prefix, "Library", "usr", "bin"),
                  os.path.join(prefix, "Library", "bin"), os.path.join(prefix, "Scripts")]
    if parts:
        old_path = environ.get("PATH", "")
        env["PATH"] = os.pathsep.join(parts + ([old_path] if old_path else []))
    return env


def _read_env_path(env_path: Path) -> Dict[str, str]:
    try:
        text = Path(env_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip().lower()] = value.strip().strip('"')
    return values


def resolve_suite(environ: Optional[Mapping[str, str]] = None,
                  env_path: Optional[Path] = None) -> "tuple[List[str], Dict[str, str]]":
    """``(argv_prefix, env_overrides)`` from the environment variables, else from ``env.path``.

    The root folder is ``env_overrides["PYTHONPATH"]``. Never returns a batch file. Raises ``SuiteUnavailable``."""
    environ = os.environ if environ is None else environ
    py, root, kind, prefix = environ.get(ENV_PYTHON), environ.get(ENV_ROOT), "venv", ""
    if not (py and root):
        env_path = Path(DEFAULT_ENV_PATH if env_path is None else env_path)
        values = _read_env_path(env_path)
        py = values.get("python")
        root = str(env_path.parent.parent) if py else None
        kind, prefix = values.get("kind", "venv"), values.get("prefix", "")
    if not py or not root:
        raise SuiteUnavailable("The VideoSuite installation was not found (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT).")
    if _is_unsafe_launcher(py):
        raise SuiteUnavailable("The configured Python is a batch file or a shell; a real python.exe is required.")
    if not (os.path.isabs(py) and os.path.isfile(py)):
        raise SuiteUnavailable("The configured Python executable does not exist.")
    if not os.path.isdir(root):
        raise SuiteUnavailable("The VideoSuite root folder does not exist.")
    return [py, "-B", "-m", "videosuite"], _build_env(environ, root, kind, prefix)


def taskkill_tree(pid: int, *, timeout: float = 15) -> None:
    """Kill a process and all its children. Best effort: failures are swallowed (the caller then tries ``proc.kill``)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout, check=False, shell=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


# ── argument validation ──────────────────────────────────────────────────────────

# The CLI reads the list file with ``str.splitlines()`` and ``strip()``: \v \f \x1c-\x1e \x85 U+2028 U+2029 are line
# separators for it (one such path would become two) and surrounding white space would be removed. Such values are
# refused, together with every control character.
_LINE_BREAKS = frozenset("\v\f\x1c\x1d\x1e\x85  ")


def _text_problem(value: str) -> Optional[str]:
    """Why ``value`` cannot travel through a line-based list file (or the command line) unchanged, else ``None``."""
    if not value.strip():
        return "is empty"
    if any(ord(c) < 32 or c in _LINE_BREAKS for c in value) or len(value.splitlines()) != 1:
        return "contains a line separator or control character"
    if value != value.strip():
        return "starts or ends with white space"
    return None


def _check_paths(paths: Any) -> List[str]:
    if not isinstance(paths, (list, tuple)) or not paths:
        raise SuiteArgumentError("paths must be a non-empty list")
    out: List[str] = []
    for i, p in enumerate(paths, 1):
        if not isinstance(p, str):
            raise SuiteArgumentError(f"path {i} must be a string")
        problem = _text_problem(p)
        if problem:
            raise SuiteArgumentError(f"path {i} {problem}")
        out.append(p)
    return out


def _calibration_paths(paths: Any) -> List[str]:
    """Plan/build can discover an entirely invented kit without source files."""
    if isinstance(paths, (list, tuple)) and not paths:
        return []
    return _check_paths(paths)


def _check_ids(values: Any, what: str) -> List[str]:
    if not isinstance(values, (list, tuple)):
        raise SuiteArgumentError(f"{what} must be a list")
    return [_check_name(value, what) for value in values]


def _calibration_text(value: Any, what: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not (empty and value == "") and _text_problem(value)):
        raise SuiteArgumentError(f"{what} must be text without control characters")
    return value


def _check_kit(value: Any) -> str:
    # Build ids include a 12-hex nonce, a hyphen and a full SHA256 (77 characters).
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise SuiteArgumentError("kit is not a valid id")
    return value


def _check_name(value: Any, what: str) -> str:
    if not (isinstance(value, str) and _NAME_RE.fullmatch(value)):
        raise SuiteArgumentError(f"{what} is not a valid id")
    return value


def _check_lang(value: Any) -> str:
    if not (isinstance(value, str) and _LANG_RE.fullmatch(value)):
        raise SuiteArgumentError("lang is not a valid language code")
    return value


def _check_fs(value: Any) -> Optional[str]:
    if value is None:
        return None
    if value not in _FS_VALUES:
        raise SuiteArgumentError("fs must be fat32, exfat or ntfs")
    return value


def _check_weight(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= _MAX_WEIGHT:
        raise SuiteArgumentError(f"{what} must be a number from 0 to {int(_MAX_WEIGHT)}")
    return float(value)


def _check_dir(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_DIR_LEN:
        raise SuiteArgumentError(f"{what} must be a folder path")
    problem = _text_problem(value)
    if problem:
        raise SuiteArgumentError(f"{what} {problem}")
    if any(c in _DIR_FORBIDDEN for c in value):
        raise SuiteArgumentError(f"{what} contains a character that is not allowed")
    return value


def _check_top(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 6:
        raise SuiteArgumentError("top must be a whole number from 1 to 6")
    return value


def _check_mode(value: Any) -> str:
    if value not in _MODES:
        raise SuiteArgumentError("mode must be fast or accurate")
    return value


def _partial_args(real_size: Any, real_name: Any, coverage: Any) -> List[str]:
    if isinstance(real_size, bool) or not isinstance(real_size, int) or not 1 <= real_size <= 2**50:
        raise SuiteArgumentError("real_size must be a whole number of bytes")
    if (not isinstance(real_name, str) or _text_problem(real_name) is not None
            or "/" in real_name or "\\" in real_name or len(real_name) > 255):
        raise SuiteArgumentError("real_name must be a plain file name")
    if coverage not in ("head", "head_tail"):
        raise SuiteArgumentError("coverage must be head or head_tail")
    return [f"--real-size={real_size}", f"--real-name={real_name}", f"--coverage={coverage}"]


def _run_args(results: Any, device: Any, player: Any, primary_player: Any, fs: Any, lang: Any,
              audio_output: Any) -> List[str]:
    """Plan 4B selection: ``primary_player`` is the owner's chosen player, ``player`` the evaluation selector.

    Overall never names a device, player, primary player, file system or audio output (the suite does not load
    the catalogue). Device/Both need one specific device and an explicit evaluation selector; when a primary is
    given the selector is ``all`` (alternatives) or that same primary. Contradictions are refused, never
    rewritten."""
    if results not in RESULTS:
        raise SuiteArgumentError("results must be overall, device or both")
    if audio_output is not None and audio_output not in AUDIO_OUTPUTS:
        raise SuiteArgumentError("audio_output must be tv_speakers, soundbar or avr")
    if results == "overall":
        if (device not in (None, "all") or player not in (None, "all") or primary_player is not None
                or audio_output is not None or fs is not None):
            raise SuiteArgumentError("overall results take no device, player, file system or audio output")
        return ["--results=overall", f"--lang={_check_lang(lang)}"]
    if device in (None, "all") or player is None:
        raise SuiteArgumentError("device results need one specific device and a player selection")
    args = [f"--results={results}"] + SuiteClient._device_args(device, player, fs, lang)
    if primary_player is not None:
        primary = _check_name(primary_player, "primary_player")
        if primary == "all" or player not in ("all", primary):
            raise SuiteArgumentError("player must be all or the primary player")
        args.append(f"--primary-player={primary}")
    if audio_output is not None:
        args.append(f"--audio-output={audio_output}")
    return args


def _check_snapshot(value: Any) -> Dict[str, Any]:
    if not (isinstance(value, dict) and value.get("schema") == SNAPSHOT_SCHEMA
            and isinstance(value.get("records"), list) and isinstance(value.get("candidates"), list)
            and isinstance(value.get("records_digest"), str) and _integer(value.get("facts_version"))):
        raise SuiteArgumentError("records must be a videosuite-record-snapshot/1 object")
    _snapshot_bytes(value)
    return value


def _snapshot_bytes(value: Dict[str, Any]) -> bytes:
    import json
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise SuiteArgumentError("records must be finite JSON") from None
    if len(data) > MAX_STDOUT_BYTES:
        raise SuiteArgumentError("records snapshot is larger than 20 MB")
    return data


# ── document validation ──────────────────────────────────────────────────────────

_REQUIRED: Dict[str, Dict[str, Any]] = {
    "version": {"schemas": dict, "capabilities": list, "facts_version": int},
    "devices": {"devices": list},
    "facts": {"records": list},
    "score": {"records": list},
    "rank": {"quality": list, "same_picture_groups": list, "not_compared": list, "missing_pairs": list,
             "weights": dict, "devices": dict},
    "compare": {"mode": str, "estimate": dict, "shortlist": list, "dropped": list, "pairs": list, "failed": int},
    "calibrate-plan": {"catalogue_version": int, "players": list, "tests": list},
    "calibrate-build": {"kit_id": str, "out": str, "descriptor_path": str, "total_bytes": int,
                        "descriptors": list, "unavailable": list},
    "calibrate-session": {"kit_id": str, "session": dict},
    "calibrate-record": {"answer": dict, "summary": dict},
    "calibrate-status": {"recovery_required": bool, "kit": (dict, type(None)),
                         "session": (dict, type(None)), "answers": list, "summary": dict},
    "calibrate-apply": {"revision": str, "applied": bool, "recovery_required": bool,
                        "changes": list, "conflicts": list, "skipped": list},
    "calibrate-report": {"note": str, "kit_active": bool, "recovery_required": bool,
                         "files": list, "best_player_changes": list},
    # Plan 4B (VideoSuite 0.7.0): read-only planning and metadata recommendations.
    "compare-plan": {"results": str, "selection": str, "mode": str, "records": list, "candidates": list,
                     "pairs": list, "estimate": dict, "records_digest": str, "failures": list},
    "quick": {"results": str, "measurement": str, "records": list, "overall": (dict, type(None)), "devices": dict,
              "selected": dict, "alternatives": list, "failures": list},
}

RESULTS = ("overall", "device", "both")
AUDIO_OUTPUTS = ("tv_speakers", "soundbar", "avr")
EVIDENCE_WORDS = ("official", "owner", "reviewed", "forum", "inferred", "unknown")
SNAPSHOT_SCHEMA = "videosuite-record-snapshot/1"
_QUICK_STATUSES = ("estimated", "tie", "insufficient")
_MEASURED_KEYS = frozenset({"quality_score", "device_score"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


def _contract(message: str, stderr_tail: str = "", exit_code: Optional[int] = None) -> SuiteError:
    return SuiteError("contract", message, exit_code=exit_code, stderr_tail=stderr_tail)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


_VERDICTS = ("PLAYS_WELL", "PLAYS_DEGRADED", "RISKY", "WONT_PLAY")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_calibration(command: str, doc: dict, require: Callable, fields: Callable) -> None:
    """Frozen calibration fields; rule values and verification payloads remain opaque JSON."""
    string = lambda v: isinstance(v, str)
    nullable_string = lambda v: v is None or string(v)
    boolean = lambda v: isinstance(v, bool)
    array = lambda v: isinstance(v, list)
    obj = lambda v: isinstance(v, dict)
    count = lambda v: _integer(v) and v >= 0
    kind = lambda v: v in ("invented", "excerpt", "manual")
    test_class = lambda v: v in ("decisive", "informational")
    anything = lambda v: True

    def prediction(row, path):
        fields(row, path, {"outcome": nullable_string, "text": string, "evidence": string})

    def session(row, path):
        fields(row, path, {"tv_software": string, "audio_output": nullable_string,
                           "usb_filesystem": nullable_string, "confirmed_at": string})

    def summary(row, path):
        fields(row, path, {key: count for key in ("done", "total", "matches", "differs")})

    def answer(row, path):
        fields(row, path, {key: string for key in ("kit_id", "test", "player", "outcome", "note", "at")})
        fields(row, path, {"conditions": obj, "older_tv_software": boolean,
                           "match": lambda v: v in ("matches", "differs", "unsure", "none")})
        fields(row["conditions"], path + ".conditions", {"audio_output": nullable_string,
               "usb_filesystem": nullable_string, "tv_software": string})

    def outcomes(rows, path, descriptor=False):
        for i, row in enumerate(rows):
            checks = {"id": string, "text": string}
            if descriptor:
                checks["value"] = anything
            fields(row, f"{path}[{i}]", checks)

    def descriptor(row, path):
        fields(row, path, {key: string for key in ("test", "player", "title", "instruction", "question")})
        fields(row, path, {"class": test_class, "rule": nullable_string, "outcomes": array,
                           "conditions": _strings, "manual": boolean, "file": nullable_string,
                           "source": obj, "prediction": obj})
        outcomes(row["outcomes"], path + ".outcomes", True)
        fields(row["source"], path + ".source", {"kind": kind, "file": nullable_string,
               "verification": lambda v: v is None or obj(v)})
        prediction(row["prediction"], path + ".prediction")

    def descriptors(rows, path):
        for i, row in enumerate(rows):
            descriptor(row, f"{path}[{i}]")

    if command == "calibrate-plan":
        require(_integer(doc["catalogue_version"]), "catalogue_version")
        require(_strings(doc["players"]), "players")
        for i, row in enumerate(doc["tests"]):
            path = f"tests[{i}]"
            fields(row, path, {key: string for key in ("test", "title", "instruction", "question")})
            fields(row, path, {"class": test_class, "players": _strings, "manual": boolean,
                               "outcomes": array, "prediction": obj, "source": obj})
            outcomes(row["outcomes"], path + ".outcomes")
            for player in row["players"]:
                require(player in row["prediction"], path + ".prediction." + player)
            for player, pred in row["prediction"].items():
                prediction(pred, path + ".prediction." + player)
            fields(row["source"], path + ".source", {"kind": kind,
                   "status": lambda v: v in ("ready", "needs_real_file", "unavailable"),
                   "file": nullable_string, "est_bytes": lambda v: v is None or count(v), "reason": nullable_string})
    elif command == "calibrate-build":
        require(count(doc["total_bytes"]), "total_bytes")
        descriptors(doc["descriptors"], "descriptors")
        for i, row in enumerate(doc["unavailable"]):
            fields(row, f"unavailable[{i}]", {"test": string, "reason": string})
    elif command == "calibrate-session":
        session(doc["session"], "session")
    elif command == "calibrate-record":
        answer(doc["answer"], "answer")
        summary(doc["summary"], "summary")
    elif command == "calibrate-status":
        if doc["kit"] is not None:
            fields(doc["kit"], "kit", {"id": string, "catalogue_version": _integer,
                   "built_at": string, "out": string, "descriptors": array})
            descriptors(doc["kit"]["descriptors"], "kit.descriptors")
        if doc["session"] is not None:
            session(doc["session"], "session")
        for i, row in enumerate(doc["answers"]):
            answer(row, f"answers[{i}]")
        summary(doc["summary"], "summary")
    elif command == "calibrate-apply":
        for i, row in enumerate(doc["changes"]):
            path = f"changes[{i}]"
            fields(row, path, {key: string for key in ("id", "test", "player", "reason")})
            fields(row, path, {"rule": nullable_string,
                   "action": lambda v: v in ("confirm", "change", "veto", "skip", "undo", "left"),
                   "previous": anything, "value": anything, "veto": boolean})
        for i, row in enumerate(doc["conflicts"]):
            fields(row, f"conflicts[{i}]", {"rule": string, "player": string, "tests": _strings})
        for i, row in enumerate(doc["skipped"]):
            fields(row, f"skipped[{i}]", {key: string for key in ("test", "player", "reason")})
    elif command == "calibrate-report":
        require(doc["note"] == "playback only, not a quality ranking", "note")
        for i, row in enumerate(doc["files"]):
            path = f"files[{i}]"
            fields(row, path, {"path": string, "player": string, "before": obj, "after": obj, "changed": boolean})
            for side in ("before", "after"):
                fields(row[side], path + "." + side, {"verdict": lambda v: v in _VERDICTS, "score": _number,
                       "evidence": string, "effective_audio": nullable_string, "effective_hdr": string})
        for i, row in enumerate(doc["best_player_changes"]):
            path = f"best_player_changes[{i}]"
            fields(row, path, {"path": string, "device": string, "before": obj, "after": obj})
            for side in ("before", "after"):
                fields(row[side], path + "." + side, {"player": string, "verdict": lambda v: v in _VERDICTS})


def _count(value: Any) -> bool:
    return _integer(value) and value >= 0


def _nullable_count(value: Any) -> bool:
    return value is None or _count(value)


def _has_measured_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key in _MEASURED_KEYS or _has_measured_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_measured_key(item) for item in value)
    return False


def _validate_plan4b(command: str, doc: dict, require: Callable, fields: Callable) -> None:
    """Plan 4B fields: required in compare-plan/quick, additive (validated when present) in compare/rank."""
    string = lambda v: isinstance(v, str)
    nullable_string = lambda v: v is None or isinstance(v, str)
    digest = lambda v: isinstance(v, str) and _DIGEST_RE.fullmatch(v) is not None

    def records(rows: Any, path: str) -> None:
        require(isinstance(rows, list), path)
        for i, row in enumerate(rows):
            fields(row, f"{path}[{i}]", {"id": string, "source": lambda v: isinstance(v, dict)})
            fields(row["source"], f"{path}[{i}].source", {"path": string})
            if "partial" in row["source"]:
                require(isinstance(row["source"]["partial"], bool), f"{path}[{i}].source.partial")

    def candidates(rows: Any) -> None:
        require(isinstance(rows, list), "candidates")
        for i, row in enumerate(rows):
            path = f"candidates[{i}]"
            fields(row, path, {"path": string, "id": nullable_string, "status": lambda v: v in ("ready", "unreadable"),
                               "reason": nullable_string, "size": _nullable_count, "mtime_ns": _nullable_count})
            require((row["status"] == "ready") == isinstance(row["id"], str), path + ".id")

    def selection() -> None:
        if "selected" in doc:
            fields(doc["selected"], "selected", {"device": string, "player": string})
        if "evaluated_players" in doc:
            require(_strings(doc["evaluated_players"]), "evaluated_players")

    def order(value: Any, path: str) -> None:
        fields(value, path, {"groups": lambda v: isinstance(v, list), "winner_ids": _strings,
                             "coverage_complete": lambda v: isinstance(v, bool)})
        ids = set()
        for i, group in enumerate(value["groups"]):
            gpath = f"{path}.groups[{i}]"
            fields(group, gpath, {"rank": _nullable_count, "ids": lambda v: _strings(v) and bool(v),
                                  "status": lambda v: v in _QUICK_STATUSES, "basis": lambda v: isinstance(v, dict),
                                  "limitations": _strings})
            ids.update(group["ids"])
            for key, row in group["basis"].items():
                bpath = f"{gpath}.basis.{key}"
                require(isinstance(row, dict), bpath)
                if "playback" in row:
                    fields(row["playback"], bpath + ".playback", {
                        "verdict": lambda v: v in _VERDICTS, "score": _number,
                        "score_range": lambda v: isinstance(v, list) and len(v) == 2 and all(_number(x) for x in v),
                        "evidence": lambda v: v in EVIDENCE_WORDS, "effective_hdr": string,
                        "effective_audio": nullable_string, "issues": lambda v: isinstance(v, list)})
        require(set(value["winner_ids"]) <= ids, path + ".winner_ids")
        require(not _has_measured_key(value), path + " (metadata estimates never carry measured scores)")

    if command in ("compare", "rank", "compare-plan"):
        if "results" in doc:
            require(doc["results"] in RESULTS, "results")
        if "selection" in doc:
            require(doc["selection"] == "all", "selection")
        if "records_digest" in doc:
            require(digest(doc["records_digest"]), "records_digest")
        if "candidates" in doc:
            candidates(doc["candidates"])
        if "coverage" in doc:
            fields(doc["coverage"], "coverage", {key: _count for key in
                   ("expected_pairs", "scheduled_pairs", "completed_pairs", "missing_pairs")})
        if "records" in doc:
            records(doc["records"], "records")
        selection()
    if command == "compare-plan":
        require(doc["mode"] == "accurate", "mode")
        fields(doc["estimate"], "estimate", {"pairs": _count, "minutes": _count})
        ids = {r["id"] for r in doc["records"]}
        for i, pair in enumerate(doc["pairs"]):
            fields(pair, f"pairs[{i}]", {"a": lambda v: v in ids, "b": lambda v: v in ids})
        for i, row in enumerate(doc["failures"]):
            fields(row, f"failures[{i}]", {"path": string, "reason": string})
    elif command == "quick":
        require(doc["results"] in RESULTS, "results")
        require(doc["measurement"] == "metadata", "measurement")
        records(doc["records"], "records")
        require((doc["overall"] is None) == (doc["results"] == "device"), "overall")
        if doc["overall"] is not None:
            order(doc["overall"], "overall")
        require((doc["results"] == "overall") == (not doc["devices"]), "devices")
        for key, value in doc["devices"].items():
            order(value, "devices." + key)
        fields(doc["selected"], "selected", {"device": nullable_string, "player": nullable_string})
        for i, row in enumerate(doc["alternatives"]):
            path = f"alternatives[{i}]"
            fields(row, path, {"device": string, "player": string, "order": lambda v: isinstance(v, dict)})
            order(row["order"], path + ".order")
            require(row["player"] != doc["selected"]["player"], path + ".player")
        for i, row in enumerate(doc["failures"]):
            fields(row, f"failures[{i}]", {"path": string, "reason": string})
        if "settings" in doc:
            require(doc["settings"] is None or isinstance(doc["settings"], dict), "settings")


def _validate_deep(command: str, doc: dict) -> None:
    """Validate consumer-facing fields while accepting additive, unknown fields."""
    def require(valid: bool, path: str) -> None:
        if not valid:
            raise _contract(f"The {command} document has an invalid {path} entry.")

    def fields(row: Any, path: str, checks: Mapping[str, Callable[[Any], bool]]) -> None:
        require(isinstance(row, dict), path)
        for key, check in checks.items():
            require(key in row and check(row[key]), f"{path}.{key}" if path else key)

    string = lambda value: isinstance(value, str)
    nullable_string = lambda value: value is None or isinstance(value, str)
    nullable_number = lambda value: value is None or _number(value)
    verdict = lambda value: value in _VERDICTS
    if command in ("compare", "rank", "compare-plan", "quick"):
        _validate_plan4b(command, doc, require, fields)
    if command.startswith("calibrate-"):
        _validate_calibration(command, doc, require, fields)
    elif command == "version":
        require(_strings(doc["capabilities"]), "capabilities")
        if "engine_tag" in doc:
            require(nullable_string(doc["engine_tag"]), "engine_tag")
    elif command in ("facts", "score"):
        for index, record in enumerate(doc["records"]):
            path = f"records[{index}]"
            fields(record, path, {"id": string, "source": lambda value: isinstance(value, dict)})
            fields(record["source"], path + ".source", {"path": string})
            if "partial" in record["source"]:
                require(isinstance(record["source"]["partial"], bool), path + ".source.partial")
            if command == "score":
                require(isinstance(record.get("playback"), dict), path + ".playback")
                for key, playback in record["playback"].items():
                    playback_path = path + ".playback." + key
                    fields(playback, playback_path, {
                        "verdict": verdict, "score": _number,
                        "score_range": lambda value: isinstance(value, list) and len(value) == 2
                        and all(_number(item) for item in value),
                        "evidence": string, "effective_hdr": string, "effective_audio": nullable_string,
                        "issues": lambda value: isinstance(value, list), "hints": _strings,
                    })
                    for issue_index, issue in enumerate(playback["issues"]):
                        fields(issue, f"{playback_path}.issues[{issue_index}]",
                               {"severity": string, "message": string})
                if "best_player" in record:
                    require(isinstance(record["best_player"], dict), path + ".best_player")
                    for key, best in record["best_player"].items():
                        fields(best, path + ".best_player." + key, {"player": string, "reason": string})
    elif command == "rank":
        for index, row in enumerate(doc["quality"]):
            fields(row, f"quality[{index}]", {"id": string, "label": string, "rank": _integer,
                                             "games": _integer, "quality_score": nullable_number})
        for key, device in doc["devices"].items():
            path = "devices." + key
            fields(device, path, {"ranking": lambda value: isinstance(value, list),
                                 "will_not_play": lambda value: isinstance(value, list),
                                 "not_compared": lambda value: isinstance(value, list),
                                 "crossing_note": nullable_string})
            for index, row in enumerate(device["ranking"]):
                fields(row, f"{path}.ranking[{index}]", {
                    "label": string, "device_rank": _integer, "verdict": verdict,
                    "playback_score": _number, "quality_score": nullable_number,
                    "why": nullable_string, "reasons": _strings,
                })
        for key in ("stale_reports", "legacy_reports", "head_to_head_conflicts"):
            if key in doc:
                require(isinstance(doc[key], list), key)
    elif command == "devices":
        for index, device in enumerate(doc["devices"]):
            path = f"devices[{index}]"
            fields(device, path, {"id": string, "label": string, "players": lambda value: isinstance(value, list)})
            for player_index, player in enumerate(device["players"]):
                player_path = f"{path}.players[{player_index}]"
                fields(player, player_path, {"id": string, "label": string,
                                            "default": lambda value: isinstance(value, bool),
                                            "rule_counts": lambda value: isinstance(value, dict)})
                for key, count in player["rule_counts"].items():
                    require(isinstance(key, str) and _integer(count), player_path + ".rule_counts." + str(key))


def parse_document(command: str, raw: bytes, *, oversize: bool = False) -> SuiteDocument:
    """Validate the stdout of ``videosuite <command> --json``; ``SuiteError(kind="contract")`` on any violation."""
    import json

    if oversize or len(raw) > MAX_STDOUT_BYTES:
        raise _contract("The suite output is larger than 20 MB.")
    if not raw.strip():
        raise _contract("The suite printed nothing on stdout.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _contract("The suite output is not valid UTF-8.") from None
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _contract("The suite output is not valid JSON.") from None
    return SuiteDocument(validate_document(command, doc))


def validate_document(command: str, doc: Any) -> dict:
    """Validate an already-parsed ``videosuite-<command>/1`` document (also nested in run results); returns it."""
    if not isinstance(doc, dict):
        raise _contract("The suite output is not a JSON object.")
    if doc.get("schema") != f"videosuite-{command}/1":
        raise _contract("The suite output has an unexpected schema.")
    if not isinstance(doc.get("tool_version"), str):
        raise _contract("The suite output has no tool_version.")
    for key, typ in _REQUIRED[command].items():
        value = doc.get(key)
        if key not in doc or not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            raise _contract(f"The suite output lacks a valid '{key}'.")
    if command == "compare":
        if doc["mode"] not in _MODES or doc["failed"] < 0 or not all(isinstance(p, dict) for p in doc["pairs"]):
            raise _contract("The compare document has invalid values.")
    _validate_deep(command, doc)
    return doc


# ── process plumbing ─────────────────────────────────────────────────────────────

class _StdoutReader(threading.Thread):
    """Collects stdout whole, up to the cap (then keeps draining and flags ``oversize``)."""

    def __init__(self, stream: Any) -> None:
        super().__init__(daemon=True, name="suite-stdout")
        self.stream = stream
        self.chunks: List[bytes] = []
        self.size = 0
        self.oversize = False

    def run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    return
                if self.oversize:
                    continue
                self.size += len(chunk)
                if self.size > MAX_STDOUT_BYTES:
                    self.oversize = True
                    self.chunks.clear()
                else:
                    self.chunks.append(chunk)
        except (OSError, ValueError):
            return

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


class _StderrReader(threading.Thread):
    """Drains stderr line by line: ``on_line`` gets each line; the last 200 are kept."""

    def __init__(self, stream: Any, on_line: Optional[Callable[[str], None]]) -> None:
        super().__init__(daemon=True, name="suite-stderr")
        self.stream = stream
        self.on_line = on_line
        self.tail: "collections.deque[str]" = collections.deque(maxlen=STDERR_TAIL_LINES)

    def run(self) -> None:
        try:
            for raw in iter(lambda: self.stream.readline(_MAX_STDERR_LINE), b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.tail.append(line)
                if self.on_line is not None:
                    try:
                        self.on_line(line)
                    except Exception:               # a progress callback must never stop the drain
                        pass
        except (OSError, ValueError):
            return

    @property
    def text(self) -> str:
        return "\n".join(self.tail)[-_MAX_TAIL_CHARS:]


class _Outcome:
    def __init__(self, returncode: Optional[int], stdout: bytes, oversize: bool, stderr_tail: str, reason: Optional[str]) -> None:
        self.returncode, self.stdout, self.oversize, self.stderr_tail, self.reason = returncode, stdout, oversize, stderr_tail, reason


class SuiteClient:
    """Runs ``videosuite`` commands and returns validated documents.

    ``popen`` (default ``subprocess.Popen``), ``killer`` (default ``taskkill_tree``) and ``clock`` are injectable for tests."""

    def __init__(self, argv_prefix: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None, *,
                 popen: Optional[Callable[..., Any]] = None, killer: Optional[Callable[[int], None]] = None,
                 clock: Optional[Callable[[], float]] = None, environ: Optional[Mapping[str, str]] = None,
                 env_path: Optional[Path] = None, root: Optional[str] = None,
                 job: Optional[Any] = None) -> None:
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        if argv_prefix is not None:
            prefix = [str(a) for a in argv_prefix]
            if not prefix or _is_unsafe_launcher(prefix[0]):
                raise SuiteUnavailable("argv_prefix must start with a real executable, not a batch file or a shell.")
            overrides = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONSAFEPATH": "1"}
            if root:
                overrides["PYTHONPATH"] = str(root)
            overrides.update(env or {})
        else:
            prefix, overrides = resolve_suite(self._environ, env_path)
            overrides.update(env or {})
            root = overrides.get("PYTHONPATH")
        # Apply to injected prefixes too: every command/helper uses this prefix.
        if "-B" not in prefix[1:]:
            prefix.insert(1, "-B")
        overrides["PYTHONDONTWRITEBYTECODE"] = "1"
        self.argv_prefix: List[str] = prefix
        self.env_overrides: Dict[str, str] = overrides
        self.root: Optional[str] = str(root) if root else None          # working folder of the child
        self._popen = popen or subprocess.Popen
        self._job = job if job is not None else (proc_guard.default_job() if popen is None else proc_guard.NULL_JOB)
        self._killer = killer or taskkill_tree
        self._default_killer = killer is None
        self._clock = clock or time.monotonic

    # ── timeouts ─────────────────────────────────────────────────────────────────

    def _timeout_devices(self) -> float:
        return DEVICES_TIMEOUT_S

    def _timeout_score_rank(self, n_files: int, deep_dv: bool) -> float:
        return SCORE_BASE_S + n_files * (SCORE_PER_FILE_S + (DEEP_DV_PER_FILE_S if deep_dv else 0))

    def _timeout_compare(self, n_files: int = 0, mode: str = "fast", top: int = 4, *,
                         all_candidates: bool = False) -> float:
        """Safety ceiling for one compare process, never displayed as an ETA.

        Legacy shortlist runs compare at most ``2*top`` files; an all-candidates run compares every unordered pair
        of the ``n_files`` submitted files (50 files = 1225 pairs). A valid user override is used exactly."""
        raw = self._environ.get(ENV_COMPARE_TIMEOUT)
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            value = None
        if value is None or not math.isfinite(value) or value <= 0:
            n = n_files if all_candidates else min(n_files, 2 * top)
            pairs = n * (n - 1) // 2
            return max(COMPARE_TIMEOUT_S, pairs * COMPARE_PER_PAIR_S.get(mode, 9000) + 600)
        return value

    # ── commands ─────────────────────────────────────────────────────────────────

    def devices(self) -> SuiteDocument:
        outcome = self._run("devices", ["devices", "--json"], self._timeout_devices())
        return self._finish("devices", outcome)

    def version(self) -> SuiteDocument:
        outcome = self._run("version", ["version", "--json"], self._timeout_devices())
        return self._finish("version", outcome)

    def facts(self, paths: Sequence[str], deep_dv: bool = False, *,
              cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Facts only. ``cancel`` is additive: existing callers are unchanged; a cancelled call returns ``None``."""
        paths = _check_paths(paths)
        extra = ["--deep-dv"] if deep_dv else []
        return self._with_list("facts", paths, extra, self._timeout_score_rank(len(paths), deep_dv), cancel=cancel)

    def facts_partial(self, path: str, *, real_size: int, real_name: str, coverage: str = "head",
                      cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Facts of ONE partial download (Plan 4B Quick magnets): no device/player arguments, no deep Dolby Vision
        scan and no record cache (the suite never caches a partial record). Returns ``None`` when cancelled."""
        paths = _check_paths([path])
        extra = _partial_args(real_size, real_name, coverage)
        return self._with_list("facts", paths, extra, SCORE_BASE_S + SCORE_PER_FILE_S, cancel=cancel,
                               cleanup_budget=5.0)

    def plan_compare(self, paths: Sequence[str], out_dir: str, *, results: str, device: Optional[str] = None,
                     player: Optional[str] = None, primary_player: Optional[str] = None, fs: Optional[str] = None,
                     lang: str = "eng", audio_output: Optional[str] = None,
                     cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Read-only all-candidates plan (``videosuite-compare-plan/1``): facts, candidates, every unordered pair and
        the suite's own estimate. ``out_dir`` is syntactically required by the CLI but never created or touched."""
        paths = _check_paths(paths)
        out_dir = _check_dir(out_dir, "out_dir")
        extra = (["--all-candidates", "--plan", f"--out={out_dir}"]
                 + _run_args(results, device, player, primary_player, fs, lang, audio_output))
        return self._with_list("compare", paths, extra, self._timeout_score_rank(len(paths), False),
                               document="compare-plan", cancel=cancel)

    def quick(self, snapshot: Dict[str, Any], *, results: str, device: Optional[str] = None,
              player: Optional[str] = None, primary_player: Optional[str] = None, fs: Optional[str] = None,
              lang: str = "eng", audio_output: Optional[str] = None,
              cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Metadata recommendations (``videosuite-quick/1``) from a validated record snapshot; never pixels.

        The snapshot travels in a private JSON file (never argv); no path list is needed."""
        snapshot = _check_snapshot(snapshot)
        extra = _run_args(results, device, player, primary_player, fs, lang, audio_output)
        return self._with_list("quick", [], extra, self._timeout_score_rank(len(snapshot["records"]), False),
                               records=snapshot, use_list=False, cancel=cancel)

    def score(self, paths: Sequence[str], device: Optional[str] = "all", player: Optional[str] = "all",
              fs: Optional[str] = None, lang: str = "eng", deep_dv: bool = False, *,
              cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Score the files. Returns the document, or ``None`` if cancelled via ``cancel`` (the process tree is killed)."""
        paths = _check_paths(paths)
        extra = self._device_args(device, player, fs, lang) + (["--deep-dv"] if deep_dv else [])
        return self._with_list("score", paths, extra, self._timeout_score_rank(len(paths), deep_dv), cancel=cancel)

    def score_partial(self, path: str, *, real_size: int, real_name: str, coverage: str = "head",
                      device: Optional[str] = "all", player: Optional[str] = "all", fs: Optional[str] = None,
                      lang: str = "eng", cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Score one partial download using its original size and file name."""
        paths = _check_paths([path])
        extra = self._device_args(device, player, fs, lang) + _partial_args(real_size, real_name, coverage)
        return self._with_list("score", paths, extra, SCORE_BASE_S + SCORE_PER_FILE_S, cancel=cancel,
                               cleanup_budget=5.0)

    def rank(self, paths: Sequence[str], reports_dir: str, device: Optional[str] = "all", player: Optional[str] = "all",
             fs: Optional[str] = None, lang: str = "eng", wq: float = 0.5, wp: float = 0.5,
             deep_dv: bool = False, *, cancel: Optional[Callable[[], bool]] = None, results: str = "both",
             records: Optional[Dict[str, Any]] = None, audio_output: Optional[str] = None,
             primary_player: Optional[str] = None) -> Optional[SuiteDocument]:
        """Rank the files. Returns the document, or ``None`` if cancelled via ``cancel`` (the process tree is killed).

        Plan 4B keywords are additive: ``results`` (overall = neutral rank, no device data), ``records`` (a validated
        record snapshot sent as a private JSON file, no re-probe), ``audio_output`` and ``primary_player``. With
        none of them the argv is exactly the legacy one."""
        paths = _check_paths(paths)
        reports_dir = _check_dir(reports_dir, "reports_dir")
        wq, wp = _check_weight(wq, "wq"), _check_weight(wp, "wp")
        if wq == 0 and wp == 0:
            raise SuiteArgumentError("wq and wp cannot both be zero")
        unified = records is not None or results != "both" or audio_output is not None or primary_player is not None
        if unified:
            records = _check_snapshot(records) if records is not None else None
            if deep_dv and records is not None:
                raise SuiteArgumentError("deep_dv cannot be combined with a record snapshot")
            selection = _run_args(results, device, player, primary_player, fs, lang, audio_output)
        else:
            selection = self._device_args(device, player, fs, lang)
        extra = selection + [f"--reports={reports_dir}", f"--wq={wq!r}", f"--wp={wp!r}"]
        if deep_dv:
            extra.append("--deep-dv")
        return self._with_list("rank", paths, extra, self._timeout_score_rank(len(paths), deep_dv), cancel=cancel,
                               records=records)

    def compare(self, paths: Sequence[str], out_dir: str, mode: str = "fast", top: int = 4, *,
                on_line: Optional[Callable[[str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None, device: Optional[str] = "all",
                player: Optional[str] = "all", fs: Optional[str] = None, lang: str = "eng",
                deep_dv: bool = False, all_candidates: bool = False, results: str = "both",
                records: Optional[Dict[str, Any]] = None, audio_output: Optional[str] = None,
                primary_player: Optional[str] = None) -> Optional[SuiteDocument]:
        """Run the pixel comparison. Returns the document (also for exit code 1: ``failed > 0``), or ``None`` if cancelled.

        Plan 4B keywords are additive: ``all_candidates`` (accurate round robin of EVERY readable submitted file, no
        shortlist; requires ``mode="accurate"``), ``results``, ``records`` (private JSON snapshot transport),
        ``audio_output`` and ``primary_player``. With none of them the argv is exactly the legacy one."""
        paths = _check_paths(paths)
        out_dir = _check_dir(out_dir, "out_dir")
        mode, top = _check_mode(mode), _check_top(top)
        if not isinstance(all_candidates, bool):
            raise SuiteArgumentError("all_candidates must be a boolean")
        unified = (all_candidates or records is not None or results != "both" or audio_output is not None
                   or primary_player is not None)
        if unified:
            if all_candidates and mode != "accurate":
                raise SuiteArgumentError("an all-candidates comparison uses accurate mode")
            records = _check_snapshot(records) if records is not None else None
            if deep_dv and records is not None:
                raise SuiteArgumentError("deep_dv cannot be combined with a record snapshot")
            selection = _run_args(results, device, player, primary_player, fs, lang, audio_output)
        else:
            selection = self._device_args(device, player, fs, lang)
        extra = selection + [f"--out={out_dir}", f"--mode={mode}", f"--top={top}"]
        if all_candidates:
            extra.append("--all-candidates")
        if deep_dv:
            extra.append("--deep-dv")
        self._require_full_support()
        return self._with_list("compare", paths, extra,
                               self._timeout_compare(len(paths), mode, top, all_candidates=all_candidates),
                               on_line=on_line, cancel=cancel, records=records)

    def _require_full_support(self) -> None:
        """Refresh packaged engine checks before any pixel process/list/output exists."""
        if self.env_overrides.get("VIDEOSUITE_OFFLINE", self._environ.get("VIDEOSUITE_OFFLINE")) != "1":
            return
        import json
        from app_preflight import preflight
        try:
            if not self.root:
                raise ValueError("The packaged suite location is missing")
            release = Path(self.root).parent
            path = release / "release.json"
            if path.stat().st_size > MAX_STDOUT_BYTES:
                raise ValueError("The offline release manifest is invalid")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            data = self._environ.get("VIDEOLYZER_DATA_ROOT") or str(
                Path(self._environ.get("LOCALAPPDATA", str(Path.home()))) / "VideoComparator/app")
            checks = preflight(release, manifest, Path(data))
            if not checks:
                raise ValueError("Required engine support could not be verified")
            failed = [c["message"] for c in checks if c["status"] != "ok" and
                      (c["capability"] in {"full", "suite", "files"} or c["id"] == "dovi_tool")]
            if failed:
                raise ValueError("; ".join(failed))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SuiteError("unavailable", "Full comparison is unavailable: " + str(exc) +
                ". Repair the matching offline installation. Metadata analysis is still available.") from None

    def calibrate_plan(self, paths: Sequence[str], players: Sequence[str], *, deep_dv: bool = False,
                       cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Plan a kit; returns None when cancelled, including during deep discovery."""
        paths = _calibration_paths(paths)
        extra = [f"--player={p}" for p in _check_ids(players, "players")]
        if deep_dv:
            extra.append("--deep-dv")
        return self._with_list("calibrate-plan", paths, extra,
                               self._timeout_score_rank(len(paths), deep_dv), cancel=cancel)

    def calibrate_build(self, paths: Sequence[str], out_dir: str, players: Sequence[str], tests: Sequence[str], *,
                        deep_dv: bool = False, on_line: Optional[Callable[[str], None]] = None,
                        cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Build a kit; total_bytes in the returned document is authoritative."""
        paths = _calibration_paths(paths)
        out_dir = _check_dir(out_dir, "out_dir")
        players, tests = _check_ids(players, "players"), _check_ids(tests, "tests")
        extra = [f"--out={out_dir}", "--yes"] + [f"--player={p}" for p in players]
        if tests:
            extra.append("--tests=" + ",".join(tests))
        if deep_dv:
            extra.append("--deep-dv")
        # Empty selection means every catalogue test (currently T01..T16).
        # The base budget also accommodates the builder's 30-second lock wait.
        timeout = 600 + 120 * (len(tests) if tests else 16) + (DEEP_DV_PER_FILE_S * len(paths) if deep_dv else 0)
        return self._with_list("calibrate-build", paths, extra, timeout, on_line=on_line, cancel=cancel)

    def calibrate_status(self, kit: Optional[str] = None) -> SuiteDocument:
        return self._calibration_fast("status", [] if kit is None else [f"--kit={_check_kit(kit)}"])

    def calibrate_session(self, out: str, tv_software: str, audio_output: Optional[str],
                          usb_fs: Optional[str]) -> SuiteDocument:
        out = _check_dir(out, "out")
        tv_software = _calibration_text(tv_software, "tv_software")
        if audio_output is not None and audio_output not in ("tv_speakers", "soundbar", "avr"):
            raise SuiteArgumentError("audio_output must be tv_speakers, soundbar or avr")
        usb_fs = _check_fs(usb_fs)
        extra = [f"--kit-file={os.path.join(out, 'kit.json')}", f"--tv-software={tv_software}"]
        if audio_output is not None:
            extra.append(f"--audio-output={audio_output}")
        if usb_fs is not None:
            extra.append(f"--usb-fs={usb_fs}")
        return self._calibration_fast("session", extra)

    def calibrate_record(self, kit: str, test: str, player: str, outcome: str, note: str = "") -> SuiteDocument:
        extra = [f"--kit={_check_kit(kit)}", f"--test={_check_name(test, 'test')}",
                 f"--player={_check_name(player, 'player')}", f"--outcome={_check_name(outcome, 'outcome')}",
                 f"--note={_calibration_text(note, 'note', empty=True)}"]
        return self._calibration_fast("record", extra)

    def calibrate_apply(self, kit: str, *, preview: bool, revision: Optional[str] = None,
                        confirm_veto: Sequence[str] = ()) -> SuiteDocument:
        kit = _check_kit(kit)
        if not isinstance(preview, bool):
            raise SuiteArgumentError("preview must be a boolean")
        if not isinstance(confirm_veto, (list, tuple)):
            raise SuiteArgumentError("confirm_veto must be a list")
        ids = []
        for cid in confirm_veto:
            # Change ids contain colon-separated test/player/rule, including @container.
            if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,512}", cid):
                raise SuiteArgumentError("confirm_veto contains an invalid change id")
            ids.append(cid)
        extra = [f"--kit={kit}"]
        if preview:
            if revision is not None or ids:
                raise SuiteArgumentError("preview takes no revision or veto confirmations")
            extra.append("--preview")
        else:
            revision = _check_name(revision, "revision")
            extra.append(f"--revision={revision}")
            if ids:
                extra.append("--confirm-veto=" + ",".join(ids))
        return self._calibration_fast("apply", extra)

    def calibrate_undo(self, kit: str) -> SuiteDocument:
        return self._calibration_fast("apply", [f"--undo={_check_kit(kit)}"])

    def calibrate_report(self, kit: str, paths: Sequence[str], *, on_line: Optional[Callable[[str], None]] = None,
                         cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        kit, paths = _check_kit(kit), _check_paths(paths)
        return self._with_list("calibrate-report", paths, [f"--kit={kit}"],
                               self._timeout_score_rank(len(paths), False), on_line=on_line, cancel=cancel)

    # ── internals ────────────────────────────────────────────────────────────────

    def _calibration_fast(self, sub: str, extra: List[str]) -> SuiteDocument:
        command = "calibrate-" + sub
        return self._finish(command, self._run(command, ["calibrate", sub, "--json"] + extra,
                                               CALIBRATION_SYNC_TIMEOUT_S))

    @staticmethod
    def _device_args(device: Optional[str], player: Optional[str], fs: Optional[str], lang: str) -> List[str]:
        args = [f"--device={_check_name('all' if device is None else device, 'device')}",
                f"--player={_check_name('all' if player is None else player, 'player')}"]
        checked_fs = _check_fs(fs)
        if checked_fs:
            args.append(f"--fs={checked_fs}")
        args.append(f"--lang={_check_lang(lang)}")
        return args

    def _with_list(self, command: str, paths: List[str], extra: List[str], timeout: float, *,
                   on_line: Optional[Callable[[str], None]] = None,
                   cancel: Optional[Callable[[], bool]] = None,
                   cleanup_budget: Optional[float] = None, document: Optional[str] = None,
                   records: Optional[Dict[str, Any]] = None, use_list: bool = True) -> Optional[SuiteDocument]:
        """Run ``command`` with its private transport files in one temp folder that is removed on every exit.

        ``document`` names the expected output schema when it differs from the command word (``compare --plan``
        prints ``videosuite-compare-plan/1``); ``records`` is a validated snapshot written to a private JSON file."""
        tmp = tempfile.mkdtemp(prefix="vsuite_")
        try:
            argv_files: List[str] = []
            if use_list:
                list_file = os.path.join(tmp, "paths.txt")
                with open(list_file, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(paths) + "\n")
                # An empty list is valid for plan/build only; passing its file would
                # trigger the CLI's shared "give at least one file" validation.
                argv_files = ["--list", list_file] if paths else []
            words = ["calibrate", command[len("calibrate-"):]] if command.startswith("calibrate-") else [command]
            argv = words + argv_files + ["--json"] + extra
            if records is not None:
                records_file = os.path.join(tmp, "records.json")
                with open(records_file, "wb") as fh:
                    fh.write(_snapshot_bytes(records))
                argv.append(f"--records={records_file}")
            outcome = self._run(command, argv, timeout, on_line=on_line, cancel=cancel,
                                cleanup_budget=cleanup_budget)
            if outcome.reason == "cancel":
                return None
            return self._finish(document or command, outcome)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _process_env(self) -> Dict[str, str]:
        """The injected environment minus every inherited PYTHON* variable (PYTHONHOME, PYTHONSTARTUP, ...), plus ours."""
        env = {k: v for k, v in self._environ.items() if not k.upper().startswith("PYTHON")}
        env.update(self.env_overrides)
        return env

    def _bounded_cleanup_call(self, action: Callable[[], Any], deadline: float, *,
                              inline_on_failure: bool = False) -> None:
        """Bound cleanup with a worker; only explicitly safe actions fall back inline.

        The default tree killer has its own subprocess timeout. With no worker
        available, injected killers must return promptly: an arbitrary Python
        callback cannot be preempted on the calling thread. Never substitute a
        real taskkill for an injected killer.
        """
        def best_effort():
            try:
                action()
            except Exception:
                pass
        worker = threading.Thread(target=best_effort, daemon=True, name="suite-cleanup")
        try:
            worker.start()
        except RuntimeError:
            if inline_on_failure:
                best_effort()
            return
        worker.join(max(0.0, deadline - self._clock()))

    def _kill(self, proc: Any, *, deadline: Optional[float] = None) -> None:
        if deadline is not None:
            # Reserve time for direct kill, wait and pipe cleanup after taskkill.
            tree_deadline = min(deadline, self._clock() + 2.0)
            if self._default_killer:
                self._bounded_cleanup_call(
                    lambda: taskkill_tree(proc.pid, timeout=max(0.001, tree_deadline - self._clock())),
                    tree_deadline, inline_on_failure=True)
            else:
                self._bounded_cleanup_call(lambda: self._killer(proc.pid), tree_deadline, inline_on_failure=True)
            if proc.poll() is None:
                self._bounded_cleanup_call(proc.kill, min(deadline, self._clock() + 0.25), inline_on_failure=True)
            try:
                proc.wait(max(0.0, deadline - self._clock()))
            except Exception:
                pass
            return
        try:
            self._killer(proc.pid)
        except Exception:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass

    def _run(self, command: str, args: List[str], timeout: float, *, on_line: Optional[Callable[[str], None]] = None,
             cancel: Optional[Callable[[], bool]] = None,
             cleanup_budget: Optional[float] = None) -> _Outcome:
        argv = self.argv_prefix + args
        try:
            with proc_guard.workers.lock:
                if proc_guard.workers.stopping:
                    raise SuiteError("unavailable", "App is shutting down.")
                proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=self._process_env(), cwd=self.root, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                worker_id = "suite:" + str(proc.pid) + ":" + str(id(proc))
                owned = proc_guard.OwnedProcess(proc, force=lambda: self._kill(proc, deadline=self._clock() + 4))
                proc_guard.workers.register(worker_id, owned.cancel, owned.wait,
                    force=owned.force, job_id=proc_guard.owner_id())
        except OSError:
            raise SuiteError("unavailable", "The VideoSuite process could not be started.") from None
        try:
            self._job.assign(proc)
        except Exception:
            pass
        with _LIVE_LOCK:
            _LIVE.add(proc)
        try:
            return self._run_process(proc, timeout, on_line=on_line, cancel=cancel,
                                     cleanup_budget=cleanup_budget)
        finally:
            if owned.wait(0):
                proc_guard.workers.unregister(worker_id)
                with _LIVE_LOCK:
                    _LIVE.discard(proc)

    def _run_process(self, proc: Any, timeout: float, *, on_line: Optional[Callable[[str], None]],
                     cancel: Optional[Callable[[], bool]], cleanup_budget: Optional[float]) -> _Outcome:
        started_readers: List[Any] = []
        reason: Optional[str] = None
        deadline = self._clock() + timeout
        cleanup_deadline: Optional[float] = None

        def begin_cleanup() -> Optional[float]:
            if cleanup_budget is None:
                return None
            # The quarter-second reserve covers the poll/scheduling boundary;
            # all termination, wait, joins and pipe closes share this deadline.
            return min(deadline, self._clock()) + cleanup_budget - _POLL_SECONDS

        try:
            out_reader = _StdoutReader(proc.stdout)
            out_reader.start()
            started_readers.append(out_reader)
            err_reader = _StderrReader(proc.stderr, on_line)
            err_reader.start()
            started_readers.append(err_reader)
            while True:
                try:
                    poll_timeout = _POLL_SECONDS if cleanup_budget is None else max(0.0, min(_POLL_SECONDS, deadline - self._clock()))
                    proc.wait(timeout=poll_timeout)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel():
                    reason = "cancel"
                elif self._clock() >= deadline:
                    reason = "timeout"
                elif out_reader.oversize:
                    reason = "oversize"
                if reason:
                    cleanup_deadline = begin_cleanup()
                    self._kill(proc, deadline=cleanup_deadline)
                    break
        except BaseException:
            cleanup_deadline = begin_cleanup()
            try:
                self._kill(proc, deadline=cleanup_deadline)
            except Exception:
                # Preserve the reader/start/callback error even if cleanup fails.
                try:
                    proc.kill()
                    proc.wait(5 if cleanup_deadline is None else max(0.0, cleanup_deadline - self._clock()))
                except Exception:
                    pass
            raise
        finally:
            if cleanup_budget is not None and cleanup_deadline is None:
                cleanup_deadline = begin_cleanup()
            for t in started_readers:
                try:
                    t.join(_JOIN_SECONDS if cleanup_deadline is None else max(0.0, cleanup_deadline - self._clock()))
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                reader_active = any(getattr(t, "stream", None) is stream and t.is_alive()
                                    for t in started_readers)
                if cleanup_deadline is not None:
                    try:
                        self._bounded_cleanup_call(stream.close, cleanup_deadline,
                                                   inline_on_failure=not reader_active)
                    except Exception:
                        pass
                elif not reader_active:
                    try:
                        stream.close()
                    except Exception:
                        pass
                # A still-active daemon reader can hold the buffered pipe lock.
                # Without a cleanup worker, leave that pipe for EOF/GC rather
                # than blocking the caller; its eventual close is best effort.
        return _Outcome(proc.returncode, out_reader.data, out_reader.oversize or reason == "oversize",
                        err_reader.text, reason)

    def _finish(self, command: str, outcome: _Outcome) -> SuiteDocument:
        tail, rc = outcome.stderr_tail, outcome.returncode
        if outcome.reason == "timeout":
            raise SuiteError("timeout", f"videosuite {command} timed out.", exit_code=rc, stderr_tail=tail)
        if outcome.oversize:
            raise _contract("The suite output is larger than 20 MB.", tail, rc)
        if rc == 0 or (command == "compare" and rc == 1):
            try:
                doc = parse_document(command, outcome.stdout)
            except SuiteError as exc:
                if rc == 0:
                    exc.stderr_tail, exc.exit_code = tail, rc
                    raise
                raise SuiteError("failed", "videosuite compare failed without a usable result.", exit_code=rc, stderr_tail=tail) from None
            doc.exit_code, doc.stderr_tail = rc, tail
            return doc
        kind = _EXIT_KIND.get(rc if rc is not None else -1, "failed")
        raise SuiteError(kind, f"videosuite {command} exited with code {rc}.", exit_code=rc, stderr_tail=tail)


# ── singleton ────────────────────────────────────────────────────────────────────

_client: Optional[SuiteClient] = None
_client_lock = threading.Lock()


def get_suite_client() -> SuiteClient:
    """The process-wide client (used with ``Depends``; tests override it). Raises ``SuiteUnavailable``."""
    global _client
    with _client_lock:
        if _client is None:
            _client = SuiteClient()
        return _client


def reset_suite_client() -> None:
    global _client
    with _client_lock:
        _client = None
