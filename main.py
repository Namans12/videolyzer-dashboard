import errno
import re
import shutil
import os
import uuid
import logging
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import List
import time
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
import json

from analysis import analyze_file, scan_folder
from magnet import MagnetUnavailable, NoFreePort, port_allocator, run_magnet_job_threaded
from comparison import compare_releases, composite_score, enrich_release
from series_compare import is_series_comparison, build_series_comparison
from request_guard import RequestGuard
from jobs import TERMINAL, jobs
import suite_routes
from path_guard import (
    PathRejected, iter_safe_video_files, make_file_filter, validate_user_path,
)

logger = logging.getLogger("video-analyzer.api")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = 120 * 1024 * 1024 * 1024    # 120 GB
MIN_FREE_BYTES   = 20 * 1024 * 1024 * 1024  # 20 GB

# The job store (jobs.py): locked, deep-copy reads, honest terminal states
# (done / error / cancelled), retention of the last 20 finished jobs purged
# inside ``create`` (no timers). Kept under its old name for the tests and docs.
_jobs = jobs

# Parallel analyses per batch/folder job.
ANALYSIS_WORKERS = 4

# Marker returned by the pool worker for a file skipped because the job was cancelled.
_SKIPPED = object()


def _emit(job_id: str, msg: str) -> None:
    jobs.append_event(job_id, msg)


def _guarded(job_id: str, worker):
    """Wrap a per-file analyser so queued files are skipped once the job is cancelled."""
    def run(path):
        if jobs.is_cancelled(job_id):
            return _SKIPPED
        return worker(path)
    return run


def _end_job(job_id: str, done_msg: str, results: list | None = None) -> None:
    """Emit the last event, THEN end the job (the SSE stream stops at a terminal
    status, so an event added after it could be missed). A job whose cancel flag
    is set ends ``cancelled`` and keeps whatever partial results it has."""
    _emit(job_id, "Cancelled." if jobs.is_cancelled(job_id) else done_msg)
    jobs.finish(job_id, results)


def _fail_job(job_id: str, error: str, error_kind: str, msg: str | None = None) -> None:
    """Emit the failure event, then end the job as ``error`` (``cancelled`` if the
    cancel flag is set)."""
    _emit(job_id, "Cancelled." if jobs.is_cancelled(job_id) else (msg or f"Error: {error}"))
    jobs.fail(job_id, error, error_kind)


