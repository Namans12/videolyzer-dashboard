"""In-memory job store for the Dashboard: locked, with honest terminal states.

One ``JobStore`` (the module-level ``jobs``) holds every job as a plain dict with
the shape the UI has always polled (``status, progress, current, total, results,
error, events`` plus kind-specific keys) and two additive keys: ``kind`` and
``error_kind``.

Rules:

* Every access goes through one lock. ``get`` returns a deep copy, so a caller can
  never see a half-written job and can never mutate the store by accident.
* ``TERMINAL`` is the one definition of "finished": ``done``, ``error``,
  ``cancelled``. The store, the SSE generator and retention all use it.
* A terminal job never changes again (``finish``/``fail`` on a finished job are
  ignored).
* Cancellation is a flag next to the job (``request_cancel``/``is_cancelled``),
  never a status. A runner stops queuing work when it sees the flag; when a
  runner ends while the flag is set, ``finish`` and ``fail`` turn the outcome into
  ``cancelled`` (``error`` is the string ``"cancelled"``, partial ``results`` and
  other fields are kept). A flag set on a job that already ended is ignored.
* Retention has no timers: ``create`` purges finished jobs beyond the newest
  ``keep_finished`` (20), but never one that finished less than ``grace_s`` (30 s)
  ago, so a UI that polls a just-finished job always gets its final state.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Optional

TERMINAL = ("done", "error", "cancelled")

KEEP_FINISHED = 20
GRACE_SECONDS = 30.0


class JobStore:
    def __init__(self, keep_finished: int = KEEP_FINISHED, grace_s: float = GRACE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.keep_finished = keep_finished
        self.grace_s = grace_s
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._cancel: set[str] = set()
        self._finished: list[tuple[str, float]] = []   # (job_id, finished_at), oldest first
        self._accepting = True

    def accept_work(self, accepting: bool) -> None:
        with self._lock:
            self._accepting = accepting

    def can_start_work(self) -> bool:
        with self._lock:
            return self._accepting

    # ── creation / reading ───────────────────────────────────────────────────

    def create(self, kind: str, **fields: Any) -> str:
        """Create a running job and return its id. Purges old finished jobs first."""
        job: dict[str, Any] = {
            "status": "running",
            "progress": "0/1",
            "current": "",
            "total": 0,
            "results": [],
            "error": None,
            "error_kind": None,
            "events": [],
            "kind": kind,
        }
        job.update(fields)
        job["kind"] = kind
        job_id = uuid.uuid4().hex
        with self._lock:
            if not self._accepting:
                raise RuntimeError("App is shutting down; new jobs are refused.")
            self._purge()
            self._jobs[job_id] = job
        return job_id

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._jobs)

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def __contains__(self, job_id: object) -> bool:
        with self._lock:
            return job_id in self._jobs

    def __iter__(self) -> Iterator[str]:
        return iter(self.ids())

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._cancel.clear()
            self._finished.clear()

    # ── writing ──────────────────────────────────────────────────────────────

    def update(self, job_id: str, **fields: Any) -> bool:
        """Set fields on a job. The store takes ownership of the values given.
        Returns False for an unknown id."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.update(fields)
            return True

    def mutate(self, job_id: str, fn: Callable[[dict], Any]) -> bool:
        """Run ``fn(job)`` on the live job dict while holding the lock (for nested
        updates such as per-magnet records)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            fn(job)
            return True

    def append_event(self, job_id: str, msg: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job["events"].append({"msg": msg, "ts": time.time()})
            return True

    def events_since(self, job_id: str, n: int) -> Optional[list[dict]]:
        """Copies of the events from index ``n`` on; None for an unknown job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return copy.deepcopy(job["events"][max(n, 0):])

    def events_and_status(self, job_id: str, n: int) -> Optional[tuple[list[dict], str]]:
        """Events from index ``n`` and the status, read in one lock hold. Because
        runners emit their last event BEFORE ``finish``/``fail``, a terminal status
        here means the returned events are complete."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return copy.deepcopy(job["events"][max(n, 0):]), job["status"]

    # ── ending a job ─────────────────────────────────────────────────────────

    def finish(self, job_id: str, results: Optional[list] = None) -> Optional[str]:
        """End the job as ``done`` (or ``cancelled`` when the cancel flag is set).
        Returns the final status, None for an unknown id."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in TERMINAL:
                return job["status"]
            if results is not None:
                job["results"] = results
            if job_id in self._cancel:
                return self._end(job_id, job, "cancelled", "cancelled", None)
            return self._end(job_id, job, "done", None, None)

    def fail(self, job_id: str, error: Any, error_kind: Optional[str] = None) -> Optional[str]:
        """End the job as ``error`` (or ``cancelled`` when the cancel flag is set)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in TERMINAL:
                return job["status"]
            if job_id in self._cancel:
                return self._end(job_id, job, "cancelled", "cancelled", None)
            return self._end(job_id, job, "error", str(error), error_kind)

    def _end(self, job_id: str, job: dict, status: str, error: Optional[str],
             error_kind: Optional[str]) -> str:
        job["status"] = status
        job["error"] = error
        job["error_kind"] = error_kind
        self._cancel.discard(job_id)
        self._finished.append((job_id, self._clock()))
        return status

    # ── cancellation ─────────────────────────────────────────────────────────

    def request_cancel(self, job_id: str) -> bool:
        """True for an existing job (even a finished one: the flag is then ignored),
        False for an unknown id."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["status"] not in TERMINAL:
                self._cancel.add(job_id)
            return True

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel

    # ── retention ────────────────────────────────────────────────────────────

    def _purge(self) -> None:
        """Called with the lock held, from ``create`` only."""
        excess = len(self._finished) - self.keep_finished
        if excess <= 0:
            return
        now = self._clock()
        kept: list[tuple[str, float]] = []
        for idx, (job_id, finished_at) in enumerate(self._finished):
            if idx < excess and now - finished_at >= self.grace_s:
                self._jobs.pop(job_id, None)
                self._cancel.discard(job_id)
            else:
                kept.append((job_id, finished_at))
        self._finished = kept


jobs = JobStore()
