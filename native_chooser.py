"""Windows common file dialog, serialized on one dedicated native UI thread.

No UI is created on import. Paste input and dialog results use the same guards.
"""
from __future__ import annotations

import ctypes
import asyncio
import ntpath
import os
import queue
import threading
from concurrent.futures import Future
from typing import List, Optional

from fastapi import HTTPException
from path_guard import PathRejected, validate_user_path

_flight = threading.Lock()
_queue = queue.Queue()
_start_lock = threading.Lock()
_ui = None


class ChooserBusy(RuntimeError):
    pass


class ChooserClosing(RuntimeError):
    pass


def guarded_paths(paths: List[str]) -> List[str]:
    if not isinstance(paths, list) or not 2 <= len(paths) <= 50:
        raise HTTPException(400, 'Select 2–50 versions of one movie/title.')
    guarded = []
    seen, names = set(), set()
    for raw in paths:
        if not isinstance(raw, str) or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in raw):
            raise HTTPException(400, 'Invalid path.')
        try:
            path = str(validate_user_path(raw, must_be='file'))
        except PathRejected as exc:
            raise HTTPException(exc.status, exc.message) from None
        key, name = ntpath.normcase(path), ntpath.normcase(ntpath.basename(path))
        if key in seen or name in names:
            raise HTTPException(400, 'Duplicate paths or file names are not allowed; remove duplicates.')
        seen.add(key); names.add(name)
        guarded.append(path)
    return guarded


def _parse_selection(buffer: str) -> List[str]:
    parts = buffer.split('\0')
    parts = parts[:parts.index('')] if '' in parts else parts
    if not parts:
        return []
    if len(parts) == 1:
        return parts
    return [ntpath.join(parts[0], leaf) for leaf in parts[1:]]


def _dialog(owner_hwnd: Optional[int]) -> List[str]:
    if os.name != 'nt':
        raise OSError('Windows file selection unavailable')
    from ctypes import wintypes as w
    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [('lStructSize', w.DWORD), ('hwndOwner', w.HWND), ('hInstance', w.HINSTANCE),
                    ('lpstrFilter', w.LPCWSTR), ('lpstrCustomFilter', w.LPWSTR), ('nMaxCustFilter', w.DWORD),
                    ('nFilterIndex', w.DWORD), ('lpstrFile', w.LPWSTR), ('nMaxFile', w.DWORD),
                    ('lpstrFileTitle', w.LPWSTR), ('nMaxFileTitle', w.DWORD), ('lpstrInitialDir', w.LPCWSTR),
                    ('lpstrTitle', w.LPCWSTR), ('Flags', w.DWORD), ('nFileOffset', w.WORD),
                    ('nFileExtension', w.WORD), ('lpstrDefExt', w.LPCWSTR), ('lCustData', ctypes.c_ssize_t),
                    ('lpfnHook', ctypes.c_void_p), ('lpTemplateName', w.LPCWSTR), ('pvReserved', ctypes.c_void_p),
                    ('dwReserved', w.DWORD), ('FlagsEx', w.DWORD)]
    buffer = ctypes.create_unicode_buffer(131072)
    spec = OPENFILENAMEW()
    spec.lStructSize = ctypes.sizeof(spec)
    spec.hwndOwner = owner_hwnd
    spec.lpstrFilter = 'Video files\0*.mkv;*.mp4;*.ts;*.m2ts;*.hevc;*.h265;*.m4v\0\0'
    spec.nFilterIndex = 1
    spec.lpstrFile = ctypes.cast(buffer, w.LPWSTR)
    spec.nMaxFile = len(buffer)
    spec.lpstrTitle = 'Select versions of one movie/title'
    spec.Flags = 0x80000 | 0x200 | 0x1000 | 0x800 | 0x8 | 0x4 | 0x2000000 | 0x800000
    api = ctypes.WinDLL('comdlg32', use_last_error=True)
    api.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    api.GetOpenFileNameW.restype = w.BOOL
    api.CommDlgExtendedError.restype = w.DWORD
    if not api.GetOpenFileNameW(ctypes.byref(spec)):
        if api.CommDlgExtendedError():
            raise OSError('File dialog failed; paste individual paths instead.')
        return []
    return _parse_selection(buffer[:])


def _worker():
    while True:
        future, owner_hwnd = _queue.get()
        try:
            paths = _dialog(owner_hwnd)
            result = guarded_paths(paths) if paths else []
        except BaseException as exc:
            if not future.cancelled():
                future.set_exception(exc)
        else:
            if not future.cancelled():
                future.set_result(result)
        finally:
            _flight.release()


def _submit(owner_hwnd):
    global _ui
    if not _flight.acquire(blocking=False):
        raise ChooserBusy('A file dialog is already open.')
    try:
        with _start_lock:
            if _ui is None:
                # No executor atexit join, and no asyncio default-executor worker
                # waiting on a modal native call when Quit shuts down the loop.
                _ui = threading.Thread(target=_worker, name='native-file-dialog', daemon=True)
                _ui.start()
        future = Future()
        future.set_running_or_notify_cancel()
        _queue.put((future, owner_hwnd))
        return future
    except BaseException:
        _flight.release()
        raise


async def choose_files_async(*, owner_hwnd: Optional[int] = None, cancel_check=lambda: False) -> List[str]:
    result = asyncio.wrap_future(_submit(owner_hwnd))
    try:
        while not result.done():
            if cancel_check():
                # Let the HTTP request finish so uvicorn's graceful shutdown can
                # proceed. The native call stays daemon-owned until process exit.
                raise ChooserClosing('The app is shutting down.')
            await asyncio.wait([result], timeout=.05)
        return result.result()
    finally:
        if not result.done():
            result.cancel()
