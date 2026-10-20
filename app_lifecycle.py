"""Explicit Quit admission and bounded cancellation, independent of browsers."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict

from jobs import TERMINAL, JobStore
from proc_guard import WorkerRegistry

logger = logging.getLogger(__name__)


class AppLifecycle:
    def __init__(self, jobs: JobStore, workers: WorkerRegistry, *, grace_s=10., force_s=5.):
        self.jobs, self.workers = jobs, workers
        self.grace_s, self.force_s = grace_s, force_s
        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self.mode = "keep"
        self.error = None

    def register_worker(self, worker_id: str, cancel: Callable[[], None],
                        wait: Callable[[float], bool]) -> None:
        with self.workers.lock:
            if self.workers.stopping:
                raise RuntimeError("App is shutting down; worker creation is refused.")
            self.workers.register(worker_id, cancel, wait)

    def unregister_worker(self, worker_id: str) -> None:
        self.workers.unregister(worker_id)

    def can_start_work(self) -> bool:
        return self.jobs.can_start_work()

    def active_jobs(self):
        active = []
        for job_id in self.jobs.ids():
            job = self.jobs.get(job_id)
            if job and job["status"] not in TERMINAL:
                active.append(dict(job_id=job_id, kind=job["kind"], status=job["status"],
                                   current=job.get("current", "")))
        return active

    def request_quit(self, mode: str) -> Dict[str, Any]:
        if mode not in ("keep", "when_finished", "stop"):
            raise ValueError("invalid quit mode")
        with self._lock:
            accepted = self.mode != "stop" or mode == "stop"
            if accepted:
                first_stop = self.mode != "stop" and mode == "stop"
                self.mode = mode
                self.jobs.accept_work(mode == "keep")
                if mode == "stop":
                    with self.workers.lock:
                        self.workers.stopping = True
                    for job in self.active_jobs():
                        self.jobs.request_cancel(job["job_id"])
                    if first_stop:
                        self.workers.signal()
            return dict(accepted=accepted, quit_pending=self.mode != "keep", active_jobs=self.active_jobs())

    def ready_to_quit(self):
        if self.mode == "when_finished":
            self.workers.wait_all(0)
        return self.mode != "keep" and not self.error and not self.active_jobs() and not self.workers.ids()

    def drain(self):
        with self._drain_lock:
            self.request_quit("stop")
            if not self.workers.wait_all(self.grace_s):
                deadline = time.monotonic() + self.force_s
                force_threads = self.workers.signal(force=True)
                for thread in force_threads:
                    thread.join(max(0, deadline - time.monotonic()))
                self.workers.wait_all(max(0, deadline - time.monotonic()))
                if any(thread.is_alive() for thread in force_threads) or self.workers.ids():
                    self.error = "Owned workers did not stop; app remains running."
                    logger.error(self.error)
                    return False
            for job in self.active_jobs():
                self.jobs.finish(job["job_id"])
            self.error = None
            return True
