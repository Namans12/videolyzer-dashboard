"""Opaque per-run registry. Serve verified bytes from an opened file, never a generic path."""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import re
import stat
import threading
from pathlib import Path
from typing import List, Sequence
from urllib.parse import quote

from fastapi import HTTPException
from starlette.responses import Response

_lock = threading.RLock()
_registry = {}
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
_KINDS = {'.json': 'json', '.txt': 'text', '.html': 'html', '.log': 'log'}
_CSP = ("sandbox; default-src 'none'; script-src 'none'; connect-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


def forget_run(run_id: str) -> None:
    with _lock:
        for key in [key for key in _registry if key[0] == run_id]:
            del _registry[key]


def _refuse():
    raise HTTPException(404, 'Report unavailable or changed.')


def _plain(path: Path) -> Path:
    # Inspect raw ancestors BEFORE resolve: junctions must never disappear in canonicalization.
    if (not path.is_absolute() or '..' in path.parts or ':' in str(path)[2:] or
            str(path).startswith(('\\\\', '//'))):
        _refuse()
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _refuse()
    return path.resolve(strict=True)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _handle_path(stream):
    """Verify the actual Windows opened handle, closing the check/open path-swap race."""
    import msvcrt
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = api.GetFinalPathNameByHandleW
    fn.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    fn.restype = w.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    count = fn(msvcrt.get_osfhandle(stream.fileno()), buf, len(buf), 0)
    if not count or count >= len(buf):
        _refuse()
    text = buf.value
    return Path(text[4:] if text.startswith('\\\\?\\') else text)


def _read(path, root, expected=None):
    try:
        canonical_root, canonical = _plain(root), _plain(path)
        if canonical.parent != canonical_root:
            _refuse()
        with open(path, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                _refuse()
            if os.name == 'nt' and ntpath.normcase(str(_handle_path(stream))) != ntpath.normcase(str(canonical)):
                _refuse()
            if expected is not None and _identity(before) != tuple(expected['identity']):
                _refuse()
            data = stream.read()
            after = os.fstat(stream.fileno())
            if (_identity(before) != _identity(after) or _identity(path.stat()) != _identity(after)
                    or _plain(path) != canonical or _plain(root) != canonical_root):
                _refuse()
        digest = hashlib.sha256(data).hexdigest()
        if expected is not None and digest != expected['sha256']:
            _refuse()
        return data, digest, _identity(after)
    except (OSError, ValueError, RuntimeError):
        _refuse()


def register_reports(run_id: str, reports_dir: Path, known_outputs: Sequence[Path]) -> List[dict]:
    if not _ID.fullmatch(run_id):
        _refuse()
    root = Path(reports_dir)
    refs = []
    for raw in known_outputs:
        path = Path(raw)
        if path.suffix.lower() not in _KINDS or any(ord(c) < 32 for c in path.name):
            _refuse()
        _, digest, identity = _read(path, root)
        rid = hashlib.sha256(f'{run_id}:{path.name}'.encode('utf-8')).hexdigest()[:16]
        entry = dict(path=path, root=root, identity=identity, sha256=digest, name=path.name, kind=_KINDS[path.suffix.lower()])
        with _lock:
            existing = _registry.get((run_id, rid))
            if existing and existing != entry:
                _refuse()
            _registry[(run_id, rid)] = entry
        refs.append(dict(report_id=rid, name=path.name, kind=entry['kind'],
                         url=f'/comparison/runs/{run_id}/reports/{rid}', sha256=digest))
    return refs


def _entry(run_id, report_id):
    if not _ID.fullmatch(run_id) or not _ID.fullmatch(report_id):
        _refuse()
    with _lock:
        entry = _registry.get((run_id, report_id))
        if entry is None:
            _refuse()
        return dict(entry)


def resolve_report(run_id: str, report_id: str) -> Path:
    entry = _entry(run_id, report_id)
    _read(entry['path'], entry['root'], entry)
    return entry['path']


def serve_report(run_id: str, report_id: str, *, download: bool = False) -> Response:
    entry = _entry(run_id, report_id)
    data, _, _ = _read(entry['path'], entry['root'], entry)
    headers = {'Content-Security-Policy': _CSP, 'X-Content-Type-Options': 'nosniff',
               'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
               'Content-Disposition': ('attachment' if download else 'inline') +
               "; filename=report; filename*=UTF-8''" + quote(entry['name'], safe='')}
    return Response(data, media_type='text/html' if entry['kind'] == 'html' else 'text/plain', headers=headers)
