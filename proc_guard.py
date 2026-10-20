"""Windows child jobs terminate their entire process tree at Dashboard exit."""
import atexit
import ctypes
import logging
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from ctypes import wintypes

logger = logging.getLogger(__name__)


class WorkerRegistry:
    """Owned identities survive failed cleanup. No PID discovery or adoption."""
    def __init__(self):
        self.lock = threading.RLock()
        self._workers = {}
        self._waiting = {}
        self.stopping = False

    def register(self, worker_id, cancel, wait, *, force=None, job_id=None):
        with self.lock:
            if worker_id in self._workers:
                raise ValueError("duplicate worker identity")
            self._workers[worker_id] = (cancel, wait, force, job_id)

    def unregister(self, worker_id):
        with self.lock:
            self._workers.pop(worker_id, None)
            self._waiting.pop(worker_id, None)

    def ids(self):
        with self.lock:
            return list(self._workers)

    def snapshot(self):
        with self.lock:
            return dict(self._workers)

    def signal(self, *, force=False):
        # Callbacks run concurrently: a broken native/session callback cannot
        # consume another worker's cleanup budget or block the HTTP event loop.
        threads = []
        for entry in self.snapshot().values():
            callback = entry[2] if force else entry[0]
            if callback:
                threads.append(self.invoke(callback, job_id=entry[3]))
        return threads

    def invoke(self, callback, *, job_id=None):
        done = threading.Event()
        callback_id = "cleanup:" + uuid.uuid4().hex
        self.register(callback_id, None, done.wait, job_id=job_id)
        def invoke():
            try:
                callback()
            except Exception:
                logger.error("Owned worker cleanup failed")
            finally:
                done.set()
                self.unregister(callback_id)
        thread = threading.Thread(target=invoke, daemon=True, name="owned-cleanup")
        thread.start()
        return thread

    def wait_all(self, timeout):
        deadline = time.monotonic() + timeout
        threads = []
        for worker_id, entry in self.snapshot().items():
            with self.lock:
                previous = self._waiting.get(worker_id)
                if previous is not None and previous.is_alive():
                    threads.append(previous)
                    continue
            def wait(worker_id=worker_id, entry=entry):
                try:
                    if entry[1](max(0, deadline - time.monotonic())):
                        self.unregister(worker_id)
                except Exception:
                    logger.error("Owned worker wait failed")
            thread = threading.Thread(target=wait, daemon=True, name="owned-wait")
            with self.lock:
                self._waiting[worker_id] = thread
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        return not self.ids() and not any(t.is_alive() for t in threads)


workers = WorkerRegistry()
_context = threading.local()


def owner_id():
    return getattr(_context, "job_id", None)


@contextmanager
def owner(job_id):
    previous = owner_id()
    _context.job_id = job_id
    try:
        yield
    finally:
        _context.job_id = previous


def inherit_owner(fn):
    job_id = owner_id()
    @wraps(fn)
    def run(*args, **kwargs):
        with owner(job_id):
            return fn(*args, **kwargs)
    return run


def job_worker(fn):
    @wraps(fn)
    def run(job_id, *args, **kwargs):
        from jobs import jobs
        done = threading.Event()
        worker_id = "job:" + job_id + ":" + uuid.uuid4().hex
        with workers.lock:
            if workers.stopping:
                jobs.request_cancel(job_id)
                jobs.finish(job_id)
                return
            workers.register(worker_id, lambda: jobs.request_cancel(job_id), done.wait, job_id=job_id)
        try:
            with owner(job_id):
                return fn(job_id, *args, **kwargs)
        finally:
            done.set()
            workers.unregister(worker_id)
    return run


def wait_process(proc, timeout):
    if proc.poll() is not None:
        return True
    try:
        proc.wait(timeout=max(0, timeout))
    except Exception:
        pass
    return proc.poll() is not None


def force_process(proc):
    if proc.poll() is not None:
        return
    # Only Popen objects created and retained by this process reach this helper.
    from suite_client import taskkill_tree
    taskkill_tree(proc.pid, timeout=2)
    if proc.poll() is None:
        proc.kill()


class OwnedProcess:
    """A retained Popen and, when assignable, its own nested Windows Job."""
    def __init__(self, proc, force=None):
        self.proc = proc
        self._force = force or (lambda: force_process(proc))
        self.job = None
        if getattr(proc, "_handle", None) is not None:
            try:
                job = KillOnCloseJob()
                if job.assign(proc):
                    self.job = job
                else:
                    job.close()
            except Exception:
                logger.warning("Child Job unavailable; crash cleanup is best effort")

    def cancel(self):
        if self.proc.poll() is None:
            terminate = getattr(self.proc, "terminate", None)
            if terminate:
                terminate()

    def force(self):
        if self.job and self.job.terminate():
            return
        if self.proc.poll() is None:
            self._force()

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        if not wait_process(self.proc, timeout):
            return False
        if self.job:
            # A tool exiting is not proof that its descendants exited. Keep the
            # Job handle to query until every assigned process is known gone.
            self.job.terminate()
            while self.job.active_count() != 0:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
            self.job.close()
        return True


def run_tool(args, *, timeout, text=True):
    """Hidden tool children are registered before Quit can take its snapshot."""
    with workers.lock:
        if workers.stopping:
            raise OSError("App is shutting down")
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, text=text,
            **({"encoding": "utf-8", "errors": "replace"} if text else {}),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        worker_id = "tool:" + str(proc.pid) + ":" + uuid.uuid4().hex
        owned = OwnedProcess(proc)
        workers.register(worker_id, owned.cancel, owned.wait, force=owned.force, job_id=owner_id())
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)
    except BaseException:
        owned.force()
        owned.wait(5)
        raise
    finally:
        if owned.wait(0):
            workers.unregister(worker_id)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()


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
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.c_void_p)
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
                logger.warning("Job Object assignment failed; crash cleanup is best effort")
                return False
            with self._lock:
                if self._handle and self._kernel32.AssignProcessToJobObject(self._handle, int(handle)):
                    return True
            logger.warning("Job Object assignment failed (%s); crash cleanup is best effort", ctypes.get_last_error())
        except Exception:
            logger.warning("Job Object assignment failed; crash cleanup is best effort")
        return False

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None

    def terminate(self):
        with self._lock:
            if self._handle:
                return bool(self._kernel32.TerminateJobObject(self._handle, 1))
            return True

    def active_count(self):
        with self._lock:
            if not self._handle:
                return 0
            accounting = ctypes.create_string_buffer(48)
            if not self._kernel32.QueryInformationJobObject(self._handle, 1, accounting, 48, None):
                return None
            return ctypes.c_uint32.from_buffer(accounting, 40).value


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


def process_created(pid):
    """Decimal Windows FILETIME plus liveness; a reused PID is a new identity."""
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        code = wintypes.DWORD()
        if (not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user))
                or not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259):
            return None
        return str(created.value)
    finally:
        kernel32.CloseHandle(handle)
