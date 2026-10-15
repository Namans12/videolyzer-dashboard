"""Windows child jobs terminate their entire process tree at Dashboard exit."""
import atexit
import ctypes
import logging
import os
import threading
from ctypes import wintypes

logger = logging.getLogger(__name__)


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnCloseJob:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows Job Objects unavailable")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32 = kernel32
        self._lock = threading.Lock()
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel32.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, proc) -> bool:
        try:
            handle = getattr(proc, "_handle", None)
            if handle is None:
                return False
            with self._lock:
                if self._handle and self._kernel32.AssignProcessToJobObject(self._handle, int(handle)):
                    return True
            logger.debug("Could not assign VideoSuite process to Job Object: %s", ctypes.get_last_error())
        except Exception:
            logger.debug("Could not assign VideoSuite process to Job Object", exc_info=True)
        return False

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


class _NullJob:
    def assign(self, proc) -> bool:
        return False

    def close(self) -> None:
        pass


NULL_JOB = _NullJob()
_job = None
_job_lock = threading.Lock()


def default_job():
    global _job
    with _job_lock:
        if _job is None:
            try:
                _job = KillOnCloseJob()
            except Exception:
                logger.warning("VideoSuite Job Object unavailable; using shutdown cleanup", exc_info=True)
                _job = NULL_JOB
            atexit.register(_job.close)
        return _job
