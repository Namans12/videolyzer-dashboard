"""Client for the installed VideoSuite CLI (``python -m videosuite``).

The Dashboard never imports the Comparizer engine and never calls a ``.bat``: cmd.exe
re-parses the arguments of a batch file, so ``&``, ``%``, ``^`` and ``"`` in a path would
reach the shell, and terminating a batch file kills only cmd.exe. Instead the client
starts the venv's ``python.exe -m videosuite ...`` with an argument LIST (never a shell
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
           "PYTHONPATH": root}
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
    return [py, "-m", "videosuite"], _build_env(environ, root, kind, prefix)


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
}


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
    return SuiteDocument(doc)


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

    def _timeout_compare(self, n_files: int = 0, mode: str = "fast", top: int = 4) -> float:
        raw = self._environ.get(ENV_COMPARE_TIMEOUT)
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            value = None
        if value is None or not math.isfinite(value) or value <= 0:
            n = min(n_files, 2 * top)
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

    def facts(self, paths: Sequence[str], deep_dv: bool = False) -> SuiteDocument:
        paths = _check_paths(paths)
        extra = ["--deep-dv"] if deep_dv else []
        return self._with_list("facts", paths, extra, self._timeout_score_rank(len(paths), deep_dv))

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
        if isinstance(real_size, bool) or not isinstance(real_size, int) or not 1 <= real_size <= 2**50:
            raise SuiteArgumentError("real_size must be a whole number of bytes")
        if (not isinstance(real_name, str) or _text_problem(real_name) is not None
                or "/" in real_name or "\\" in real_name or len(real_name) > 255):
            raise SuiteArgumentError("real_name must be a plain file name")
        if coverage not in ("head", "head_tail"):
            raise SuiteArgumentError("coverage must be head or head_tail")
        extra = self._device_args(device, player, fs, lang) + [f"--real-size={real_size}",
                f"--real-name={real_name}", f"--coverage={coverage}"]
        return self._with_list("score", paths, extra, SCORE_BASE_S + SCORE_PER_FILE_S, cancel=cancel,
                               cleanup_budget=5.0)

    def rank(self, paths: Sequence[str], reports_dir: str, device: Optional[str] = "all", player: Optional[str] = "all",
             fs: Optional[str] = None, lang: str = "eng", wq: float = 0.5, wp: float = 0.5,
             deep_dv: bool = False, *, cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        """Rank the files. Returns the document, or ``None`` if cancelled via ``cancel`` (the process tree is killed)."""
        paths = _check_paths(paths)
        reports_dir = _check_dir(reports_dir, "reports_dir")
        wq, wp = _check_weight(wq, "wq"), _check_weight(wp, "wp")
        if wq == 0 and wp == 0:
            raise SuiteArgumentError("wq and wp cannot both be zero")
        extra = self._device_args(device, player, fs, lang) + [f"--reports={reports_dir}", f"--wq={wq!r}", f"--wp={wp!r}"]
        if deep_dv:
            extra.append("--deep-dv")
        return self._with_list("rank", paths, extra, self._timeout_score_rank(len(paths), deep_dv), cancel=cancel)

    def compare(self, paths: Sequence[str], out_dir: str, mode: str = "fast", top: int = 4, *,
                on_line: Optional[Callable[[str], None]] = None,
                cancel: Optional[Callable[[], bool]] = None, device: Optional[str] = "all",
                player: Optional[str] = "all", fs: Optional[str] = None, lang: str = "eng",
                deep_dv: bool = False) -> Optional[SuiteDocument]:
        """Run the pixel comparison. Returns the document (also for exit code 1: ``failed > 0``), or ``None`` if cancelled."""
        paths = _check_paths(paths)
        out_dir = _check_dir(out_dir, "out_dir")
        mode, top = _check_mode(mode), _check_top(top)
        extra = self._device_args(device, player, fs, lang) + [f"--out={out_dir}", f"--mode={mode}", f"--top={top}"]
        if deep_dv:
            extra.append("--deep-dv")
        return self._with_list("compare", paths, extra, self._timeout_compare(len(paths), mode, top),
                               on_line=on_line, cancel=cancel)

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
                   cleanup_budget: Optional[float] = None) -> Optional[SuiteDocument]:
        tmp = tempfile.mkdtemp(prefix="vsuite_")
        try:
            list_file = os.path.join(tmp, "paths.txt")
            with open(list_file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(paths) + "\n")
            words = ["calibrate", command[len("calibrate-"):]] if command.startswith("calibrate-") else [command]
            # An empty list is valid for plan/build only; passing its file would
            # trigger the CLI's shared "give at least one file" validation.
            argv = words + (["--list", list_file] if paths else []) + ["--json"] + extra
            outcome = self._run(command, argv, timeout, on_line=on_line, cancel=cancel,
                                cleanup_budget=cleanup_budget)
            if outcome.reason == "cancel":
                return None
            return self._finish(command, outcome)
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
            proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=self._process_env(), cwd=self.root, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
