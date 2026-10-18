"""``/suite/calibration/...`` endpoints: the Bravia calibration kit workflow of VideoSuite 0.5.0.

* ``POST /suite/calibration/plan``     ``{paths, players?, deep_dv?}``: a SYNCHRONOUS answer (the plan document), or
  ``{job_id}`` when ``deep_dv`` is true (a cancellable job under the heavy ``calibration`` slot)
* ``GET  /suite/calibration/status``   ``?kit=<id>``: the store's view of the (latest or named) kit, read-only
* ``POST /suite/calibration/session``  ``{out, tv_software, audio_output?, usb_filesystem?}``: the conditions the
  owner confirmed (the route resolves ``<out>\\kit.json``)
* ``POST /suite/calibration/record``   ``{kit, test, player, outcome, note?}``
* ``POST /suite/calibration/build``    ``{paths, out, players?, tests?, deep_dv?}`` -> ``{job_id}`` (heavy slot)
* ``POST /suite/calibration/apply``    ``{kit, preview, revision?, confirm_veto?}``: preview or apply
* ``POST /suite/calibration/undo``     ``{kit}``
* ``POST /suite/calibration/report``   ``{kit, paths}`` -> ``{job_id}`` (playback only, read-only; a score/rank slot)

Source ``paths`` are video FILES that need not exist but whose folder must. The suite SKIPS a missing source: it
contributes no record, so an excerpt test that needs it is reported ``needs_real_file`` (no offline planning from
cached records is done; a missing folder, for example an unplugged drive, is a 404). The path guard's raw checks
(UNC, alternate data streams, device names) and the line-separator and control-character checks of the client stay.
``out`` follows the folder policy of ``/suite/compare`` and, for ``build`` and ``session``, is refused (400) when it
or an existing ancestor is a link, symlink or junction (checked on the raw path, before canonicalization hides it).
Request models are strict; a 422 never carries the offending value (no ``input``, no ``ctx``).

Synchronous routes answer the CLI document as it is. Failures never carry stderr text: 503 not installed, 504
timeout, 502 unusable response or failure, 400 a request the CLI refused, and 409 with a ``code`` for the store's own
refusals (``store_damaged``, ``recovery_required``, ``calibration_busy``, ``stale_revision``, ``settings_refused``).
A document with ``recovery_required: true`` is data (200), not an error. After a non-preview apply and after an undo
the cached devices/settings document is dropped under its fill lock (never ``reset_state``, which frees job slots).

Jobs (kinds ``suite_calibration_plan|build|report``) end like the other suite jobs (``done`` / ``error`` with
``error_kind`` / ``cancelled`` through ``POST /job/{id}/cancel``). Plan-with-``deep_dv`` and build take the
``calibration`` slot: one heavy job at a time together with ``compare`` and deep-DV ``score`` (409 both ways).
"""
from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

import suite_routes
from jobs import jobs
from path_guard import _normalise
from suite_client import (SuiteArgumentError, SuiteError, SuiteUnavailable, _calibration_paths, _calibration_text,
                          _check_fs, _check_kit, _check_name, get_suite_client)
from suite_routes import (MAX_PATHS, UNAVAILABLE_DETAIL, _Strict, _done, _emit, _end_cancelled, _guarded_folder,
                          _guarded_paths, _progress_forwarder, _run_slotted, _start)

logger = logging.getLogger("video-analyzer.suite")

MAX_IDS = 64
MAX_VETO_IDS = 200
_CHANGE_ID_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,512}")
KIT_QUERY_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"
CALIBRATE_TOO_OLD_DETAIL = "This VideoSuite has no calibration commands; update VideoSuite to 0.5.0 or later."


# ── 422 without an echo of the input ─────────────────────────────────────────────

def _safe_error(error: Dict[str, Any]) -> Dict[str, Any]:
    """Only ``type``, ``loc`` and ``msg``: never ``input``, never ``ctx`` (they can hold the offending value)."""
    loc = [part if isinstance(part, int) else str(part)[:64] for part in error.get("loc", ())]
    return {"type": str(error.get("type", "")), "loc": loc, "msg": str(error.get("msg", ""))}


