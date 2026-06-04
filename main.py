import errno
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

logger = logging.getLogger("video-analyzer.api")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_UPLOAD_BYTES = 120 * 1024 * 1024 * 1024    # 120 GB
MIN_FREE_BYTES   = 20 * 1024 * 1024 * 1024  # 20 GB

_jobs: dict[str, dict] = {}


def _cleanup_old_jobs() -> None:
    """Keep only the 20 most recent completed jobs."""
    done = [jid for jid, job in _jobs.items() if job["status"] in {"done", "error"}]
    for jid in done[:-20]:
        _jobs.pop(jid, None)

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

app.add_middleware(MaxBodySizeMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    job    = _jobs[job_id]
    total  = len(saved_paths)
    done   = 0
    results: list = []
    worker = partial(analyze_file, skip_dovi_scan=fast)

    def emit(msg: str) -> None:
        job["events"].append({"msg": msg, "ts": time.time()})
        job["progress"] = f"{done}/{total}"

    emit(f"Starting analysis of {total} file(s)…")

    with ThreadPoolExecutor(max_workers=min(4, total)) as executor:
        future_map = {
            executor.submit(worker, path): (path, path_name_map[path])
            for path in saved_paths
        }
        for future in as_completed(future_map):
            temp_path, orig_name = future_map[future]
            job["current"] = orig_name
            try:
                result = future.result()
                if result:
                    _patch_result_path(result, temp_path, orig_name)
                    results.append(result)
                    emit(f"✓ {orig_name}")
                else:
                    emit(f"✗ {orig_name} — no data extracted")
            except Exception as exc:
                logger.warning("Analysis failed for %s: %s", orig_name, exc)
                emit(f"✗ {orig_name} — {exc}")
            finally:
                done += 1
                job["progress"] = f"{done}/{total}"
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    if results:
        job["results"] = rank_results(results)
        job["status"]  = "done"
        emit(f"Done — {len(results)}/{total} analysed successfully.")
    else:
        job["status"] = "error"
        job["error"]  = "No files could be analyzed."
        emit("Error: no files could be analysed.")
    threading.Timer(30.0, _cleanup_old_jobs).start()


def save_upload(file: UploadFile) -> tuple[str, str]:
    original_name = os.path.basename(file.filename or "upload.bin")
    
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
    unique_name   = f"{uuid.uuid4().hex}_{original_name}"
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

    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running", "progress": "0/1",
        "current": orig_name, "total": 1,
        "results": [], "error": None, "events": [],
    }

    def _run_single(jid: str, path: str, name: str, f: bool) -> None:
        job = _jobs[jid]
        job["events"].append({"msg": f"Analyzing {name}…", "ts": time.time()})
        try:
            result = analyze_file(path, skip_dovi_scan=f)
            if result:
                _patch_result_path(result, path, name)
                result["batch_rank"] = 1
                job["results"] = [result]
            job["status"] = "done"
            job["events"].append({"msg": f"✓ {name}", "ts": time.time()})
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            threading.Timer(30.0, _cleanup_old_jobs).start()

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

    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running", "progress": f"0/{len(saved_paths)}",
        "current": "", "total": len(saved_paths),
        "results": [], "error": None, "events": [],
    }
    background_tasks.add_task(_run_batch_job, job_id, saved_paths, path_name_map, fast)
    return {"job_id": job_id, "total": len(saved_paths)}


@app.get("/analyze-path/")
def analyze_video_path(background_tasks: BackgroundTasks, path: str, fast: bool = Query(False)):
    """Accept a server-local file or folder path and run analysis as a background job.
    Returns a `job_id` immediately and emits progress via the existing SSE `/progress/{job_id}` endpoint.
    """
    path = path.strip().strip('"')
    if not (os.path.isfile(path) or os.path.isdir(path)):
        raise HTTPException(status_code=400, detail="File or folder does not exist.")

    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "status": "running",
        "progress": "0/1",
        "current": "",
        "total": 1,
        "results": [],
        "error": None,
        "events": [],
    }

    background_tasks.add_task(_run_path_job, job_id, path, fast)

    return {"job_id": job_id}


def _run_path_job(job_id: str, path: str, fast: bool) -> None:
    job = _jobs[job_id]

    def emit(msg: str) -> None:
        job["events"].append({"msg": msg, "ts": time.time()})

    emit("Job started")

    results = []
    if os.path.isdir(path):
        emit(f"Scanning folder: {os.path.basename(path)}…")
        try:
            file_paths = []
            for root, _, files in os.walk(path):
                for f in files:
                    if f.lower().endswith((".mkv", ".mp4", ".ts", ".m2ts", ".hevc", ".h265")):
                        file_paths.append(os.path.join(root, f))
            
            job["total"] = len(file_paths)
            worker = partial(analyze_file, skip_dovi_scan=fast)
            
            with ThreadPoolExecutor(max_workers=min(4, len(file_paths))) as executor:
                future_map = {executor.submit(worker, p): p for p in file_paths}
                done = 0
                for future in as_completed(future_map):
                    p = future_map[future]
                    job["current"] = os.path.basename(p)
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                            emit(f"✓ {os.path.basename(p)}")
                        else:
                            emit(f"✗ {os.path.basename(p)} — no data")
                    except Exception as exc:
                        logger.warning("scan_folder: failed on %s — %s", p, exc)
                        emit(f"✗ {os.path.basename(p)} — {exc}")
                    finally:
                        done += 1
                        job["progress"] = f"{done}/{len(file_paths)}"
        except Exception as exc:
            logger.warning("Failed scanning folder %s — %s", path, exc)
            job["error"] = str(exc)
            emit(f"Error scanning folder: {exc}")
    else:
        base = os.path.basename(path)
        emit(f"Analyzing {base}…")
        try:
            result = analyze_file(path, skip_dovi_scan=fast)
            if result:
                results.append(result)
                emit(f"✓ {base}")
            else:
                emit(f"✗ {base} — no data extracted")
        except Exception as exc:
            logger.warning("Failed analyzing %s — %s", path, exc)
            job["error"] = str(exc)
            emit(f"Error: {exc}")

    job["results"] = rank_results(results)
    job["status"]  = "done"
    emit("Done.")
    threading.Timer(30.0, _cleanup_old_jobs).start()


@app.get("/scan-folder/")
def scan_folder_api(path: str, fast: bool = Query(False)):
    path = path.strip().strip('"')
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Folder does not exist.")
    results = scan_folder(path, skip_dovi_scan=fast)
    if not results:
        raise HTTPException(status_code=404, detail="No supported video files found in this folder.")
    return results


@app.get("/job/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/job/{job_id}/events")
async def stream_job_events(job_id: str):
    import asyncio
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_gen():
        seen = 0
        while True:
            events = job.get("events", [])
            while seen < len(events):
                yield f"data: {json.dumps(events[seen])}\n\n"
                seen += 1
            if job["status"] in {"done", "error"}:
                yield "data: __done__\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})