def _patch_result_path(result: dict, temp_path: str, orig_name: str) -> None:
    result["file"] = orig_name
    temp_base = os.path.basename(temp_path)
    if "path" in result and temp_base in result["path"]:
        result["path"] = orig_name.join(result["path"].rsplit(temp_base, 1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    for f in os.listdir(UPLOAD_DIR):
        try:
            os.remove(os.path.join(UPLOAD_DIR, f))
        except OSError:
            pass
    logger.info("Upload dir cleaned on startup.")
    yield


app = FastAPI(title="Video Metadata Analyzer", lifespan=lifespan)

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "POST":
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_UPLOAD_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request too large — max {MAX_UPLOAD_BYTES // (1024**3)} GB."}
                )
        return await call_next(request)

# add_middleware order: the LAST one added is the OUTERMOST. CORS must be outermost
# so preflights are answered before the guard and the guard's 4xx for an allowed
# origin still carry CORS headers.
app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(RequestGuard)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /suite/... (devices, score, rank, compare): the installed VideoSuite as guarded jobs.
suite_routes.install(app)


# ── Helpers ──────────────────────────────────────────────────────────────────

def check_disk_space() -> None:
    try:
        usage = shutil.disk_usage(UPLOAD_DIR)
        if usage.free < MIN_FREE_BYTES:
            free_gb = usage.free / 1_073_741_824
            raise HTTPException(
                status_code=507,
                detail=f"Server storage almost full ({free_gb:.1f} GB free). "
                       "Clean the uploads/ folder and retry.",
            )
    except HTTPException:
        raise
    except OSError:
        pass


def _run_batch_job(job_id: str, saved_paths: list[str],
                   path_name_map: dict[str, str], fast: bool) -> None:
    total  = len(saved_paths)
    done   = 0
    results: list = []
    worker = _guarded(job_id, partial(analyze_file, skip_dovi_scan=fast))

    _emit(job_id, f"Starting analysis of {total} file(s)…")
    jobs.update(job_id, progress=f"{done}/{total}")

    with ThreadPoolExecutor(max_workers=max(1, min(ANALYSIS_WORKERS, total))) as executor:
        future_map = {
            executor.submit(worker, path): (path, path_name_map[path])
            for path in saved_paths
        }
        for future in as_completed(future_map):
            temp_path, orig_name = future_map[future]
            skipped = False
            try:
                result = future.result()
                if result is _SKIPPED:
                    skipped = True          # cancelled before this file started
                else:
                    jobs.update(job_id, current=orig_name)
                    if result:
                        _patch_result_path(result, temp_path, orig_name)
                        results.append(result)
                        _emit(job_id, f"✓ {orig_name}")
                    else:
                        _emit(job_id, f"✗ {orig_name} — no data extracted")
            except Exception as exc:
                logger.warning("Analysis failed for %s: %s", orig_name, exc)
                _emit(job_id, f"✗ {orig_name} — {exc}")
            finally:
                if not skipped:
                    done += 1
                    jobs.update(job_id, progress=f"{done}/{total}")
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    if results or jobs.is_cancelled(job_id):
        _end_job(job_id, f"Done — {len(results)}/{total} analysed successfully.",
                 rank_results(results))
    else:
        _fail_job(job_id, "No files could be analyzed.", "analysis_failed",
                  "Error: no files could be analysed.")


def _run_single(job_id: str, path: str, name: str, fast: bool) -> None:
    try:
        if jobs.is_cancelled(job_id):
            _end_job(job_id, "")            # cancelled before it started
            return
        _emit(job_id, f"Analyzing {name}…")
        try:
            result = analyze_file(path, skip_dovi_scan=fast)
        except Exception as exc:
            logger.warning("Analysis failed for %s: %s", name, exc)
            _fail_job(job_id, str(exc), "analysis_failed")
            return
        if result:
            _patch_result_path(result, path, name)
            result["batch_rank"] = 1
            _end_job(job_id, f"✓ {name}", [result])
        else:
            # RB-10: analyze_file returns None on its timeout or when nothing could
            # be extracted; that is a failure, not an empty success.
            _fail_job(job_id, "analysis timed out or failed", "timeout",
                      f"✗ {name} — no data extracted")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _disk_name(name: str) -> str:
    """``name`` made safe as a Windows file name: no ``:`` (it would create an alternate data stream), none of the
    characters ``< > " / | ? *`` and backslash, no control characters; trailing dots and spaces removed (Windows
    ignores them)."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", name).strip(" .")
    return cleaned or "upload.bin"


def _is_user_cancel(exc: BaseException) -> bool:
    """magnet.py signals a user cancel with ``RuntimeError("Cancelled")``; that is not an error worth a traceback."""
    return isinstance(exc, RuntimeError) and str(exc) == "Cancelled"


def save_upload(file: UploadFile) -> tuple[str, str]:
    original_name = os.path.basename(file.filename or "upload.bin")        # what the user sees
    
    # For large files, tell user to use path input instead
    # (uvicorn rejects huge uploads before we can even check size)
    # This message shows for files where content-length header is present
    content_length = getattr(file, 'size', None)
    if content_length and content_length > 10 * 1024 * 1024 * 1024:  # 10 GB
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large to upload ({content_length // (1024**3):.0f} GB). "
                "Use the path input instead — paste the full file path or folder path "
                "directly into the text box. No upload needed for local files."
            )
        )
    unique_name   = f"{uuid.uuid4().hex}_{_disk_name(original_name)}"
    file_path     = os.path.join(UPLOAD_DIR, unique_name)
    bytes_written = 0
    chunk_size    = 1024 * 1024  # 1 MB
    try:
        with open(file_path, "wb") as buffer:
            while True:
                chunk = file.file.read(chunk_size)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    buffer.close()
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large — max {MAX_UPLOAD_BYTES // (1024*1024)} MB allowed.",
                    )
                if bytes_written % (50 * 1024 * 1024) == 0:
                    if shutil.disk_usage(UPLOAD_DIR).free < MIN_FREE_BYTES:
                        buffer.close()
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass
                        raise HTTPException(
                            status_code=507,
                            detail="Server ran out of disk space during upload.",
                        )
                buffer.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        try:
            os.remove(file_path)
        except OSError:
            pass
        if exc.errno == errno.ENOSPC:
            raise HTTPException(
                status_code=507,
                detail="Server disk is full. Free up space and retry.",
            )
        raise HTTPException(status_code=500, detail=f"Could not save upload: {exc}")
    finally:
        try:
            file.file.close()
        except Exception:
            pass
    return file_path, original_name


def rank_results(results: list) -> list:
    results.sort(
        key=lambda x: (
            x.get("tv_score", 0),
            x.get("score", 0),
            x.get("confidence_score", 0),
            x.get("bitrate_mbps", 0),
            x.get("file", ""),
        ),
        reverse=True,
    )
    for idx, item in enumerate(results, 1):
        item["batch_rank"] = idx
    return results


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health/")
def health_check():
    usage = shutil.disk_usage(UPLOAD_DIR)
    return {
        "status":       "ok",
        "disk_free_gb": round(usage.free  / 1_073_741_824, 2),
        "disk_used_gb": round(usage.used  / 1_073_741_824, 2),
        "upload_dir":   os.path.abspath(UPLOAD_DIR),
    }


@app.post("/analysis/")
async def analyze_video(background_tasks: BackgroundTasks, request: Request, fast: bool = Query(False)):
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise HTTPException(status_code=400,
                            detail="Upload must use multipart/form-data with a file field.")
    check_disk_space()

    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid multipart body: {exc}") from exc

    file = form.get("file") or form.get("upload") or form.get("video")
    if not isinstance(file, (UploadFile, StarletteUploadFile)):
        raise HTTPException(status_code=400, detail="No uploaded file found. Use field name 'file'.")
    if not file.filename:
        raise HTTPException(status_code=400, detail="Uploaded file has no filename.")

    temp_path, orig_name = save_upload(file)   # type: ignore[arg-type]

    job_id = jobs.create("single", progress="0/1", current=orig_name, total=1)

    background_tasks.add_task(_run_single, job_id, temp_path, orig_name, fast)
    return {"job_id": job_id, "total": 1}


@app.post("/analyze-multiple/")
async def analyze_multiple(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    fast: bool = Query(False),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    check_disk_space()

    saved_paths:   list[str]      = []
    path_name_map: dict[str, str] = {}

    for f in files:
        if not f.filename:
            continue
        temp_path, orig_name = save_upload(f)
        saved_paths.append(temp_path)
        path_name_map[temp_path] = orig_name

    if not saved_paths:
        raise HTTPException(status_code=400, detail="All uploaded files were rejected.")

    job_id = jobs.create("batch", progress=f"0/{len(saved_paths)}", total=len(saved_paths))
    background_tasks.add_task(_run_batch_job, job_id, saved_paths, path_name_map, fast)
    return {"job_id": job_id, "total": len(saved_paths)}


def _validated_path(raw: str, *, must_be: str) -> str:
    """Run the path guard and map a rejection to an HTTPException."""
    try:
        return str(validate_user_path(raw, must_be=must_be))
    except PathRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@app.get("/analyze-path/")
def analyze_video_path(background_tasks: BackgroundTasks, path: str, fast: bool = Query(False)):
    """Accept a server-local file or folder path and run analysis as a background job.
    Returns a `job_id` immediately and emits progress via the SSE `/job/{job_id}/events` endpoint.
    """
    path = _validated_path(path, must_be="any")

    job_id = jobs.create("path", progress="0/1", current="", total=1)

    background_tasks.add_task(_run_path_job, job_id, path, fast)

    return {"job_id": job_id}


def _run_path_job(job_id: str, path: str, fast: bool) -> None:
    _emit(job_id, "Job started")

    results = []
    if os.path.isdir(path):
        _emit(job_id, f"Scanning folder: {os.path.basename(path)}…")
        try:
            # os.walk follows junctions: every directory and file is re-validated
            # so a junction cannot lead outside the allowed roots or to a share.
            file_paths = list(iter_safe_video_files(path))
        except Exception as exc:
            logger.warning("Failed scanning folder %s — %s", path, exc)
            _fail_job(job_id, str(exc), "internal", f"Error scanning folder: {exc}")
            return

        if not file_paths:
            # RB-11: ThreadPoolExecutor(max_workers=0) raised ValueError and the job
            # used to end "done" with a confusing error text. Check BEFORE the pool.
            _fail_job(job_id, "no video files found", "no_files",
                      "Error: no video files found in this folder.")
            return

        jobs.update(job_id, total=len(file_paths), progress=f"0/{len(file_paths)}")
        worker = _guarded(job_id, partial(analyze_file, skip_dovi_scan=fast))
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, min(ANALYSIS_WORKERS, len(file_paths)))) as executor:
            future_map = {executor.submit(worker, p): p for p in file_paths}
            for future in as_completed(future_map):
                p = future_map[future]
                skipped = False
                try:
                    result = future.result()
                    if result is _SKIPPED:
                        skipped = True      # cancelled before this file started
                    else:
                        jobs.update(job_id, current=os.path.basename(p))
                        if result:
                            results.append(result)
                            _emit(job_id, f"✓ {os.path.basename(p)}")
                        else:
                            _emit(job_id, f"✗ {os.path.basename(p)} — no data")
                except Exception as exc:
                    logger.warning("scan_folder: failed on %s — %s", p, exc)
                    _emit(job_id, f"✗ {os.path.basename(p)} — {exc}")
                finally:
                    if not skipped:
                        done += 1
                        jobs.update(job_id, progress=f"{done}/{len(file_paths)}")

        if not results and not jobs.is_cancelled(job_id):
            _fail_job(job_id, "No files could be analyzed.", "analysis_failed",
                      "Error: no files could be analysed.")
            return
    else:
        base = os.path.basename(path)
        if jobs.is_cancelled(job_id):
            _end_job(job_id, "")
            return
        _emit(job_id, f"Analyzing {base}…")
        try:
            result = analyze_file(path, skip_dovi_scan=fast)
        except Exception as exc:
            logger.warning("Failed analyzing %s — %s", path, exc)
            _fail_job(job_id, str(exc), "analysis_failed")
            return
        if result:
            results.append(result)
            _emit(job_id, f"✓ {base}")
        else:
            _fail_job(job_id, "analysis timed out or failed", "timeout",
                      f"✗ {base} — no data extracted")
            return

    _end_job(job_id, "Done.", rank_results(results))


@app.post("/magnet/")
async def analyze_magnet(background_tasks: BackgroundTasks, request: Request,
                         fast: bool = Query(False),
                         hdr10plus: bool = Query(False)):
    """Accept a magnet URI, fetch its metadata + head/tail slices, classify
    each video file, and run ffprobe against the partial files.

    Returns a `job_id` immediately. Reuses the existing job poll/SSE endpoints
    for status streaming. Adds `magnet_files` (per-file verdicts) and
    `magnet_torrent` (name + info_hash) onto the job record alongside the
    usual `results` array.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    magnet_uri = (body or {}).get("magnet", "").strip()
    if not magnet_uri:
        raise HTTPException(status_code=400, detail="Missing `magnet` field in JSON body.")
    if not magnet_uri.lower().startswith("magnet:?"):
        raise HTTPException(status_code=400,
                            detail="Not a magnet URI — must start with 'magnet:?'.")

    job_id = jobs.create("magnet", progress="metadata", current="", total=0,
                         magnet_files=[], magnet_torrent=None)
    background_tasks.add_task(_run_magnet_job, job_id, magnet_uri, fast, hdr10plus)
    return {"job_id": job_id}


def _request_cancel_or_404(job_id: str) -> dict:
    if not jobs.request_cancel(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"ok": True}


@app.post("/job/{job_id}/cancel")
def cancel_job(job_id: str):
    """Ask any job to stop. The runner ends it as ``cancelled`` (partial results kept)."""
    return _request_cancel_or_404(job_id)


@app.post("/magnet/{job_id}/cancel")
def cancel_magnet(job_id: str):
    return _request_cancel_or_404(job_id)


def _magnet_error_kind(exc: BaseException) -> str:
    if isinstance(exc, MagnetUnavailable):
        return "unavailable"
    if isinstance(exc, NoFreePort):
        return "busy"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "internal"


def _run_magnet_job(job_id: str, magnet_uri: str, fast: bool,
                    enable_hdr10plus: bool = False) -> None:
    def emit(msg: str) -> None:
        _emit(job_id, msg)

    def cancel_check() -> bool:
        return jobs.is_cancelled(job_id)

    emit("Magnet job started")
    port: int | None = None
    try:
        # One process-wide pool: never share a listen port with another session.
        port = port_allocator.acquire()
        out = run_magnet_job_threaded(
            magnet_uri,
            skip_dovi_scan=fast,
            emit=emit,
            cancel_check=cancel_check,
            listen_port=port,
            enable_hdr10plus=enable_hdr10plus,
        )
        results = rank_results(out.get("analyses", []))
        # Enrich each analysis with filename intel + verification flags.
        # The torrent's display name is the most accurate source for filename intel;
        # individual file names are also reasonable.
        torrent_name = out.get("torrent_name") or ""
        enriched = [
            enrich_release(r, display_name=r.get("file") or torrent_name,
                            magnet_uri=magnet_uri)
            for r in results
        ]
        files = out.get("files", [])
        jobs.update(
            job_id,
            enriched=enriched,                  # new: with intel + verification + badges
            magnet_files=files,
            magnet_torrent={"name": torrent_name, "info_hash": out.get("info_hash")},
            total=len(files),
            progress=f"{len(results)}/{len(files)}",
        )
        _end_job(job_id, f"Done — {len(results)} playable / {len(files)} total file(s).",
                 results)                       # legacy field — still the raw analyses
    except MagnetUnavailable as exc:
        _fail_job(job_id, str(exc), _magnet_error_kind(exc))
    except TimeoutError as exc:
        _fail_job(job_id, str(exc), _magnet_error_kind(exc), f"Timed out: {exc}")
    except Exception as exc:
        if not _is_user_cancel(exc):
            logger.exception("Magnet job %s failed", job_id)
        _fail_job(job_id, str(exc), _magnet_error_kind(exc))
    finally:
        if port is not None:
            port_allocator.release(port)


# ── Multi-magnet comparison ──────────────────────────────────────────────────

# Concurrency for /compare-magnets/.
#   ≤2 magnets → run sequentially (single libtorrent session on 6881).
#   3-8 magnets → bounded parallel: max 2 concurrent sessions on adjacent ports.
# Rationale: libtorrent's DHT/peer handshake is the slow part. Running >2
# sessions saturates the consumer-grade DSL link and starves each torrent
# of incoming peer slots. Empirically 2 concurrent is the sweet spot.
COMPARE_MAX_MAGNETS    = 8
COMPARE_PARALLEL_LIMIT = 2
# Listen ports come from the process-wide ``magnet.port_allocator`` (shared with
# /magnet/), so compare slots never collide with single jobs or another compare.


@app.post("/compare-magnets/")
async def compare_magnets(background_tasks: BackgroundTasks, request: Request,
                           fast: bool = Query(True),
                           hdr10plus: bool = Query(False)):
    """Run multiple magnet jobs and produce a side-by-side comparison.

    Body: { "magnets": ["magnet:?…", "magnet:?…", …] }

    Returns: { "job_id": "...", "total": N }

    Poll /job/{job_id} for status. The completed job record will include:
      - per_magnet: [ { magnet, status, error, torrent, analyses }, ... ]
      - comparison: full output of comparison.compare_releases()
      - winner: short {winner_index, winner_name, reasons}
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc

    magnets = (body or {}).get("magnets") or []
    if not isinstance(magnets, list) or not magnets:
        raise HTTPException(status_code=400,
                            detail="Provide a non-empty `magnets` array in the JSON body.")
    if len(magnets) > COMPARE_MAX_MAGNETS:
        raise HTTPException(status_code=400,
                            detail=f"Up to {COMPARE_MAX_MAGNETS} magnets at a time.")
    cleaned: list[str] = []
    for i, m in enumerate(magnets):
        if not isinstance(m, str):
            raise HTTPException(status_code=400, detail=f"magnets[{i}] is not a string.")
        m = m.strip()
        if not m.lower().startswith("magnet:?"):
            raise HTTPException(status_code=400,
                                detail=f"magnets[{i}] is not a magnet URI.")
        cleaned.append(m)

    job_id = jobs.create(
        "compare", progress=f"0/{len(cleaned)}", current="", total=len(cleaned),
        per_magnet=[], comparison=None, winner=None, mode=None,
        episode_comparison=None, dropped=[],
    )
    background_tasks.add_task(_run_compare_job, job_id, cleaned, fast, hdr10plus)
    return {"job_id": job_id, "total": len(cleaned)}


@app.post("/compare-magnets/{job_id}/cancel")
def cancel_compare(job_id: str):
    return _request_cancel_or_404(job_id)


def _run_compare_job(job_id: str, magnets: list[str], fast: bool,
                     enable_hdr10plus: bool = False) -> None:
    def emit(msg: str) -> None:
        _emit(job_id, msg)

    def cancel_check() -> bool:
        return jobs.is_cancelled(job_id)

    use_parallel = len(magnets) > 2
    emit(f"Comparing {len(magnets)} magnet(s) — "
         f"{'parallel (limit ' + str(COMPARE_PARALLEL_LIMIT) + ')' if use_parallel else 'sequential'}.")

    per_magnet: list[dict] = [{
        "index":   i,
        "magnet":  m,
        "status":  "pending",
        "error":   None,
        "torrent": None,
        "files":   [],
        "analyses": [],
        "events":  [],
    } for i, m in enumerate(magnets)]
    # The store holds this same list: every change to a record goes through
    # ``update_rec`` (under the store lock) so a concurrent GET /job/{id} deep copy
    # never sees a half-written record. Reads below happen after the workers end.
    jobs.update(job_id, per_magnet=per_magnet)
    done_count = 0
    count_lock = threading.Lock()

    def update_rec(idx: int, fn) -> None:
        if not jobs.mutate(job_id, lambda _job: fn(per_magnet[idx])):
            fn(per_magnet[idx])

    def _run_one(idx: int, magnet_uri: str) -> None:
        nonlocal done_count
        if cancel_check():
            return                      # queued behind a cancel: never starts, stays "pending"
        update_rec(idx, lambda rec: rec.update(status="running"))
        port: int | None = None

        def emit_one(msg: str) -> None:
            ev = {"msg": msg, "ts": time.time()}
            update_rec(idx, lambda rec: rec["events"].append(ev))
            # Aggregate to the top-level event stream with a magnet prefix.
            emit(f"[#{idx + 1}] {msg}")

        try:
            port = port_allocator.acquire()
            out = run_magnet_job_threaded(
                magnet_uri,
                skip_dovi_scan=fast,
                emit=emit_one,
                cancel_check=cancel_check,
                listen_port=port,
                enable_hdr10plus=enable_hdr10plus,
            )
            update_rec(idx, lambda rec: rec.update(
                torrent={"name": out.get("torrent_name"),
                         "info_hash": out.get("info_hash")},
                files=out.get("files", []),
                analyses=out.get("analyses", []),
                status="done",
            ))
        except MagnetUnavailable as exc:
            update_rec(idx, lambda rec: rec.update(status="error", error=str(exc)))
            emit_one(f"Error: {exc}")
        except TimeoutError as exc:
            update_rec(idx, lambda rec: rec.update(status="error", error=str(exc)))
            emit_one(f"Timed out: {exc}")
        except Exception as exc:  # noqa: BLE001
            if not _is_user_cancel(exc):
                logger.exception("compare-magnets job %s magnet %d failed", job_id, idx)
            update_rec(idx, lambda rec: rec.update(status="error", error=str(exc)))
            emit_one(f"Error: {exc}")
        finally:
            if port is not None:
                port_allocator.release(port)
            # `done_count += 1` from two pool threads can lose an update: lock it.
            with count_lock:
                done_count += 1
                jobs.update(job_id, progress=f"{done_count}/{len(magnets)}")

    try:
        if use_parallel:
            # Bounded parallel — each session on its own port.
            with ThreadPoolExecutor(max_workers=COMPARE_PARALLEL_LIMIT) as ex:
                futures = [
                    ex.submit(_run_one, i, m)
                    for i, m in enumerate(magnets)
                ]
                for _ in as_completed(futures):
                    if cancel_check():
                        emit("Cancelled — stopping remaining magnet jobs.")
                        break
        else:
            for i, m in enumerate(magnets):
                if cancel_check():
                    emit("Cancelled — stopping remaining magnet jobs.")
                    break
                _run_one(i, m)

        if cancel_check():
            # A cancelled job keeps its partial per_magnet records and builds no
            # comparison.
            _end_job(job_id, "")
            return

        # ── Build comparison from whichever magnets produced analyses ───────
        # Two torrents that are episodic season packs get a per-episode
        # comparison; everything else (movies, single files) uses the
        # representative-file path. We ALWAYS build the representative-file
        # `comparison` too so the overview matrix renders in both modes.
        series_mode = is_series_comparison(per_magnet)
        jobs.update(job_id, mode="series" if series_mode else "movie")

        # Surface magnets that produced NO analysis (timed out / no peers /
        # unparseable) so the UI can show "N of M analyzed" instead of silently
        # dropping them. analyze_file returns None on its 6-min timeout — common
        # for huge MKV REMUX with deep HDR10+ on — and those used to just vanish.
        dropped = [
            {
                "index":  rec["index"],
                "magnet": rec.get("magnet"),
                "name":   (rec.get("torrent") or {}).get("name"),
                "reason": rec.get("error") or (
                    "metadata received but no file could be analyzed in time "
                    "(large REMUX + deep scan can exceed the per-file limit)"
                ),
            }
            for rec in per_magnet
            if rec.get("status") != "done" or not rec.get("analyses")
        ]
        jobs.update(job_id, dropped=dropped)
        if dropped:
            emit(f"⚠ {len(dropped)} of {len(per_magnet)} magnet(s) produced no "
                 f"analyzable file — see 'dropped' in the result.")

        enriched_releases: list[dict] = []
        for rec in per_magnet:
            if rec["status"] != "done" or not rec["analyses"]:
                continue
            # Pick the best playable file per torrent as that torrent's representative.
            # If multiple, pick the largest video file (most likely the feature).
            analyses = sorted(rec["analyses"],
                              key=lambda a: a.get("file_size_gb", 0) or 0,
                              reverse=True)
            best = analyses[0]
            display = rec["torrent"]["name"] if rec["torrent"] else best.get("file", "")
            enriched_releases.append(enrich_release(
                best, display_name=display, magnet_uri=rec["magnet"],
            ))

        if not enriched_releases:
            _fail_job(job_id, "No magnet produced analyzable files.", "no_analyses",
                      "Done — no comparable releases.")
            return

        # ONE ranking everywhere: order releases by pick_winner's EXACT key
        # (tv_score, composite, trust, bitrate) so the matrix columns, winner
        # banner, and frontend leaderboard all agree — including under a
        # tv_score tie, where a different tiebreak here used to let the banner
        # highlight a different column than the leaderboard's #1. This is the
        # Bravia-first rule — a P7 REMUX with a huge composite must not outrank
        # an ideal P8.1 that the TV plays natively.
        enriched_releases.sort(
            key=lambda r: (
                int(r.get("analysis", {}).get("tv_score") or 0),
                composite_score(r)[0],
                int(r.get("verification", {}).get("trust_score") or 0),
                float(r.get("analysis", {}).get("bitrate_mbps") or 0),
            ),
            reverse=True,
        )

        comparison = compare_releases(enriched_releases)
        # Top-level winner stays aligned with the representative-file matrix
        # columns (the season winner lives in episode_comparison.summary).
        jobs.update(job_id, comparison=comparison, winner=comparison["winner"])

        if series_mode:
            emit("Series detected — matching episodes across torrents…")
            episode_comparison = build_series_comparison(per_magnet)
            jobs.update(job_id, episode_comparison=episode_comparison)
            s = episode_comparison["summary"]
            emit(f"Series comparison built — {s['total_episodes']} episode(s), "
                 f"{s['contested_count']} head-to-head.")
            if s["winner_name"]:
                emit(f"Season winner: {s['winner_name']}")
        else:
            winner = comparison["winner"]
            if winner["winner_index"] is not None:
                emit(f"Winner: {winner['winner_name']} "
                     f"({comparison['releases'][winner['winner_index']]['composite_score']}/100)")

        _end_job(job_id, "Done — comparison built.")
    except Exception as exc:
        logger.exception("compare-magnets job %s failed", job_id)
        _fail_job(job_id, str(exc), "internal")


@app.get("/scan-folder/")
def scan_folder_api(path: str, fast: bool = Query(False)):
    path = _validated_path(path, must_be="dir")
    # scan_folder's os.walk follows junctions: every walked file is re-validated
    # (allowed roots, UNC and network-drive checks), roots configured or not.
    results = scan_folder(path, skip_dovi_scan=fast, file_filter=make_file_filter())
    if not results:
        raise HTTPException(status_code=404, detail="No supported video files found in this folder.")
    return results


@app.get("/job/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/job/{job_id}/events")
async def stream_job_events(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_gen():
        seen = 0
        while True:
            # Re-read the store every iteration: it hands out copies, so a reference
            # taken once would never see new events. The status is read together
            # with the events; runners emit their last event before ending the job.
            snap = jobs.events_and_status(job_id, seen)
            if snap is None:               # purged (or cleared) while streaming
                yield "data: __done__\n\n"
                return
            events, status = snap
            for ev in events:
                yield f"data: {json.dumps(ev)}\n\n"
            seen += len(events)
            if status in TERMINAL:
                yield "data: __done__\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
