"""Path guard: validates server-local paths received over HTTP.

``validate_user_path`` applies these checks in this exact order:

1. strip surrounding whitespace and one pair of double quotes, and a plain ``\\\\?\\X:`` long-path prefix
   (``\\\\?\\UNC\\`` is not plain and stays rejected)
2. raw-string checks (empty, NUL, UNC / ``//`` / ``\\\\?\\`` / ``\\\\.\\`` prefixes,
   alternate data streams, reserved device names)                    -> 400
3. ``PureWindowsPath(raw)`` must have a ``X:`` drive and be absolute  -> 400;
   the drive letter of a plain ``X:\\...`` input is remembered
4. ``Path.resolve`` (strict unless ``must_exist=False``)
   FileNotFoundError / NotADirectoryError -> 404, anything else      -> 400
5. a resolved UNC path is accepted only when the ORIGINAL drive letter is a network drive (a mapped drive
   resolves to ``\\\\server\\share``), ``VIDEOLYZER_ALLOW_REMOTE_DRIVES=1`` and it is not a device path;
   otherwise UNC and device paths are rejected. A resolved drive letter that is a network drive is
   rejected unless that variable is set                              -> 400
6. containment in ``allowed_roots`` / ``VIDEOLYZER_ALLOWED_ROOTS`` (``;``-separated,
   default empty = any local drive). Roots are resolved the same way (a root given as a mapped drive
   letter becomes its UNC target; a drive-only root ``X:`` is read as ``X:\\``)    -> 403
7. ``must_be`` ("any" | "file" | "dir")                               -> 400
8. a file must have a supported video extension                       -> 400

Messages are fixed strings: neither exception text nor any path other than the one
the user sent ever appears in them.
"""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Iterator, Optional

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".ts", ".m2ts", ".hevc", ".h265", ".m4v")
ENV_ALLOWED_ROOTS = "VIDEOLYZER_ALLOWED_ROOTS"
ENV_ALLOW_REMOTE = "VIDEOLYZER_ALLOW_REMOTE_DRIVES"

_DRIVE_RE = re.compile(r"[A-Za-z]:")
_RESERVED_RE = re.compile(
    r"(con|prn|aux|nul|com[0-9]|lpt[0-9])", re.IGNORECASE
)
_DRIVE_REMOTE = 4


