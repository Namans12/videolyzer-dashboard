"""Backend-owned unified comparison runs (Plan 4B U5): immutable plans, confirmation and the stage chain.

* ``RunStore`` keeps each run as an ``InternalRunSnapshot`` (with its absolute, run-owned ``reports_dir``) plus
  private state: the frozen record snapshot, the settings basis and the report registry. HTTP/job/result
  envelopes only ever carry ``to_public_snapshot`` projections.
* ``plan_run`` computes facts ONCE (Quick: ``facts``; Full: read-only ``compare --plan``), freezes them in a
  private run-owned ``videosuite-record-snapshot/1`` file, copies the exact effective settings bytes (Device/Both)
  into a run-owned settings file and ends ``awaiting_confirmation``. It holds only its lightweight slot.
* ``execute_run`` runs the confirmed chain under ONE lease taken at confirmation: Quick = ``quick`` from the
  snapshot (one score/rank slot); Full = accurate all-candidates ``compare`` then ``rank`` (neutral for Overall,
  the chosen device for Device, both answers for Both) under one heavy lease, calling ``SuiteClient`` directly.
  Cancellation is checked at every boundary; the original settings/profile basis is rechecked before every
  continuation and before publication (a device-basis change ends ``settings_changed``, keeping Both's
  independent Overall answer and the verified reports).
  The browser is never needed: polling, SSE and disconnects do not affect the chain.
* ``primary_player`` and the evaluation ``player`` travel independently and unchanged to every suite call;
  Overall passes neither (and never loads the catalogue or settings).
* Nested suite documents are preserved verbatim, including ``settings.path`` for the run-owned settings copy.
  It is provenance, not a report link; the result UI must use the registered relative report URLs and avoid
  displaying this backend path.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import logging
import os
import itertools
import re
import shutil
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import proc_guard
import suite_version
import magnet
import magnet_suite
import comparison_reports
from comparison_contracts import (CapabilityError, ContractError, records_digest, record_id, require_capabilities,
                                  to_public_snapshot, validate_internal_snapshot, validate_run_result)
from jobs import TERMINAL, jobs
from suite_client import SuiteArgumentError, SuiteClient, SuiteError, SuiteUnavailable

logger = logging.getLogger("video-analyzer.comparison")

KEEP_FINISHED = 20
RUN_MARKER = '.videosuite-run.json'
ORPHAN_AGE_S = 30 * 86400
ORPHAN_DELETE_LIMIT = 100
ORPHAN_SCAN_LIMIT = 1000
_UNSET = object()
FINAL_STAGES = ("done", "error", "cancelled")
PENDING_STAGES = ("planning", "selection_required", "awaiting_confirmation")
SNAPSHOT_SCHEMA = "videosuite-record-snapshot/1"
RESULT_SCHEMA = "videosuite-comparison-run/1"

_stat: Callable[..., Any] = os.stat          # test seam for the shared harness's virtual inputs


class RunConflict(Exception):
    """A stale/duplicate confirmation or a changed basis (HTTP 409)."""

    def __init__(self, message: str, kind: str = "conflict") -> None:
        super().__init__(message)
        self.kind = kind


class _Failure(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind, self.message = kind, message


class _Cancelled(Exception):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _default_data_root() -> Path:
    return Path(os.environ.get("VIDEOLYZER_DATA_ROOT") or os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VideoComparator", "app"))


def basis_digest(request: Dict[str, Any], *, settings_bytes: Optional[bytes], versions: Dict[str, Any]) -> str:
    """The run's frozen basis: request (incl. primary and evaluation players), exact settings bytes, versions."""
    body = {"request": request,
            "settings_sha256": None if settings_bytes is None else hashlib.sha256(settings_bytes).hexdigest(),
            "versions": versions}
    return hashlib.sha256(_canonical(body)).hexdigest()


def client_for_snapshot(client: Any, settings_file: Optional[Path]) -> Any:
    """A per-run client whose ``VIDEOSUITE_SETTINGS`` is the run-owned settings copy; the shared client is never
    mutated. ``None`` (Overall) returns the shared client unchanged."""
    if settings_file is None:
        return client
    if isinstance(client, SuiteClient):
        env = dict(client.env_overrides)
        env["VIDEOSUITE_SETTINGS"] = str(settings_file)
        return SuiteClient(argv_prefix=list(client.argv_prefix), env=env, root=client.root, popen=client._popen,
                           killer=None if client._default_killer else client._killer, clock=client._clock,
                           environ=client._environ, job=client._job)
    if hasattr(client, "for_settings"):                    # test fakes
        return client.for_settings(settings_file)
    return client


# ── store ───────────────────────────────────────────────────────────────────────

class RunStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._private: Dict[str, Dict[str, Any]] = {}
        self._results: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._data_root: Optional[Path] = None
        self.build_id: Optional[str] = None
        self.clock: Callable[[], float] = time.time
        self.new_id: Callable[[], str] = lambda: uuid.uuid4().hex

    def configure(self, *, data_root: Optional[Path] = None, clock: Optional[Callable[[], float]] = None,
                  new_id: Optional[Callable[[], str]] = None, build_id: Any = _UNSET) -> None:
        with self._lock:
            if data_root is not None:
                self._data_root = Path(data_root)
            if clock is not None:
                self.clock = clock
            if new_id is not None:
                self.new_id = new_id
            if build_id is not _UNSET:
                self.build_id = build_id if isinstance(build_id, str) and build_id else None

    def reset(self) -> None:
        """Forget every run (tests); keeps the configured data root."""
        with self._lock:
            for run_id in self._runs:
                comparison_reports.forget_run(run_id)
            self._runs.clear()
            self._private.clear()
            self._results.clear()
            self._order.clear()
            self.clock = time.time
            self.new_id = lambda: uuid.uuid4().hex
            self.build_id = None

    def data_root(self) -> Path:
        return self._data_root or _default_data_root()

    def create(self, request: Dict[str, Any]) -> str:
        with self._lock:
            self._purge()
            run_id = str(self.new_id())
        run_id = run_id if run_id.startswith("run-") else "run-" + run_id
        if not re.fullmatch(r'run-[A-Za-z0-9_-]{1,124}', run_id):
            raise ValueError('Invalid generated run identity')
        runs_root = self.data_root().absolute() / 'runs'
        runs_root.mkdir(parents=True, exist_ok=True)
        comparison_reports._plain(runs_root)  # refuse raw reparse ancestors before any writes
        run_dir = runs_root / run_id
        run_dir.mkdir()                       # never adopt a pre-existing directory
        (run_dir / RUN_MARKER).write_bytes(_canonical(dict(
            schema='videosuite-owned-run/1', run_id=run_id, created_at=float(self.clock()))))
        snapshot = {
            "run_id": run_id, "plan_revision": "rev-" + hashlib.sha256(run_id.encode()).hexdigest()[:16],
            "request": copy.deepcopy(request), "stage": "planning", "job_id": None, "candidates": [],
            "conditions": {"device": request.get("device"), "primary_player": request.get("primary_player"),
                           "player": request.get("player"), "evaluated_players": [], "fs": request.get("fs"),
                           "lang": request.get("lang", "eng"), "audio_output": request.get("audio_output"),
                           "settings_digest": None},
            "versions": {"tool_version": None, "engine_tag": None, "facts_version": None, "rules_versions": {}},
            "records_digest": None, "reports_dir": str(run_dir / "reports"), "estimate": None,
            "created_at": float(self.clock()),
        }
        validate_internal_snapshot(snapshot)
        with self._lock:
            self._runs[run_id] = snapshot
            self._private[run_id] = {"run_dir": run_dir, "reports": {}}
            self._order.append(run_id)
        return run_id

    def _purge(self) -> None:
        finished = [r for r in self._order if self._runs[r]["stage"] in FINAL_STAGES]
        for run_id in finished[:max(0, len(finished) - KEEP_FINISHED + 1)]:
            comparison_reports.forget_run(run_id)
            run_dir = self._private[run_id]["run_dir"]
            runs_root = (self.data_root() / "runs").resolve()
            if run_dir.parent == runs_root and run_dir.name == run_id:      # only this store's own folder
                self._remove_owned_run(run_dir)
            for table in (self._runs, self._private, self._results):
                table.pop(run_id, None)
            self._order.remove(run_id)

    def _owned_marker(self, folder: Path) -> Optional[Dict[str, Any]]:
        try:
            root = self.data_root().absolute() / 'runs'
            if folder.parent != root or not re.fullmatch(r'run-[A-Za-z0-9_-]{1,124}', folder.name):
                return None
            comparison_reports._plain(folder)
            marker = folder / RUN_MARKER
            comparison_reports._plain(marker)
            info = marker.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            data = json.loads(marker.read_bytes())
            if (data.get('schema') != 'videosuite-owned-run/1' or data.get('run_id') != folder.name
                    or type(data.get('created_at')) not in (int, float)):
                return None
            return data
        except (OSError, ValueError, TypeError, AttributeError, comparison_reports.HTTPException):
            return None

    def _remove_owned_run(self, folder: Path) -> bool:
        if not self._owned_marker(folder):
            return False
        try:
            # Refuse a tree containing ANY reparse entry (also junctions). Limit
            # inspection so an unexpected tree cannot make startup unbounded.
            count = 0
            for base, dirs, files in os.walk(folder, followlinks=False):
                for name in dirs + files:
                    count += 1
                    if count > 10000:
                        return False
                    info = (Path(base) / name).lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        return False
            shutil.rmtree(folder)
            return True
        except OSError:
            return False

    def cleanup_orphans(self, *, limit: int = ORPHAN_DELETE_LIMIT) -> int:
        """At startup only: old marked direct children; bounded, no reparse traversal.

        Unmarked folders from older app versions are intentionally left for the
        owner. Never infer ownership from a run-* name alone.
        """
        removed = 0
        root = self.data_root().absolute() / 'runs'
        try:
            comparison_reports._plain(root)
            with self._lock, os.scandir(root) as scan:
                for entry in itertools.islice(scan, ORPHAN_SCAN_LIMIT):
                    if removed >= min(max(0, limit), ORPHAN_DELETE_LIMIT):
                        break
                    folder = Path(entry.path)
                    marker = self._owned_marker(folder)
                    if (entry.name not in self._runs and marker is not None
                            and marker['created_at'] <= self.clock() - ORPHAN_AGE_S
                            and self._remove_owned_run(folder)):
                        removed += 1
        except (OSError, comparison_reports.HTTPException):
            pass
        return removed

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            return copy.deepcopy(run) if run is not None else None

    def result(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            result = self._results.get(run_id)
            return copy.deepcopy(result) if result is not None else None

    def private(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._private[run_id]

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(self._runs[r]) for r in reversed(self._order)]

    def list_active(self) -> List[Dict[str, Any]]:
        return [r for r in self.list_all() if r["stage"] not in FINAL_STAGES]

    def update(self, run_id: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            candidate = dict(self._runs[run_id], **copy.deepcopy(fields))
            validate_internal_snapshot(candidate)
            self._runs[run_id] = candidate
            snapshot = copy.deepcopy(candidate)
        _sync_job(snapshot)
        return snapshot

    def set_result(self, run_id: str, result: Dict[str, Any]) -> None:
        checked = validate_run_result(result)
        with self._lock:
            self._results[run_id] = checked

    def confirm(self, run_id: str, revision: str) -> Dict[str, Any]:
        """Atomically confirm the immutable plan once; stale revisions and second confirmations are refused."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            if run["stage"] != "awaiting_confirmation":
                raise RunConflict("This run is not waiting for confirmation.")
            if revision != run["plan_revision"]:
                raise RunConflict("This plan changed; review the new plan.")
            return self.update(run_id, stage="facts")

    def unconfirm(self, run_id: str) -> None:
        """Return a confirmed-but-not-started run to ``awaiting_confirmation`` (e.g. its slot was busy)."""
        with self._lock:
            if self._runs.get(run_id, {}).get("stage") == "facts":
                self.update(run_id, stage="awaiting_confirmation")

    def cancel_pending(self, run_id: str) -> str:
        with self._lock:
            run = self._runs[run_id]
            if run["stage"] in ("selection_required", "awaiting_confirmation"):
                self.update(run_id, stage="cancelled")
                return "cancelled"
            return run["stage"]


store = RunStore()


def public(run_id: str) -> Dict[str, Any]:
    return to_public_snapshot(store.get(run_id))


def report_path(run_id: str, report_id: str) -> Optional[str]:
    """The registered absolute path of a report (U7 serves it by this opaque id only)."""
    with store._lock:
        entry = store._private.get(run_id, {}).get("reports", {}).get(report_id)
        return entry["path"] if entry else None


def _sync_job(snapshot: Dict[str, Any]) -> None:
    job_id = snapshot.get("job_id")
    job = jobs.get(job_id) if job_id else None
    # Confirmation can advance the run before the planning worker marks its job
    # finished. Only the execution job owns facts/pixels/rank snapshots.
    owns_stage = job is not None and not (job['kind'] == 'comparison_plan' and
                                         snapshot['stage'] in ('facts', 'pixels', 'rank', 'done'))
    if owns_stage and job['status'] not in TERMINAL:
        jobs.update(job_id, run_id=snapshot["run_id"], run=to_public_snapshot(snapshot))


# ── helpers ─────────────────────────────────────────────────────────────────────

def _check_cancel(job_id: str) -> None:
    if jobs.is_cancelled(job_id):
        raise _Cancelled()


def _versions(doc: Any, request: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise _Failure("contract", "VideoSuite returned an unusable version document.")
    info = {"status": "ok" if suite_version.parse_version(doc.get("tool_version")) else "error",
            "tool_version": doc.get("tool_version"), "capabilities": doc.get("capabilities")}
    try:
        require_capabilities(info, suite_version.UNIFIED_CAPABILITIES)
    except CapabilityError as exc:
        raise _Failure("unavailable", str(exc)) from None
    rules = doc.get("rules_versions") if isinstance(doc.get("rules_versions"), dict) else {}
    device = request.get("device")
    return {"tool_version": doc.get("tool_version"), "engine_tag": doc.get("engine_tag"),
            "facts_version": doc.get("facts_version") if isinstance(doc.get("facts_version"), int) else None,
            "rules_versions": {} if request["results"] == "overall" else
            {device: rules[device]} if isinstance(rules.get(device), int) else {}}


def _read_settings(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Failure("settings", "The VideoSuite settings file could not be read.") from exc


def _device_basis(client: Any, request: Dict[str, Any]) -> Dict[str, Any]:
    """Live catalogue check of the explicit primary and the evaluated players (catalogue order)."""
    doc = client.devices()
    device = next((d for d in doc.get("devices", []) if isinstance(d, dict) and d.get("id") == request["device"]), None)
    if device is None:
        raise _Failure("usage", "The chosen device is not offered by the installed VideoSuite.")
    players = [p["id"] for p in device.get("players", []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
    if request["primary_player"] not in players:
        raise _Failure("usage", "The chosen player is not offered for this device.")
    evaluated = players if request["include_alternatives"] else [request["primary_player"]]
    block = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    return {"evaluated": evaluated, "settings_path": block.get("path") if isinstance(block.get("path"), str) else None}


def _snapshot_from_facts(paths: List[str], facts: Dict[str, Any]) -> Dict[str, Any]:
    """Quick: candidates from the real records plus each file's current stat identity (never re-probed later)."""
    by_path = {r["source"]["path"]: r for r in facts.get("records", []) if isinstance(r, dict)}
    records, candidates = [], []
    for path in paths:
        record = by_path.get(path)
        try:
            st = _stat(path)
            size, mtime = st.st_size, st.st_mtime_ns
        except OSError:
            size = mtime = None
        reason = None
        if record is None:
            reason = "could not be read"
        elif record["source"].get("probe_coverage") == "failed":
            reason = "could not be probed"
        elif size is None or record["id"] != record_id(path, size, mtime):
            reason = "the file changed while it was read"
        if reason:
            candidates.append(dict(path=path, id=None, status="unreadable", reason=reason, size=size, mtime_ns=mtime))
        else:
            records.append(record)
            candidates.append(dict(path=path, id=record["id"], status="ready", reason=None, size=size, mtime_ns=mtime))
    if not records:
        raise _Failure("no_files", "None of the files could be read.")
    facts_version = records[0]["facts_version"]
    return {"schema": SNAPSHOT_SCHEMA, "facts_version": facts_version,
            "records_digest": records_digest(records, candidates, facts_version), "records": records,
            "candidates": candidates}


def _run_candidates(suite_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(candidate_id="c%d" % i, kind="file", path=c["path"], uri=None, file_index=None,
                 status=c["status"], record_id=c["id"], reason=c["reason"], size=c["size"], mtime_ns=c["mtime_ns"])
            for i, c in enumerate(suite_candidates)]


def _selection(request: Dict[str, Any], results: Optional[str] = None) -> Dict[str, Any]:
    results = results or request["results"]
    if results == "overall":
        return dict(results="overall", device=None, player=None, primary_player=None, fs=None,
                    lang=request["lang"], audio_output=None)
    return dict(results=results, device=request["device"], player=request["player"],
                primary_player=request["primary_player"], fs=request["fs"], lang=request["lang"],
                audio_output=request["audio_output"])


def _current_basis(run_id: str, client: Any) -> str:
    snap, private = store.get(run_id), store.private(run_id)
    request = snap["request"]
    versions = _versions(client.version(), request)
    settings = _read_settings(private.get("settings_path")) if request["results"] != "overall" else None
    return basis_digest(request, settings_bytes=settings, versions=versions)


def _basis_changed(run_id: str, client: Any) -> bool:
    return _current_basis(run_id, client) != store.private(run_id)["basis"]


def _neutral_basis_changed(run_id: str, client: Any) -> bool:
    snap = store.get(run_id)
    versions = _versions(client.version(), snap['request'])
    return any(versions[k] != snap['versions'][k] for k in ('tool_version', 'engine_tag', 'facts_version'))


def verify_current(run_id: str, client: Any) -> None:
    """At confirmation: every candidate's stat identity and the settings/profile basis must be unchanged."""
    snap = store.get(run_id)
    for candidate in snap["candidates"]:
        if candidate["kind"] != "file":
            continue
        try:
            st = _stat(candidate["path"])
            current = (st.st_size, st.st_mtime_ns)
        except OSError:
            current = (None, None)
        if current != (candidate["size"], candidate["mtime_ns"]):
            _end_stale(run_id, "files_changed", "A file changed after planning; plan the run again.")
    try:
        changed = _basis_changed(run_id, client)
    except _Failure as exc:
        _end_stale(run_id, exc.kind, exc.message)
    if changed:
        _end_stale(run_id, "settings_changed",
                   "VideoSuite settings or device rules changed after planning; plan the run again.")


def _end_stale(run_id: str, kind: str, message: str) -> None:
    store.update(run_id, stage="error")
    store.private(run_id)["error"] = (kind, message)
    raise RunConflict(message, kind)


def _register_reports(run_id: str, compare_doc: Dict[str, Any]) -> None:
    private = store.private(run_id)
    reports_dir = Path(store.get(run_id)["reports_dir"])
    for pair in compare_doc.get("pairs", []):
        raw = pair.get("report") if isinstance(pair, dict) else None
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        # These exact companion names are the unchanged suite pair writer's outputs.
        # Never enumerate a report directory or accept a user-supplied file list.
        known = [path] + [path.with_suffix(suffix) for suffix in ('.txt', '.html', '.log')
                          if path.with_suffix(suffix).exists()]
        refs = comparison_reports.register_reports(run_id, reports_dir, known)
        for ref in refs:
            private['reports'][ref['report_id']] = dict(path=str(reports_dir / ref['name']), name=ref['name'], sha256=ref['sha256'], kind=ref['kind'])


def _report_refs(run_id: str) -> List[Dict[str, Any]]:
    reports = store.private(run_id)["reports"]
    return [{"report_id": rid, "name": entry["name"], "kind": entry.get('kind', 'json'),
             "url": f"/comparison/runs/{run_id}/reports/{rid}", "sha256": entry["sha256"]}
            for rid, entry in sorted(reports.items(), key=lambda item: item[1]["name"])]


def _publish(run_id: str, stage: str, *, completed: List[str], overall: Any = None, device: Any = None,
             failures: Optional[List[Dict[str, Any]]] = None, compare_doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snap = store.update(run_id, stage=stage)
    request = snap["request"]
    private = store.private(run_id)
    candidates = snap["candidates"]
    submitted = len(candidates)
    readable = sum(1 for c in candidates if c["status"] == "ready")
    failures = [dict(candidate_id=c["candidate_id"], stage="facts", kind="unreadable", message=c["reason"] or "")
                for c in candidates if c["status"] != "ready"] + list(failures or [])
    if request["depth"] == "quick":
        expected = completed_pairs = 0
    else:
        expected = submitted * (submitted - 1) // 2
        coverage = (compare_doc or {}).get("coverage") or {}
        completed_pairs = coverage.get("completed_pairs", 0) if isinstance(coverage, dict) else 0
    missing = expected - completed_pairs
    neutral_survives = (overall is not None and request['results'] == 'both' and failures
                       and all(f['kind'] == 'settings_changed' for f in failures))
    complete = (missing == 0 and readable == submitted and (stage == "done" or neutral_survives)
                and not any(f["stage"] in ("facts", "pixels") for f in failures))
    result = {
        "schema": RESULT_SCHEMA, "run_id": run_id, "request": request, "snapshot": to_public_snapshot(snap),
        "measurement": "metadata" if request["depth"] == "quick" else "pixels", "completed_stages": completed,
        "coverage": {"submitted": submitted, "readable": readable, "expected_pairs": expected,
                     "completed_pairs": completed_pairs, "missing_pairs": missing, "complete": complete},
        "facts": copy.deepcopy(private["snapshot"]["records"]) if "snapshot" in private else [],
        "overall": overall, "device": device, "failures": failures, "reports": _report_refs(run_id),
        "estimate": snap["estimate"],
        "provenance": {"build_id": store.build_id, "tool_version": snap["versions"]["tool_version"],
                       "engine_tag": snap["versions"]["engine_tag"], "facts_version": snap["versions"]["facts_version"],
                       "rules_versions": snap["versions"]["rules_versions"], "records_digest": snap["records_digest"],
                       "settings_digest": snap["conditions"]["settings_digest"]},
    }
    store.set_result(run_id, result)
    return store.result(run_id)


# ── planning ────────────────────────────────────────────────────────────────────

def _plan(run_id: str, job_id: str, client: Any) -> None:
    snap, private = store.get(run_id), store.private(run_id)
    request = snap["request"]
    if request['input_kind'] == 'magnets':
        return _plan_magnets(run_id, job_id, client)
    paths = list(request["paths"])
    run_dir: Path = private["run_dir"]
    jobs.append_event(job_id, f"Planning {len(paths)} file(s)")
    _check_cancel(job_id)
    versions = _versions(client.version(), request)
    evaluated, settings_bytes, settings_path = [], None, None
    if request["results"] != "overall":
        basis = _device_basis(client, request)
        evaluated, settings_path = basis["evaluated"], basis["settings_path"]
        settings_bytes = _read_settings(settings_path)
    _check_cancel(job_id)
    selection = _selection(request)
    if request["depth"] == "quick":
        facts = client.facts(paths, cancel=lambda: jobs.is_cancelled(job_id))
        if facts is None:
            raise _Cancelled()
        snapshot, estimate = _snapshot_from_facts(paths, facts), None
    else:
        plan = client.plan_compare(paths, snap["reports_dir"], cancel=lambda: jobs.is_cancelled(job_id), **selection)
        if plan is None:
            raise _Cancelled()
        snapshot = {"schema": SNAPSHOT_SCHEMA, "facts_version": plan["records"][0]["facts_version"] if plan["records"]
                    else versions["facts_version"], "records_digest": plan["records_digest"],
                    "records": plan["records"], "candidates": plan["candidates"]}
        estimate = dict(plan["estimate"])
        if not plan["records"]:
            raise _Failure("no_files", "None of the files could be read.")
    if [c["path"] for c in snapshot["candidates"]] != paths or records_digest(
            snapshot["records"], snapshot["candidates"], snapshot["facts_version"]) != snapshot["records_digest"]:
        raise _Failure("contract", "VideoSuite returned facts that do not match the submitted files.")
    if versions["facts_version"] is not None and versions["facts_version"] != snapshot["facts_version"]:
        raise _Failure("contract", "VideoSuite facts version changed during planning.")
    versions["facts_version"] = snapshot["facts_version"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "records.json").write_bytes(_canonical(snapshot))              # private, run-owned facts
    settings_file = None
    if request["results"] != "overall":
        settings_file = run_dir / "settings.json"
        if settings_bytes is not None:
            settings_file.write_bytes(settings_bytes)                         # exact bytes; original untouched
    private.update(snapshot=snapshot, settings_path=settings_path, settings_file=settings_file,
                   basis=basis_digest(request, settings_bytes=settings_bytes, versions=versions))
    conditions = dict(snap["conditions"], evaluated_players=evaluated,
                      settings_digest=None if settings_bytes is None else hashlib.sha256(settings_bytes).hexdigest())
    store.update(run_id, stage="awaiting_confirmation", candidates=_run_candidates(snapshot["candidates"]),
                 conditions=conditions, versions=versions, records_digest=snapshot["records_digest"],
                 estimate=estimate)


def plan_run(run_id: str, client: Any, *, lease: Any = None) -> None:
    """Planning job: facts once, frozen basis, ``awaiting_confirmation``. Holds only ``lease`` (lightweight)."""
    job_id = store.get(run_id)["job_id"]

    def body(job_id: str) -> None:
        try:
            _plan(run_id, job_id, client)
            jobs.update(job_id, progress="1/1")
            jobs.append_event(job_id, "Plan ready: confirm to start.")
            jobs.finish(job_id)
        except _Cancelled:
            store.update(run_id, stage="cancelled")
            jobs.append_event(job_id, "Cancelled.")
            jobs.request_cancel(job_id)
            jobs.finish(job_id)
        except BaseException as exc:
            kind, message = _classify(exc)
            store.update(run_id, stage="error")
            jobs.append_event(job_id, "Cancelled." if jobs.is_cancelled(job_id) else f"Error: {message}")
            jobs.fail(job_id, message, kind)
            if not isinstance(exc, Exception):
                raise

    try:
        proc_guard.job_worker(body)(job_id)
    finally:
        if lease is not None:
            lease.release()
        if store.get(run_id)["stage"] == "planning":          # refused at shutdown before it began
            store.update(run_id, stage="cancelled")


def _classify(exc: BaseException) -> tuple:
    if isinstance(exc, _Failure):
        return exc.kind, exc.message
    if isinstance(exc, SuiteError):
        return exc.kind, exc.message
    if isinstance(exc, SuiteArgumentError):
        return "usage", str(exc)
    if isinstance(exc, SuiteUnavailable):
        return "unavailable", "VideoSuite is not available."
    if isinstance(exc, ContractError):
        logger.error("comparison run contract violation: %s", exc)
        return "contract", "The comparison result failed its contract."
    logger.exception("comparison run failed")
    return "internal", "internal error"


# ── execution ───────────────────────────────────────────────────────────────────

def _execute(run_id: str, job_id: str, client: Any, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    snap, private = store.get(run_id), store.private(run_id)
    request = snap["request"]
    if request['input_kind'] == 'magnets':
        return _execute_magnets(run_id, job_id, client, state)
    snapshot = private["snapshot"]
    paths = [c["path"] for c in snapshot["candidates"]]
    device_run = request["results"] != "overall"
    run_client = client_for_snapshot(client, private["settings_file"] if device_run else None)
    cancel = lambda: jobs.is_cancelled(job_id)                                   # noqa: E731
    if request["depth"] == "quick":
        state["completed"] = ["planning", "facts"]
        _check_cancel(job_id)
        jobs.append_event(job_id, "Estimating from metadata (no pixel measurement)")
        doc = run_client.quick(snapshot, cancel=cancel, **_selection(request))
        if doc is None:
            raise _Cancelled()
        state['overall'] = doc['overall'] if request['results'] == 'both' else None
        state["completed"].append("quick")
        if _basis_changed(run_id, client):
            raise _Failure("settings_changed", "VideoSuite settings or device rules changed during the run.")
        return _publish(run_id, "done", completed=state["completed"],
                        overall=doc["overall"] if request["results"] != "device" else None,
                        device=dict(doc) if device_run else None)
    state["completed"] = ["planning", "facts"]
    _check_cancel(job_id)
    store.update(run_id, stage="pixels")
    jobs.append_event(job_id, f"Comparing every pair of {len(paths)} file(s) (accurate)")
    progress = _progress(job_id)
    compare = run_client.compare(paths, snap["reports_dir"], "accurate", all_candidates=True, records=snapshot,
                                 on_line=progress, cancel=cancel, **_selection(request))
    if compare is None:
        raise _Cancelled()
    compare = dict(compare)
    state["compare"] = compare
    _register_reports(run_id, compare)
    state["completed"].append("pixels")
    _check_cancel(job_id)                                    # a cancelled run never starts rank
    if _basis_changed(run_id, client) and (request['results'] != 'both' or _neutral_basis_changed(run_id, client)):
        raise _Failure("settings_changed", "VideoSuite settings or device rules changed during the run.")
    store.update(run_id, stage="rank")
    jobs.append_event(job_id, "Ranking")
    answers: Dict[str, Any] = {}
    for results in (["overall", "device"] if request["results"] == "both" else [request["results"]]):
        if results == 'device' and _basis_changed(run_id, client):
            raise _Failure('settings_changed', 'VideoSuite settings or device rules changed during the run.')
        doc = run_client.rank(paths, snap["reports_dir"], records=snapshot, cancel=cancel,
                              **_selection(request, results))
        if doc is None:
            raise _Cancelled()
        answers[results] = dict(doc)
        if results == 'overall' and request['results'] == 'both':
            state['overall'] = dict(doc)
            state['completed'].append('rank')
        _check_cancel(job_id)
    if _basis_changed(run_id, client):
        raise _Failure("settings_changed", "VideoSuite settings or device rules changed during the run.")
    if 'rank' not in state['completed']:
        state["completed"].append("rank")
    failed = compare.get("failed", 0)
    failures = [] if not failed else [dict(candidate_id=None, stage="pixels", kind="failed_pairs",
                                           message=f"{failed} comparison pair(s) failed")]
    result = _publish(run_id, "error" if failed else "done", completed=state["completed"],
                      overall=answers.get("overall"), device=answers.get("device"), failures=failures,
                      compare_doc=compare)
    if failed:
        raise _Failure("failed_pairs", f"{failed} comparison pair(s) failed")
    return result


def _progress(job_id: str) -> Callable[[str], None]:
    def on_line(line: str) -> None:
        jobs.update(job_id, current=line[:200])
    return on_line


def execute_run(run_id: str, client: Any, *, lease: Any = None) -> None:
    """The confirmed chain under ONE lease (released on every exit); no browser involvement."""
    job_id = store.get(run_id)["job_id"]
    state: Dict[str, Any] = {"completed": ["planning", "facts"]}

    def partial(stage: str, failure: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            overall = (state.get('overall') if failure and failure['kind'] == 'settings_changed'
                       and not _neutral_basis_changed(run_id, client) else None)
            return _publish(run_id, stage, completed=state["completed"], failures=[failure] if failure else [],
                            compare_doc=state.get("compare"), overall=overall)
        except Exception:
            logger.exception("comparison run %s: could not record its partial result", run_id)
            store.update(run_id, stage=stage)
            return None

    def body(job_id: str) -> None:
        try:
            result = _execute(run_id, job_id, client, state)
            jobs.update(job_id, progress="1/1")
            jobs.append_event(job_id, "Done.")
            jobs.finish(job_id, result)
        except _Cancelled:
            jobs.request_cancel(job_id)
            result = partial("cancelled")
            jobs.append_event(job_id, "Cancelled.")
            jobs.finish(job_id, result)
        except BaseException as exc:
            kind, message = _classify(exc)
            if store.get(run_id)["request"]["depth"] == "quick":
                failed_stage = "quick"
            else:
                failed_stage = "rank" if "pixels" in state["completed"] else "pixels"
            result = store.result(run_id) if kind == "failed_pairs" else partial(
                "error", dict(candidate_id=None, stage=failed_stage, kind=kind, message=message))
            if result is not None:
                jobs.update(job_id, results=result)
            jobs.append_event(job_id, "Cancelled." if jobs.is_cancelled(job_id) else f"Error: {message}")
            jobs.fail(job_id, message, kind)
            if not isinstance(exc, Exception):
                raise

    try:
        proc_guard.job_worker(body)(job_id)
    finally:
        if lease is not None:
            lease.release()
        if store.get(run_id)["stage"] not in FINAL_STAGES:      # refused at shutdown before it began
            store.update(run_id, stage="cancelled")


def _torrent(*args, **kwargs):
    with magnet.port_allocator.lease() as port:
        return magnet.run_magnet_job_threaded(*args, listen_port=port, **kwargs)


def _magnet_failure(exc: Exception, uri: str, stage: str) -> tuple[str, str]:
    """One safe diagnostic and one plain reason, shared by both torrent stages.

    Never log raw tracebacks here: native exception messages/causes can contain
    full tracker URLs and their credentials. Retain the hash, type and redacted
    message so the installed backend log can distinguish discovery from runtime.
    """
    def safe(message):
        message = re.sub(r"(?i)(?:magnet:\?[^\s'\"<>]+|[a-z][a-z0-9+.-]*://[^\s'\"<>]+)",
                         '<redacted URI>', str(message))
        message = re.sub(r"(?i)\b[\w-]*(?:token|secret|password|passkey|api[_-]?key|authorization)[\w-]*"
                         r"\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", '<redacted credential>', message)
        return ''.join(c if c.isprintable() else ' ' for c in message)[:500]

    match = re.search(r'(?i)urn:btih:([0-9a-f]{40}|[a-z2-7]{32})(?=&|$)', uri)
    details = f'{type(exc).__name__}: {safe(exc)}'
    if exc.__cause__ is not None:
        details += f'; caused by {type(exc.__cause__).__name__}: {safe(exc.__cause__)}'
    logger.warning('Magnet %s hash=%s: %s', stage, match.group(1) if match else 'unknown', details)

    if isinstance(exc, magnet.MagnetUnavailable):
        return 'unavailable', str(exc)
    if isinstance(exc, RuntimeError) and str(exc).strip().lower() == 'cancelled':
        return 'cancelled', 'Torrent reading was cancelled.'
    if isinstance(exc, TimeoutError):
        if isinstance(exc, magnet.MagnetMetadataTimeout) or stage == 'metadata':
            seconds = exc.seconds if isinstance(exc, magnet.MagnetMetadataTimeout) else magnet.METADATA_TIMEOUT_S
            return 'timeout', (f'No peers answered within {seconds:g} seconds: the torrent may have no live seeds, '
                               'or your firewall blocks the app. Try again or use another link.')
        return 'timeout', 'Torrent reading timed out. Try again or use another link with live seeds.'
    if isinstance(exc, ValueError):
        return 'invalid_uri', 'This magnet link or its selected video entry is invalid. Check the link and choose the video again.'
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC or getattr(exc, 'winerror', None) == 112:
            return 'disk_space', 'Not enough free space for temporary torrent files. Free space on the temporary drive and try again.'
        return 'storage', 'The app could not read or write its temporary torrent files. Check folder access and try again.'
    if isinstance(exc, magnet.NoFreePort):
        return 'busy', 'All torrent connections are busy. Wait for another magnet job to finish and try again.'
    if isinstance(exc, RuntimeError):
        return 'torrent_runtime', 'The torrent engine failed while reading this link. Try again or check the backend log.'
    return 'internal', 'An unexpected error stopped torrent reading. Check the backend log for details.'


def _plan_magnets(run_id, job_id, client):
    snap, private = store.get(run_id), store.private(run_id)
    request = snap['request']
    versions = _versions(client.version(), request)
    evaluated, settings_bytes, settings_path = [], None, None
    if request['results'] != 'overall':
        basis = _device_basis(client, request)
        evaluated, settings_path = basis['evaluated'], basis['settings_path']
        settings_bytes = _read_settings(settings_path)
    candidates = []
    for i, link in enumerate(request['magnets']):
        _check_cancel(job_id)
        try:
            doc = _torrent(link['uri'], True, _progress(job_id),
                                                 lambda: jobs.is_cancelled(job_id),
                                                 metadata_only=True, use_default_scorer=False)
            entries = [dict(index=f['index'], name=f['name'], size=f['size_bytes'])
                       for f in doc['files'] if f['verdict'] == 'good']
            index = link['file_index']
            if index is None and len(entries) == 1:
                index = entries[0]['index']
            selected = next((e for e in entries if e['index'] == index), None)
            status = 'pending' if selected else 'selection_required' if index is None and entries else 'failed'
            reason = None if selected else 'Choose one video entry for this link.' if entries and index is None else 'No selected playable entry.'
        except magnet.MagnetUnavailable as exc:
            _magnet_failure(exc, link['uri'], 'metadata')
            raise _Failure('unavailable', str(exc)) from None
        except Exception as exc:
            kind, reason = _magnet_failure(exc, link['uri'], 'metadata')
            if kind == 'cancelled':
                raise _Cancelled() from None
            _check_cancel(job_id)
            entries, index, status, selected = [], link['file_index'], 'failed', None
        candidates.append(dict(candidate_id=f'c{i}', kind='magnet', path=None, uri=link['uri'],
                               file_index=index, status=status, record_id=None, reason=reason,
                               size=selected['size'] if selected else None, mtime_ns=None, entries=entries))
    _check_cancel(job_id)
    run_dir = private['run_dir']
    run_dir.mkdir(parents=True, exist_ok=True)
    settings_file = None
    if request['results'] != 'overall':
        settings_file = run_dir / 'settings.json'
        if settings_bytes is not None:
            settings_file.write_bytes(settings_bytes)
    private.update(settings_path=settings_path, settings_file=settings_file,
                   basis=basis_digest(request, settings_bytes=settings_bytes, versions=versions))
    conditions = dict(snap['conditions'], evaluated_players=evaluated,
                      settings_digest=hashlib.sha256(settings_bytes).hexdigest() if settings_bytes is not None else None)
    store.update(run_id, candidates=candidates, versions=versions, conditions=conditions,
                 stage='selection_required' if any(c['status'] == 'selection_required' for c in candidates) else 'awaiting_confirmation')


def _execute_magnets(run_id, job_id, client, state):
    snap, private = store.get(run_id), store.private(run_id)
    request = snap['request']
    run_client = client_for_snapshot(client, private['settings_file'])
    cancel = lambda: jobs.is_cancelled(job_id)
    suite_elapsed = 0.0
    rows, candidates = [], copy.deepcopy(snap['candidates'])
    store.update(run_id, stage='facts')
    for candidate in candidates:
        _check_cancel(job_id)
        key = candidate['candidate_id']
        # Failed links remain visible even when other links yield useful facts.
        row = dict(key=key, path=str(private['run_dir'] / (key + '.mkv')),
                   real_name=key + '.mkv', real_size=candidate['size'] or 0, coverage='head')
        if candidate['status'] != 'failed':
            detached = []
            def facts_callback(entries, emit, cancel_check):
                nonlocal suite_elapsed
                stage_start = time.monotonic()
                facts = magnet_suite.partial_facts(entries, emit, cancel_check, client=run_client,
                                                   start=stage_start - suite_elapsed)
                suite_elapsed += time.monotonic() - stage_start
                detached.extend(facts)
                return {e['key']: dict(status='ok' if 'record' in e else 'error', record=e.get('record')) for e in facts}
            try:
                doc = _torrent(candidate['uri'], True, _progress(job_id), cancel,
                                                     partial_scorer=facts_callback, use_default_scorer=False,
                                                     selected_file_index=candidate['file_index'])
                # Recheck immutable metadata identity before accepting retained facts.
                selected = next((f for f in doc['files'] if f['index'] == candidate['file_index']), None)
                expected = next((f for f in candidate['entries'] if f['index'] == candidate['file_index']), None)
                if not selected or not expected or (selected['name'], selected['size_bytes']) != (expected['name'], expected['size']):
                    raise ValueError('Selected torrent entry changed')
                if detached:
                    row.update(detached[0], key=key)
            except magnet.MagnetUnavailable as exc:
                _magnet_failure(exc, candidate['uri'], 'partial_facts')
                row['error'] = dict(status='unavailable', error_kind='unavailable', message=str(exc))
            except Exception as exc:
                kind, message = _magnet_failure(exc, candidate['uri'], 'partial_facts')
                if kind == 'cancelled':
                    raise _Cancelled() from None
                _check_cancel(job_id)
                row['error'] = dict(status='error', error_kind=kind, message=message)
        if 'record' not in row:
            row.setdefault('error', dict(status='error', error_kind='probe', message=candidate['reason'] or 'No partial facts.'))
            candidate.update(status='failed', reason=row['error']['message'])
        else:
            candidate.update(status='ready', record_id=row['record']['id'], reason=None)
        rows.append(row)
    snapshot = magnet_suite.partial_snapshot(rows)
    private['snapshot'] = snapshot
    store.update(run_id, candidates=candidates, records_digest=snapshot['records_digest'])
    _check_cancel(job_id)
    changed = _basis_changed(run_id, client)
    if changed and (request['results'] != 'both' or _neutral_basis_changed(run_id, client)):
        raise _Failure('settings_changed', 'VideoSuite settings or device rules changed during the run.')
    quick_start = time.monotonic()
    suite_cancel = lambda: cancel() or suite_elapsed + time.monotonic() - quick_start >= magnet_suite.SUITE_MAGNET_BUDGET_S
    recommendations = magnet_suite.recommend_partials(rows, _progress(job_id), suite_cancel, client_factory=lambda: run_client,
                                                       **_selection(request, 'overall' if changed else None))
    _check_cancel(job_id)
    doc = next((r['recommendation'] for r in recommendations.values() if r.get('recommendation')), None)
    failures = [dict(candidate_id=k, stage='quick', kind=r.get('error_kind', r['status']),
                     message=r.get('message', 'Partial recommendation unavailable.'))
                for k, r in recommendations.items() if r['status'] != 'ok']
    state['completed'] = ['planning', 'facts'] + (['quick'] if doc else [])
    state['overall'] = doc['overall'] if doc and request['results'] == 'both' else None
    if changed or _basis_changed(run_id, client):
        raise _Failure('settings_changed', 'VideoSuite settings or device rules changed during the run.')
    return _publish(run_id, 'done' if doc else 'error', completed=state['completed'],
                    overall=doc['overall'] if doc and request['results'] != 'device' else None,
                    device=doc if doc and request['results'] != 'overall' else None, failures=failures)
