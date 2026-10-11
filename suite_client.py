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
SCORE_BASE_S, SCORE_PER_FILE_S, DEEP_DV_PER_FILE_S = 60, 45, 900
COMPARE_TIMEOUT_S = 6 * 3600

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


def taskkill_tree(pid: int) -> None:
    """Kill a process and all its children. Best effort: failures are swallowed (the caller then tries ``proc.kill``)."""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False, shell=False,
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

_REQUIRED: Dict[str, Dict[str, type]] = {
    "devices": {"devices": list},
    "facts": {"records": list},
    "score": {"records": list},
    "rank": {"quality": list, "same_picture_groups": list, "not_compared": list, "missing_pairs": list,
             "weights": dict, "devices": dict},
    "compare": {"mode": str, "estimate": dict, "shortlist": list, "dropped": list, "pairs": list, "failed": int},
}


def _contract(message: str, stderr_tail: str = "", exit_code: Optional[int] = None) -> SuiteError:
    return SuiteError("contract", message, exit_code=exit_code, stderr_tail=stderr_tail)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


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
    if command == "devices":
        for d in doc["devices"]:
            if not (isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("players"), list)
                    and all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in d["players"])):
                raise _contract("The devices document has an invalid device entry.")
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
                 env_path: Optional[Path] = None, root: Optional[str] = None) -> None:
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
        self._killer = killer or taskkill_tree
        self._clock = clock or time.monotonic

    # ── timeouts ─────────────────────────────────────────────────────────────────

    def _timeout_devices(self) -> float:
        return DEVICES_TIMEOUT_S

    def _timeout_score_rank(self, n_files: int, deep_dv: bool) -> float:
        return SCORE_BASE_S + n_files * (SCORE_PER_FILE_S + (DEEP_DV_PER_FILE_S if deep_dv else 0))

    def _timeout_compare(self) -> float:
        raw = self._environ.get(ENV_COMPARE_TIMEOUT)
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            value = None
        if value is None or not math.isfinite(value) or value <= 0:
            return COMPARE_TIMEOUT_S
        return value

    # ── commands ─────────────────────────────────────────────────────────────────

    def devices(self) -> SuiteDocument:
        outcome = self._run("devices", ["devices", "--json"], self._timeout_devices())
        return self._finish("devices", outcome)

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
        return self._with_list("compare", paths, extra, self._timeout_compare(), on_line=on_line, cancel=cancel)

    # ── internals ────────────────────────────────────────────────────────────────

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
                   cancel: Optional[Callable[[], bool]] = None) -> Optional[SuiteDocument]:
        tmp = tempfile.mkdtemp(prefix="vsuite_")
        try:
            list_file = os.path.join(tmp, "paths.txt")
            with open(list_file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(paths) + "\n")
            argv = [command, "--list", list_file, "--json"] + extra
            outcome = self._run(command, argv, timeout, on_line=on_line, cancel=cancel)
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

    def _kill(self, proc: Any) -> None:
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
             cancel: Optional[Callable[[], bool]] = None) -> _Outcome:
        argv = self.argv_prefix + args
        try:
            proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=self._process_env(), cwd=self.root, shell=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            raise SuiteError("unavailable", "The VideoSuite process could not be started.") from None
        out_reader, err_reader = _StdoutReader(proc.stdout), _StderrReader(proc.stderr, on_line)
        out_reader.start()
        err_reader.start()
        reason: Optional[str] = None
        deadline = self._clock() + timeout
        try:
            while True:
                try:
                    proc.wait(timeout=_POLL_SECONDS)
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
                    self._kill(proc)
                    break
        except BaseException:
            self._kill(proc)                         # the tree never outlives an error in a callback
            raise
        finally:
            for t in (out_reader, err_reader):
                t.join(_JOIN_SECONDS)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
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