class _QuietRoute(APIRoute):
    """A route whose request-validation failure is a 422 listing ``type``/``loc``/``msg`` only."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Any:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(status_code=422, content={"detail": [_safe_error(e) for e in exc.errors()]})

        return handler


router = APIRouter(prefix="/suite/calibration", tags=["suite"], route_class=_QuietRoute)


# ── request models ───────────────────────────────────────────────────────────────

SourcePaths = Annotated[List[Annotated[str, StringConstraints(max_length=4096)]], Field(max_length=MAX_PATHS)]
ReportPaths = Annotated[List[Annotated[str, StringConstraints(max_length=4096)]],
                        Field(min_length=1, max_length=MAX_PATHS)]
IdList = Annotated[List[str], Field(max_length=MAX_IDS)]


class _Ids(_Strict):
    @staticmethod
    def _ids(value: List[str], what: str) -> List[str]:
        return [_check_name(item, what) for item in value]


class PlanRequest(_Ids):
    paths: SourcePaths
    players: IdList = Field(default_factory=list)
    deep_dv: Optional[StrictBool] = None

    @field_validator("players")
    @classmethod
    def _players(cls, value: List[str]) -> List[str]:
        return cls._ids(value, "players")


class BuildRequest(_Ids):
    paths: SourcePaths
    out: str = Field(max_length=4096)
    players: IdList = Field(default_factory=list)
    tests: IdList = Field(default_factory=list)
    deep_dv: Optional[StrictBool] = None

    @field_validator("players", "tests")
    @classmethod
    def _lists(cls, value: List[str], info: Any) -> List[str]:
        return cls._ids(value, info.field_name)


class SessionRequest(_Strict):
    out: str = Field(max_length=4096)
    tv_software: str = Field(max_length=200)
    audio_output: Optional[Literal["tv_speakers", "soundbar", "avr"]] = None
    usb_filesystem: Optional[Literal["fat32", "exfat", "ntfs"]] = None

    @field_validator("tv_software")
    @classmethod
    def _tv(cls, value: str) -> str:
        return _calibration_text(value, "tv_software")

    @field_validator("usb_filesystem")
    @classmethod
    def _fs(cls, value: Optional[str]) -> Optional[str]:
        return _check_fs(value)


class RecordRequest(_Strict):
    kit: str
    test: str
    player: str
    outcome: str
    note: str = Field("", max_length=2000)

    @field_validator("kit")
    @classmethod
    def _kit(cls, value: str) -> str:
        return _check_kit(value)

    @field_validator("test", "player", "outcome")
    @classmethod
    def _names(cls, value: str, info: Any) -> str:
        return _check_name(value, info.field_name)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return _calibration_text(value, "note", empty=True)


class ApplyRequest(_Strict):
    kit: str
    preview: StrictBool
    revision: Optional[str] = Field(None, max_length=64)
    confirm_veto: List[str] = Field(default_factory=list, max_length=MAX_VETO_IDS)

    @field_validator("kit")
    @classmethod
    def _kit(cls, value: str) -> str:
        return _check_kit(value)

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _check_name(value, "revision")

    @field_validator("confirm_veto")
    @classmethod
    def _veto(cls, value: List[str]) -> List[str]:
        # change ids are <test>:<player>:<rule>, the rule possibly with an @container
        if not all(_CHANGE_ID_RE.fullmatch(item) for item in value):
            raise ValueError("confirm_veto contains an invalid change id")
        return value

    @model_validator(mode="after")
    def _preview_or_apply(self) -> "ApplyRequest":
        if self.preview:
            if self.revision is not None or self.confirm_veto:
                raise ValueError("a preview takes no revision or veto confirmations")
        elif self.revision is None:
            raise ValueError("applying needs the revision of a preview")
        return self


class UndoRequest(_Strict):
    kit: str

    @field_validator("kit")
    @classmethod
    def _kit(cls, value: str) -> str:
        return _check_kit(value)


class ReportRequest(_Strict):
    kit: str
    paths: ReportPaths

    @field_validator("kit")
    @classmethod
    def _kit(cls, value: str) -> str:
        return _check_kit(value)


# ── guards and error mapping ─────────────────────────────────────────────────────

def _guarded_sources(paths: List[str]) -> List[str]:
    """Source files that need not exist (the suite skips a missing one), plus the client's own text rules."""
    try:
        # Check raw text before the shared guard trims whitespace/quotes. Otherwise
        # trailing newlines disappear before the client's list-file safety check.
        _calibration_paths(paths)
    except SuiteArgumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    resolved = _guarded_paths(paths, must_exist=False)
    try:
        _calibration_paths(resolved)
    except SuiteArgumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return resolved