class PathRejected(Exception):
    """A path failed validation. ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def configured_roots() -> list[str]:
    """Allowed roots from the environment (empty list = no restriction)."""
    raw = os.environ.get(ENV_ALLOWED_ROOTS, "")
    return [part.strip().strip('"') for part in raw.split(";") if part.strip().strip('"')]


def _is_remote_drive(drive: str) -> bool:
    """True when ``drive`` (``"C:"``) is a network drive. Unknown -> False."""
    try:
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
    except AttributeError:  # not Windows
        return False
    return get_drive_type(drive + "\\") == _DRIVE_REMOTE


_LONG_PREFIX_RE = re.compile(r"\\\\\?\\(?=[A-Za-z]:\\)")      # \\?\ directly followed by X:\  (never \\?\UNC\)
_PLAIN_DRIVE_RE = re.compile(r"([A-Za-z]:)[\\/]")


def _normalise(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return _LONG_PREFIX_RE.sub("", raw, count=1)


def _original_drive(raw: str) -> Optional[str]:
    """The drive letter of a plain ``X:\\...`` input (upper case, ``"X:"``), else ``None``."""
    match = _PLAIN_DRIVE_RE.match(raw)
    return match.group(1).upper() if match else None


def _reject_raw(raw: str) -> None:
    if not raw:
        raise PathRejected(400, "path is empty")
    if "\x00" in raw:
        raise PathRejected(400, "invalid path")
    # \\host\share, //host/share, \\?\C:\x, \\.\NUL and mixed \/ forms.
    if len(raw) >= 2 and raw[0] in "\\/" and raw[1] in "\\/":
        raise PathRejected(400, "network and device paths are not allowed")
    # Alternate data streams (only the drive colon is legal).
    if ":" in raw[2:]:
        raise PathRejected(400, "invalid path")


def _reject_reserved_leaf(name: str) -> None:
    # CON, NUL, COM1 ... also when followed by an extension ("NUL.mkv").
    stem = name.split(".", 1)[0].rstrip(" ")
    if _RESERVED_RE.fullmatch(stem):
        raise PathRejected(400, "invalid path")


def _resolve(path: Path, strict: bool) -> Path:
    try:
        return path.resolve(strict=strict)
    except (FileNotFoundError, NotADirectoryError):
        raise PathRejected(404, "path not found") from None
    except (OSError, ValueError, RuntimeError):
        raise PathRejected(400, "path not accessible") from None


def _is_device_path(text: str) -> bool:
    r"""``\\.\...`` and ``\\?\...`` other than ``\\?\UNC\...``."""
    head = text[:4]
    if head == "\\\\.\\":
        return True
    return head == "\\\\?\\" and text[4:8].upper() != "UNC\\"


def _check_resolved(resolved: Path, original_drive: Optional[str] = None) -> None:
    """Reject UNC, device and network-drive results. ``original_drive`` is the drive letter the user typed.

    A mapped network drive resolves to a UNC path, so a UNC result is accepted only when the user typed a
    network drive letter AND ``VIDEOLYZER_ALLOW_REMOTE_DRIVES=1`` and the result is not a device path."""
    allow_remote = os.environ.get(ENV_ALLOW_REMOTE) == "1"
    text = str(resolved)
    if text.startswith("\\\\"):
        if _is_device_path(text) or original_drive is None or not _is_remote_drive(original_drive):
            raise PathRejected(400, "network and device paths are not allowed")
        if not allow_remote:
            raise PathRejected(400, "network drives are not allowed")
        return
    drive = resolved.drive
    if drive and not allow_remote and _is_remote_drive(drive):
        raise PathRejected(400, "network drives are not allowed")


def _normalise_root(root: str) -> str:
    """A drive-only root ``X:`` means the drive's root ``X:\\`` (``Path("X:")`` is the drive's current folder)."""
    return root + "\\" if _DRIVE_RE.fullmatch(root) else root


def _within_roots(resolved: Path, roots: Iterable[str]) -> bool:
    for root in roots:
        try:
            root_resolved = _resolve(Path(_normalise_root(root)), False)    # resolved like the path itself
        except (PathRejected, OSError, ValueError, RuntimeError):
            continue  # an unusable root can never match: fail closed
        try:
            if resolved.is_relative_to(root_resolved):
                return True
        except ValueError:
            continue
    return False


def validate_user_path(
    raw: str,
    *,
    allowed_roots: Optional[Iterable[str]] = None,
    must_be: str = "any",
    must_exist: bool = True,
) -> Path:
    """Validate ``raw`` and return the resolved path, or raise ``PathRejected``."""
    if must_be not in ("any", "file", "dir"):
        raise ValueError("must_be must be 'any', 'file' or 'dir'")
    if not isinstance(raw, str):
        raise PathRejected(400, "invalid path")

    raw = _normalise(raw)                                        # (1)
    _reject_raw(raw)                                             # (2)

    pure = PureWindowsPath(raw)                                  # (3)
    if not _DRIVE_RE.fullmatch(pure.drive) or not pure.is_absolute():
        raise PathRejected(400, "path must be absolute and start with a drive letter")
    original_drive = _original_drive(raw)

    path = Path(raw)
    if must_exist:                                               # (4)
        resolved = _resolve(path, True)
    else:
        # Windows ignores trailing dots/spaces: normalise so "jx." is "jx".
        name = pure.name.rstrip(". ")
        if name == "":
            raise PathRejected(400, "invalid path")
        _reject_reserved_leaf(name)
        parent = _resolve(path.parent, True)
        _check_resolved(parent, original_drive)
        try:
            if not parent.is_dir():
                raise PathRejected(400, "parent is not a folder")
        except OSError:
            raise PathRejected(400, "path not accessible") from None
        resolved = parent / name
        # A pre-planted junction/symlink leaf must be followed so that the checks
        # below see its real target, not the unresolved spelling.
        try:
            planted = os.path.lexists(resolved)
        except (OSError, ValueError):
            raise PathRejected(400, "path not accessible") from None
        if planted:
            resolved = _resolve(resolved, True)
    _check_resolved(resolved, original_drive)                  # (5)

    roots = configured_roots() if allowed_roots is None else list(allowed_roots)
    if roots and not _within_roots(resolved, roots):             # (6)
        raise PathRejected(403, "path is outside the allowed folders")

    try:                                                         # (7)
        exists = resolved.exists()
        is_file = exists and resolved.is_file()
        is_dir = exists and resolved.is_dir()
    except OSError:
        raise PathRejected(400, "path not accessible") from None
    if exists and not (is_file or is_dir):
        raise PathRejected(400, "path not accessible")
    if must_be == "file" and exists and not is_file:
        raise PathRejected(400, "path is not a file")
    if must_be == "dir" and exists and not is_dir:
        raise PathRejected(400, "path is not a folder")

    if is_file or (not exists and must_be == "file"):            # (8)
        if not resolved.name.lower().endswith(VIDEO_EXTENSIONS):
            raise PathRejected(400, "not a supported video file")
    return resolved


def iter_safe_video_files(
    root: str, allowed_roots: Optional[Iterable[str]] = None
) -> Iterator[str]:
    """Yield video files below ``root``, re-validating every directory and file.

    ``os.walk`` follows junctions, so a junction inside a validated folder could
    lead outside the allowed roots (or to a network share). Directories and files
    that no longer pass ``validate_user_path`` are skipped. A folder whose real location was already
    visited (a junction/symlink loop, or a second link to the same folder) is not entered again.
    """
    roots = configured_roots() if allowed_roots is None else list(allowed_roots)

    def real(p: str) -> str:
        try:
            return os.path.normcase(os.path.realpath(p))
        except (OSError, ValueError):
            return os.path.normcase(os.path.abspath(p))

    visited = {real(root)}

    def enter(p: str) -> bool:
        key = real(p)
        if key in visited:
            return False
        visited.add(key)
        return True

    def ok(p: str, must_be: str) -> bool:
        try:
            validate_user_path(p, allowed_roots=roots, must_be=must_be)
            return True
        except PathRejected:
            return False

    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if ok(os.path.join(current, d), "dir") and enter(os.path.join(current, d))]
        for name in files:
            if name.lower().endswith(VIDEO_EXTENSIONS):
                full = os.path.join(current, name)
                if ok(full, "file"):
                    yield full


def make_file_filter(
    allowed_roots: Optional[Iterable[str]] = None,
) -> Callable[[str], bool]:
    """Predicate for ``analysis.scan_folder(file_filter=...)``."""
    roots = configured_roots() if allowed_roots is None else list(allowed_roots)

    def accept(path: str) -> bool:
        try:
            validate_user_path(path, allowed_roots=roots, must_be="file")
            return True
        except PathRejected:
            return False

    return accept
