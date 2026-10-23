"""``/comparison/...``: the unified one-screen run API (Plan 4B U5).

* ``POST /comparison/plans`` (a ``RunRequest``) -> ``{job_id, run_id}``: a planning job (facts once, frozen basis);
  the run then waits in ``awaiting_confirmation``. Holds one light score/rank slot only while planning.
* ``POST /comparison/runs {run_id, plan_revision}`` -> ``{job_id, run_id}``: atomically confirms that immutable plan
  once (stale revision, second confirmation, changed files or changed settings basis = 409) and starts the chain
  under ONE lease: Full = the heavy comparison slot for compare AND rank; Quick = one score/rank slot.
* ``GET /comparison/runs`` -> ``{runs: [PublicRunSnapshot...]}`` (newest first, active and recently finished).
* ``GET /comparison/runs/{run_id}`` -> ``{run: PublicRunSnapshot, result: RunResult|null}``.
* ``POST /comparison/runs/{run_id}/cancel {}`` cancels a pending plan (a running chain is also cancelled through
  its job; ``POST /job/{job_id}/cancel`` works too).

Every response carries public snapshots only (never ``reports_dir``). Validation errors are 422 without an echo of
the submitted values. Magnet runs arrive with U6 (501 until then).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import comparison_runs
import suite_routes
import comparison_reports
import native_chooser
from comparison_contracts import ContractError, to_public_snapshot, validate_run_request
from jobs import jobs
from suite_client import get_suite_client

router = APIRouter(prefix="/comparison", tags=["comparison"])

MAX_BODY_BYTES = 1024 * 1024
_RUN_ID_RE = re.compile(r"run-[A-Za-z0-9_-]{1,124}")


def _guard_paths(paths: List[str]) -> List[str]:
    """The /suite path guard (video files that exist; ADS/UNC/control characters refused). Test seam."""
    return native_chooser.guarded_paths(paths)


async def _json_object(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request too large.")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="JSON object required") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    return body


def _run_or_404(run_id: Any) -> Dict[str, Any]:
    run = comparison_runs.store.get(run_id) if isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id) else None
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.post("/plans")
async def create_plan(request: Request, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    raw = await _json_object(request)
    try:
        run_request = validate_run_request(raw)
    except ContractError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    if run_request["input_kind"] == "files":
        run_request["paths"] = _guard_paths(run_request["paths"])
    lease = suite_routes.acquire_lease("score_rank")
    try:
        run_id = comparison_runs.store.create(run_request)
        job_id = jobs.create("comparison_plan", progress="0/1", current="", total=len(run_request.get("paths", run_request.get("magnets", []))),
                             run_id=run_id)
        comparison_runs.store.update(run_id, job_id=job_id)
        background_tasks.add_task(comparison_runs.plan_run, run_id, client, lease=lease)
    except BaseException:
        lease.release()
        raise
    return {"job_id": job_id, "run_id": run_id}


@router.post("/runs")
async def confirm_run(request: Request, background_tasks: BackgroundTasks, client: Any = Depends(get_suite_client)):
    body = await _json_object(request)
    if set(body) != {"run_id", "plan_revision"} or not all(isinstance(v, str) for v in body.values()):
        return JSONResponse(status_code=422, content={"detail": "Expected run_id and plan_revision."})
    run = _run_or_404(body["run_id"])
    run_id = run["run_id"]
    slot = "score_rank" if run["request"]["depth"] == "quick" else "compare"
    if run["stage"] != "awaiting_confirmation" or body["plan_revision"] != run["plan_revision"]:
        raise HTTPException(status_code=409, detail="This plan is not waiting for this confirmation.")
    lease = suite_routes.acquire_lease(slot)
    try:
        try:
            comparison_runs.store.confirm(run_id, body["plan_revision"])
        except comparison_runs.RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            await run_in_threadpool(comparison_runs.verify_current, run_id, client)
        except comparison_runs.RunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            job_id = jobs.create("comparison_run", progress="0/1", current="",
                                 total=len(run["candidates"]), run_id=run_id)
        except BaseException:
            comparison_runs.store.unconfirm(run_id)
            raise
        comparison_runs.store.update(run_id, job_id=job_id)
        background_tasks.add_task(comparison_runs.execute_run, run_id, client, lease=lease)
    except BaseException:
        lease.release()
        raise
    return {"job_id": job_id, "run_id": run_id}


@router.get("/runs")
def list_runs() -> Dict[str, Any]:
    return {"runs": [to_public_snapshot(run) for run in comparison_runs.store.list_all()]}


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> Dict[str, Any]:
    run = _run_or_404(run_id)
    return {"run": to_public_snapshot(run), "result": comparison_runs.store.result(run["run_id"])}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> Dict[str, Any]:
    if await _json_object(request) != {}:
        raise HTTPException(status_code=400, detail="empty JSON object required")
    run = _run_or_404(run_id)
    stage = comparison_runs.store.cancel_pending(run["run_id"])
    if stage not in comparison_runs.FINAL_STAGES and run["job_id"]:
        jobs.request_cancel(run["job_id"])                  # planning or running: its job stops at the next boundary
    return {"run_id": run["run_id"], "stage": comparison_runs.store.get(run["run_id"])["stage"]}


def install(app: FastAPI) -> None:
    app.include_router(router)


@router.get('/runs/{run_id}/reports/{report_id}')
def run_report(run_id: str, report_id: str, request: Request):
    _run_or_404(run_id)
    query = request.query_params
    if (set(query) - {'download'} or len(query.getlist('download')) > 1
            or query.get('download', '0') not in ('0', '1')):
        raise HTTPException(422, 'Only download=0 or download=1 is accepted.')
    return comparison_reports.serve_report(run_id, report_id, download=query.get('download') == '1')
