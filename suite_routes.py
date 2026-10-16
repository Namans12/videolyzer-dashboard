"""``/suite/...`` endpoints: the installed VideoSuite as guarded Dashboard jobs.

* ``GET  /suite/devices``  the CLI's device catalogue, cached 60 s (``reset_state`` clears it).
* ``GET  /suite/settings`` the owner-settings facts of the cached devices document; same gate, cache and errors as ``/suite/devices``.
  503 ``SuiteUnavailable`` (or a process that cannot start), 504 timeout, 502 every other
  failure (exit code 2 = a suite older than 0.3.0 without ``devices``: 502 with ``DEVICES_TOO_OLD_DETAIL``).
  HTTP errors never carry stderr text.
* ``POST /suite/score``    ``{paths, device?, player?, fs?, lang?, deep_dv?}`` -> ``{job_id}``
* ``POST /suite/rank``     ``{paths, reports_dir, device?, player?, fs?, lang?, wq?, wp?}`` -> ``{job_id}``
* ``POST /suite/compare``  ``{paths, out_dir, mode, top?}`` -> ``{job_id}`` (progress = stderr lines;
  cancel with ``POST /job/{id}/cancel``)

Request bodies are pydantic models (``extra="forbid"``) whose value rules are the D10 client's own
``_check_*`` functions (one source of truth). Every path goes through the D3 path guard (400/403/404, never
an echo of exception text): ``paths`` are video FILES, ``reports_dir`` an existing folder, ``out_dir`` a
folder that need not exist but whose parent must (and ``out_dir`` may not contain shell metacharacters).

Jobs (kinds ``suite_score|suite_rank|suite_compare``) answer HTTP 200 whatever happens; the outcome is
``status``: ``done`` with ``results`` = the CLI JSON, ``error`` with ``error`` (short message), ``error_kind``
(a ``suite_client.ERROR_KINDS`` value, or ``busy``/``internal``), ``error_detail`` (the last 500 characters of
stderr) and, when the CLI got as far as exiting, ``exit_code``; or ``cancelled`` (a running job has its CLI process
tree killed; a job cancelled while queued never starts the CLI). A compare whose CLI reported ``failed > 0`` keeps ``results`` and ends ``error`` /
``failed_pairs``.

Concurrency: one heavy job (compare or deep-DV score) at a time (409), at most two score/rank jobs (409). The
route takes the slot atomically and hands it to the runner (``_slot_held=True``); a runner called directly takes
the slot itself and ends its job ``busy`` if there is none. The slot is released in ``finally``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Annotated, Any, Callable, Dict, List, Literal, Mapping, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator, model_validator

from jobs import jobs
import suite_version
from path_guard import PathRejected, validate_user_path
from suite_client import (SuiteArgumentError, SuiteError, SuiteUnavailable, _check_dir, _check_fs, _check_lang,
                          _check_name, _check_weight, get_suite_client)

logger = logging.getLogger("video-analyzer.suite")

router = APIRouter(prefix="/suite", tags=["suite"])

DEVICES_TOO_OLD_DETAIL = ("VideoSuite does not support 'devices'; reinstall the Comparizer "
                          "(videosuite 0.3.0 or later)")
UNAVAILABLE_DETAIL = "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"
COMPARE_BUSY_DETAIL = "a comparison is already running"
SCORE_RANK_BUSY_DETAIL = "too many suite jobs are running"
HEAVY_TEXT = {"compare": "a comparison is already running", "deep_dv": "a deep Dolby Vision scan is already running"}

MAX_PATHS = 50
MAX_SCORE_RANK_JOBS = 2
DEVICES_CACHE_SECONDS = 60.0
MAX_PROGRESS_EVENTS = 2000
_MAX_LINE = 500
_MAX_DETAIL = 500

_clock: Callable[[], float] = time.monotonic        # patched by tests


# ── state: devices cache and concurrency slots ───────────────────────────────────

_cache_lock = threading.Lock()
_devices_cache: "Optional[tuple[float, dict]]" = None


class _Slots:
    """One heavy job, ``MAX_SCORE_RANK_JOBS`` score/rank jobs; check-and-take is atomic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._heavy: Optional[str] = None
        self._score_rank = 0

    def acquire(self, slot: str) -> bool:
        return self.try_acquire(slot) is None

    def try_acquire(self, slot: str) -> Optional[str]:
        with self._lock:
            if slot != "compare" and self._score_rank >= MAX_SCORE_RANK_JOBS:
                return SCORE_RANK_BUSY_DETAIL
            if slot in ("compare", "score_rank+deep_dv") and self._heavy is not None:
                if slot == "compare" and self._heavy == "compare":
                    return COMPARE_BUSY_DETAIL
                return (f"{HEAVY_TEXT[self._heavy]}; only one heavy job "
                        "(a picture comparison or a deep Dolby Vision scan) runs at a time")
            if slot == "compare":
                self._heavy = "compare"
            else:
                self._score_rank += 1
                if slot == "score_rank+deep_dv":
                    self._heavy = "deep_dv"
            return None

    def release(self, slot: str) -> None:
        with self._lock:
            if slot == "compare":
                self._heavy = None
            else:
                self._score_rank = max(0, self._score_rank - 1)
                if slot == "score_rank+deep_dv":
                    self._heavy = None

    def reset(self) -> None:
        with self._lock:
            self._heavy = None
            self._score_rank = 0