def _is_linked(path: Path) -> bool:
    """A symlink, or any Windows reparse point (junctions included), judged with ``lstat`` so it is not followed."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="path not accessible") from None
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")


def _reject_linked_output(raw: str, what: str) -> None:
    """Refuse an output folder that is, or has an existing ancestor that is, a link or junction.

    The shared folder guard canonicalizes (``alias\\kit`` becomes ``target\\kit``) and would hide the link from the
    suite's own output-safety check, so this runs on the RAW path (same normalisation as the guard, then
    ``abspath`` like the CLI) BEFORE ``_guarded_folder``. Anything that is not a drive-absolute path is left to the
    guard's own refusals."""
    text = _normalise(raw)
    if not _DRIVE_PATH_RE.match(text):
        return
    try:
        start = Path(os.path.abspath(text))
    except (OSError, ValueError):
        return
    for part in (start, *start.parents):
        if _is_linked(part):
            raise HTTPException(status_code=400, detail=f"{what} cannot be, or lie inside, a link or junction")


def _guarded_output(raw: str, what: str, *, must_exist: bool) -> str:
    _reject_linked_output(raw, what)
    return _guarded_folder(raw, what, must_exist=must_exist)


_CONFLICTS = (
    (lambda tail: "calibration store" in tail and any(text in tail for text in ("is damaged", "is malformed")), "store_damaged",
     "The calibration store is damaged and was left unchanged. Restore calibration.json.bak or move the file "
     "aside, then try again."),
    (lambda tail: "matches neither the state before nor after" in tail, "recovery_required",
     "An earlier apply or undo was interrupted and the settings file now matches neither state. Restore "
     "settings.json.bak or decide by hand, then try again."),
    (lambda tail: "another VideoSuite calibration command is using" in tail, "calibration_busy",
     "Another calibration command is running. Try again in a moment."),
    (lambda tail: "changed since the preview" in tail, "stale_revision",
     "The store, the catalogue, the settings or the conditions changed since the preview. Preview again and "
     "confirm the new revision."),
    (lambda tail: "settings changed while applying" in tail, "settings_changed",
     "The settings file changed while applying. Preview and apply again."),
    (lambda tail: any(text in tail for text in ("cannot be used as a settings document", "cannot read the settings file",
                                                "rule_overrides is not a list", "the settings file would grow")),
     "settings_refused", "The settings file cannot be updated safely and was not changed. Fix it by hand first."),
)


def _reply(status: int, detail: str, code: Optional[str] = None) -> JSONResponse:
    content: Dict[str, Any] = {"detail": detail}
    if code is not None:
        content["code"] = code
    return JSONResponse(status_code=status, content=content)


def _error_response(exc: SuiteError) -> JSONResponse:
    """A clean answer for a failed synchronous call; stderr text is classified, never forwarded."""
    tail = exc.stderr_tail or ""
    if exc.kind == "unavailable":
        return _reply(503, UNAVAILABLE_DETAIL)
    if exc.kind == "timeout":
        return _reply(504, "VideoSuite timed out.")
    if exc.kind == "contract":
        return _reply(502, "VideoSuite returned an unusable response.")
    if exc.kind == "usage":
        if "invalid choice: 'calibrate'" in tail:
            return _reply(502, CALIBRATE_TOO_OLD_DETAIL, "suite_too_old")
        return _reply(400, "VideoSuite refused the request.", "refused")
    if exc.kind == "failed":
        for matches, code, message in _CONFLICTS:
            if matches(tail):
                return _reply(409, message, code)
    return _reply(502, "VideoSuite failed.")


def _sync(call: Callable[[], Any], after: Optional[Callable[[], None]] = None) -> Any:
    """Run one synchronous suite call; ``after`` runs whatever the outcome (the call may have written settings)."""
    try:
        return dict(call())
    except SuiteUnavailable:
        return _reply(503, UNAVAILABLE_DETAIL)
    except SuiteArgumentError:
        return _reply(400, "invalid request", "invalid")
    except SuiteError as exc:
        return _error_response(exc)
    except Exception:
        logger.exception("synchronous calibration call failed")
        return _reply(500, "internal error", "internal")
    finally:
        if after is not None:
            after()


# ── job runners ──────────────────────────────────────────────────────────────────