_slots = _Slots()


def reset_state() -> None:
    """Forget the cached devices document and free every job slot (tests: ``clean_state``)."""
    global _devices_cache
    with _cache_lock:
        _devices_cache = None
    _slots.reset()


# ── request models ───────────────────────────────────────────────────────────────

PathList = Annotated[List[Annotated[str, StringConstraints(max_length=4096)]], Field(min_length=1, max_length=MAX_PATHS)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DeviceOptions(_Strict):
    device: Optional[str] = None
    player: Optional[str] = None
    fs: Optional[str] = None
    lang: Optional[str] = None

    @field_validator("device", "player")
    @classmethod
    def _names(cls, value: Optional[str], info: Any) -> Optional[str]:
        return None if value is None else _check_name(value, info.field_name)

    @field_validator("fs")
    @classmethod
    def _fs(cls, value: Optional[str]) -> Optional[str]:
        return _check_fs(value)

    @field_validator("lang")
    @classmethod
    def _lang(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _check_lang(value)


class ScoreRequest(_DeviceOptions):
    paths: PathList
    deep_dv: Optional[StrictBool] = None


class RankRequest(_DeviceOptions):
    paths: PathList
    reports_dir: str = Field(max_length=4096)
    wq: Optional[float] = None
    wp: Optional[float] = None

    @field_validator("wq", "wp", mode="before")
    @classmethod
    def _weights(cls, value: Any, info: Any) -> Optional[float]:
        return None if value is None else _check_weight(value, info.field_name)

    @model_validator(mode="after")
    def _not_both_zero(self) -> "RankRequest":
        if self.wq == 0 and self.wp == 0:
            raise ValueError("wq and wp cannot both be zero")
        return self


class CompareRequest(_Strict):
    paths: PathList
    out_dir: str = Field(max_length=4096)
    mode: Literal["fast", "accurate"]
    top: Optional[Annotated[int, Field(strict=True, ge=1, le=6)]] = None


# ── path guard glue ──────────────────────────────────────────────────────────────

def _guarded_path(raw: str, **kwargs: Any) -> str:
    try:
        return str(validate_user_path(raw, **kwargs))
    except PathRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


def _guarded_paths(paths: List[str]) -> List[str]:
    return [_guarded_path(p, must_be="file") for p in paths]


def _guarded_folder(raw: str, what: str, *, must_exist: bool) -> str:
    resolved = _guarded_path(raw, must_be="dir", must_exist=must_exist)
    try:
        return _check_dir(resolved, what)
    except SuiteArgumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _options(**values: Any) -> Dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


# ── job plumbing ─────────────────────────────────────────────────────────────────

def _emit(job_id: str, msg: str) -> None:
    jobs.append_event(job_id, msg)


def _fail(job_id: str, message: str, kind: str, detail: str = "", exit_code: Optional[int] = None) -> None:
    """Emit the failure event, THEN end the job (a cancel flag turns it into ``cancelled``)."""
    _emit(job_id, "Cancelled." if jobs.is_cancelled(job_id) else f"Error: {message}")
    if detail:
        jobs.update(job_id, error_detail=detail[-_MAX_DETAIL:])
    if exit_code is not None:
        jobs.update(job_id, exit_code=exit_code)
    jobs.fail(job_id, message, kind)


def _done(job_id: str, doc: Any, msg: str = "Done.") -> None:
    _emit(job_id, "Cancelled." if jobs.is_cancelled(job_id) else msg)
    jobs.update(job_id, progress="1/1")
    jobs.finish(job_id, dict(doc))


def _end_cancelled(job_id: str) -> None:
    """End a job that was cancelled (while queued, or after the client killed the CLI) as ``cancelled``."""
    jobs.request_cancel(job_id)                         # (no-op when the flag is already set)
    _emit(job_id, "Cancelled.")
    jobs.finish(job_id)


def _run_slotted(job_id: str, slot: str, slot_held: bool, body: Callable[[], None]) -> None:
    """Run ``body`` holding ``slot`` and map every failure to an honest job end.

    A job that was cancelled while it was still queued never starts the CLI."""
    if not slot_held:
        detail = _slots.try_acquire(slot)
        if detail is not None:
            _fail(job_id, detail, "busy")
            return
    try:
        if jobs.is_cancelled(job_id):
            _end_cancelled(job_id)
            return
        body()
    except SuiteError as exc:
        _fail(job_id, exc.message, exc.kind, exc.stderr_tail, exc.exit_code)
    except SuiteArgumentError as exc:
        _fail(job_id, str(exc), "usage")
    except SuiteUnavailable:
        _fail(job_id, "VideoSuite is not available.", "unavailable")
    except Exception:                                   # never let a runner end without ending its job
        logger.exception("suite job %s failed", job_id)
        _fail(job_id, "internal error", "internal")
    finally:
        _slots.release(slot)


def run_score_job(job_id: str, client: Any, paths: List[str], *, device: Optional[str] = None,
                  player: Optional[str] = None, fs: Optional[str] = None, lang: Optional[str] = None,
                  deep_dv: Optional[bool] = None, _slot_held: bool = False) -> None:
    def body() -> None:
        _emit(job_id, f"Scoring {len(paths)} file(s)")
        doc = client.score(paths, cancel=lambda: jobs.is_cancelled(job_id),
                           **_options(device=device, player=player, fs=fs, lang=lang, deep_dv=deep_dv))
        if doc is None:                                 # the client was cancelled and killed the process tree
            _end_cancelled(job_id)
            return
        _done(job_id, doc)

    _run_slotted(job_id, "score_rank+deep_dv" if deep_dv else "score_rank", _slot_held, body)


def run_rank_job(job_id: str, client: Any, paths: List[str], reports_dir: str, *, device: Optional[str] = None,
                 player: Optional[str] = None, fs: Optional[str] = None, lang: Optional[str] = None,
                 wq: Optional[float] = None, wp: Optional[float] = None, _slot_held: bool = False) -> None:
    def body() -> None:
        _emit(job_id, f"Ranking {len(paths)} file(s)")
        doc = client.rank(paths, reports_dir, cancel=lambda: jobs.is_cancelled(job_id),
                          **_options(device=device, player=player, fs=fs, lang=lang, wq=wq, wp=wp))
        if doc is None:                                 # the client was cancelled and killed the process tree
            _end_cancelled(job_id)
            return
        _done(job_id, doc)

    _run_slotted(job_id, "score_rank", _slot_held, body)


def run_compare_job(job_id: str, client: Any, paths: List[str], out_dir: str, mode: str = "fast",
                    top: Optional[int] = None, *, _slot_held: bool = False) -> None:
    def body() -> None:
        _emit(job_id, f"Comparing {len(paths)} file(s) ({mode})")
        lines = 0

        def on_line(line: str) -> None:
            nonlocal lines
            jobs.update(job_id, current=line[:200])
            lines += 1
            if lines <= MAX_PROGRESS_EVENTS:
                _emit(job_id, line[:_MAX_LINE])
            elif lines == MAX_PROGRESS_EVENTS + 1:
                _emit(job_id, "(further progress lines are not recorded)")

        doc = client.compare(paths, out_dir, mode, 4 if top is None else top, on_line=on_line,
                             cancel=lambda: jobs.is_cancelled(job_id))
        if doc is None:                                 # the client was cancelled and killed the process tree
            _end_cancelled(job_id)
            return
        failed = doc.get("failed", 0)
        if failed:
            jobs.update(job_id, results=dict(doc), progress="1/1")
            _fail(job_id, f"{failed} comparison pair(s) failed", "failed_pairs", getattr(doc, "stderr_tail", ""),
                  getattr(doc, "exit_code", None))
            return
        _done(job_id, doc)

    _run_slotted(job_id, "compare", _slot_held, body)


# ── routes ───────────────────────────────────────────────────────────────────────

def _suite_unavailable(_request: Any, _exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": UNAVAILABLE_DETAIL})


def install(app: FastAPI) -> None:
    """Mount the router and the 503 mapping for a client that cannot be built."""
    app.include_router(router)
    app.add_exception_handler(SuiteUnavailable, _suite_unavailable)


@router.get("/version")
def suite_version_status(request: Request, refresh: bool = False) -> Any:
    # Discovery happens inside the route so absence is a 200 handshake result.
    factory = request.app.dependency_overrides.get(get_suite_client, get_suite_client)
    return suite_version.ensure(suite_version._discover(factory), refresh=refresh)


_fill_lock = threading.Lock()


def _fresh_devices() -> Optional[dict]:
    with _cache_lock:
        cached = _devices_cache
        if cached is not None and _clock() - cached[0] < DEVICES_CACHE_SECONDS:
            return cached[1]
    return None


def _devices_document(client: Any) -> Dict[str, Any]:
    """Version gate first; serialized cache fills; errors are never cached."""
    global _devices_cache
    version = suite_version.ensure(client)
    if version["status"] == "too_old":
        raise HTTPException(status_code=502, detail=version["message"])
    doc = _fresh_devices()
    if doc is not None:
        return doc
    with _fill_lock:
        doc = _fresh_devices()
        if doc is not None:
            return doc
        try:
            doc = dict(client.devices())
        except SuiteError as exc:
            if exc.kind == "unavailable":
                raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL) from None
            if exc.kind == "timeout":
                raise HTTPException(status_code=504, detail="VideoSuite timed out.") from None
            if exc.kind == "usage":
                raise HTTPException(status_code=502, detail=DEVICES_TOO_OLD_DETAIL) from None
            if exc.kind == "contract":
                raise HTTPException(status_code=502, detail="VideoSuite returned an unusable response.") from None
            raise HTTPException(status_code=502, detail="VideoSuite failed.") from None
        with _cache_lock:
            _devices_cache = (_clock(), doc)
        return doc


@router.get("/devices")
def suite_devices(client: Any = Depends(get_suite_client)) -> Any:
    return _devices_document(client)


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def settings_view(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Read-only settings facts; malformed optional values never cause a 500."""
    block = doc.get("settings")
    supported = isinstance(block, dict)
    block = block if supported else {}
    count = block.get("applied_count")
    devices: Dict[str, Any] = {}
    for dev in _list(doc.get("devices")):
        if not isinstance(dev, dict) or not isinstance(dev.get("id"), str):
            continue
        own = dev.get("settings") if isinstance(dev.get("settings"), dict) else {}
        overrides = []
        for player in _list(dev.get("players")):
            if not isinstance(player, dict):
                continue
            for item in _list(player.get("overrides")):
                if isinstance(item, dict):
                    overrides.append({"player": _opt_str(player.get("id")), "rule": _opt_str(item.get("rule")),
                                      "source": _opt_str(item.get("source")), "reason": _opt_str(item.get("reason")),
                                      "confidence": _opt_str(item.get("confidence"))})
        devices[dev["id"]] = {"label": _opt_str(dev.get("label")) or dev["id"],
                              "usb_filesystem": _opt_str(own.get("usb_filesystem")),
                              "audio_output": _opt_str(own.get("audio_output")), "overrides": overrides}
    return {"tool_version": _opt_str(doc.get("tool_version")), "supported": supported,
            "path": _opt_str(block.get("path")), "loaded": block.get("loaded") is True,
            "errors": [e for e in _list(block.get("errors")) if isinstance(e, str)],
            "applied_count": count if isinstance(count, int) and not isinstance(count, bool) else 0,
            "devices": devices}


@router.get("/settings")
def suite_settings(client: Any = Depends(get_suite_client)) -> Any:
    """Owner settings reported by VideoSuite; never reads the settings file itself."""
    return settings_view(_devices_document(client))


def _take_slot(slot: str) -> None:
    detail = _slots.try_acquire(slot)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


def _start(slot: str, kind: str, total: int, background_tasks: BackgroundTasks, runner: Callable[..., None],
           *args: Any, **kwargs: Any) -> Dict[str, str]:
    _take_slot(slot)
    try:
        job_id = jobs.create(kind, progress="0/1", current="", total=total)
        background_tasks.add_task(runner, job_id, *args, _slot_held=True, **kwargs)
    except BaseException:
        _slots.release(slot)
        raise
    return {"job_id": job_id}


@router.post("/score")
def suite_score(body: ScoreRequest, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    paths = _guarded_paths(body.paths)
    return _start("score_rank+deep_dv" if body.deep_dv else "score_rank", "suite_score", len(paths),
                  background_tasks, run_score_job, client, paths,
                  device=body.device, player=body.player, fs=body.fs, lang=body.lang, deep_dv=body.deep_dv)


@router.post("/rank")
def suite_rank(body: RankRequest, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    paths = _guarded_paths(body.paths)
    reports_dir = _guarded_folder(body.reports_dir, "reports_dir", must_exist=True)
    return _start("score_rank", "suite_rank", len(paths), background_tasks, run_rank_job, client, paths, reports_dir,
                  device=body.device, player=body.player, fs=body.fs, lang=body.lang, wq=body.wq, wp=body.wp)


@router.post("/compare")
def suite_compare(body: CompareRequest, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    paths = _guarded_paths(body.paths)
    out_dir = _guarded_folder(body.out_dir, "out_dir", must_exist=False)
    return _start("compare", "suite_compare", len(paths), background_tasks, run_compare_job, client, paths, out_dir,
                  body.mode, body.top)