def run_calibration_plan_job(job_id: str, client: Any, paths: List[str], players: List[str], *,
                             deep_dv: bool = True, _slot_held: bool = False) -> None:
    """The plan as a job (the route starts one only for ``deep_dv``): a heavy ``calibration`` slot."""
    def body() -> None:
        _emit(job_id, f"Planning a calibration kit from {len(paths)} file(s)")
        doc = client.calibrate_plan(paths, players, deep_dv=deep_dv, cancel=lambda: jobs.is_cancelled(job_id))
        if doc is None:                                 # the client was cancelled and killed the process tree
            _end_cancelled(job_id)
            return
        _done(job_id, doc)

    _run_slotted(job_id, "calibration" if deep_dv else "score_rank", _slot_held, body)


def run_calibration_build_job(job_id: str, client: Any, paths: List[str], out: str, players: List[str],
                              tests: List[str], *, deep_dv: bool = False, _slot_held: bool = False) -> None:
    def body() -> None:
        _emit(job_id, f"Building a calibration kit ({len(tests) or 'all'} test(s))")
        doc = client.calibrate_build(paths, out, players, tests, deep_dv=deep_dv, on_line=_progress_forwarder(job_id),
                                     cancel=lambda: jobs.is_cancelled(job_id))
        if doc is None:
            _end_cancelled(job_id)
            return
        _done(job_id, doc)

    _run_slotted(job_id, "calibration", _slot_held, body)


def run_calibration_report_job(job_id: str, client: Any, kit: str, paths: List[str], *,
                               _slot_held: bool = False) -> None:
    def body() -> None:
        _emit(job_id, f"Reporting {len(paths)} file(s) for the calibration kit")
        doc = client.calibrate_report(kit, paths, on_line=_progress_forwarder(job_id),
                                      cancel=lambda: jobs.is_cancelled(job_id))
        if doc is None:
            _end_cancelled(job_id)
            return
        _done(job_id, doc)

    _run_slotted(job_id, "score_rank", _slot_held, body)


# ── routes ───────────────────────────────────────────────────────────────────────

@router.post("/plan")
def calibration_plan(body: PlanRequest, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    paths = _guarded_sources(body.paths)
    if body.deep_dv:
        return _start("calibration", "suite_calibration_plan", max(1, len(paths)), background_tasks,
                      run_calibration_plan_job, client, paths, body.players, deep_dv=True)
    return _sync(lambda: client.calibrate_plan(paths, body.players, deep_dv=False))


@router.get("/status")
def calibration_status(kit: Optional[str] = Query(None, pattern=KIT_QUERY_PATTERN),
                       client: Any = Depends(get_suite_client)):
    return _sync(lambda: client.calibrate_status(kit))


@router.post("/session")
def calibration_session(body: SessionRequest, client: Any = Depends(get_suite_client)):
    out = _guarded_output(body.out, "out", must_exist=True)
    return _sync(lambda: client.calibrate_session(out, body.tv_software, body.audio_output, body.usb_filesystem))


@router.post("/record")
def calibration_record(body: RecordRequest, client: Any = Depends(get_suite_client)):
    return _sync(lambda: client.calibrate_record(body.kit, body.test, body.player, body.outcome, body.note))


@router.post("/build")
def calibration_build(body: BuildRequest, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    paths = _guarded_sources(body.paths)
    out = _guarded_output(body.out, "out", must_exist=False)
    return _start("calibration", "suite_calibration_build", max(1, len(paths)), background_tasks,
                  run_calibration_build_job, client, paths, out, body.players, body.tests,
                  deep_dv=bool(body.deep_dv))


@router.post("/apply")
def calibration_apply(body: ApplyRequest, client: Any = Depends(get_suite_client)):
    return _sync(lambda: client.calibrate_apply(body.kit, preview=body.preview, revision=body.revision,
                                                confirm_veto=list(body.confirm_veto)),
                 after=None if body.preview else suite_routes.invalidate_devices_cache)


@router.post("/undo")
def calibration_undo(body: UndoRequest, client: Any = Depends(get_suite_client)):
    return _sync(lambda: client.calibrate_undo(body.kit), after=suite_routes.invalidate_devices_cache)


@router.post("/report")
def calibration_report(body: ReportRequest, background_tasks: BackgroundTasks,
                       client: Any = Depends(get_suite_client)):
    paths = _guarded_sources(body.paths)
    return _start("score_rank", "suite_calibration_report", len(paths), background_tasks,
                  run_calibration_report_job, client, body.kit, paths)
