"""Packaged same-origin policy and a bounded built-frontend surface.

The packaged entry binds its socket once and passes that exact socket to uvicorn.
Importing this module creates no listener or app data.
"""
from __future__ import annotations

import errno
import hmac
import json
import os
import re
import socket
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).absolute().parent))

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse

from request_guard import RequestGuard
from app_preflight import preflight

SPA_PATHS = ("/", "/analyze", "/suite", "/calibration")
RESERVED_API_PREFIXES = (
    "/app", "/suite", "/comparison", "/job", "/health", "/analysis",
    "/analyze-path", "/analyze-multiple", "/scan-folder", "/magnet", "/compare-magnets",
    "/docs", "/redoc", "/openapi.json",
)


def configure_origin(app: FastAPI, origin: str, *, development: bool = False) -> None:
    """Replace policy before startup; production trusts one bound IPv4 origin."""
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid app origin") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or origin != f"http://127.0.0.1:{port}"):
        raise ValueError("app origin must be the bound http://127.0.0.1:port")
    if app.middleware_stack is not None:
        raise RuntimeError("configure origin before app startup")
    others = [m for m in app.user_middleware if m.cls not in (CORSMiddleware, RequestGuard)]
    cors = dict(allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    if development:
        cors["allow_origin_regex"] = r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
    else:
        cors["allow_origins"] = [origin]
    guard = Middleware(RequestGuard, origin=origin, development=development)
    cors_layer = Middleware(CORSMiddleware, **cors)
    # Production validates Host even on CORS preflights. Development keeps CORS
    # outermost so guard errors can be read by the separately hosted Vite UI.
    policy = [cors_layer, guard] if development else [guard, cors_layer]
    app.user_middleware = [*policy, *others]


def install_static(app: FastAPI, dist: Path) -> None:
    """Serve only index navigation and /assets; no catch-all API fallback."""
    dist = dist.resolve(strict=True)
    index = dist / "index.html"
    if not index.is_file():
        raise ValueError("built frontend index.html is missing")

    def safe_file(relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        if (not parts or "\\" in relative or ":" in relative
                or any(p.startswith(".") for p in parts)):
            raise HTTPException(404)
        candidate = dist.joinpath(*parts)
        # Reject symlinks/junctions at every level, even if they point inside dist.
        for path in (candidate, *candidate.parents):
            if path == dist:
                break
            if path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise HTTPException(404)
        if not candidate.resolve().is_relative_to(dist) or not candidate.is_file():
            raise HTTPException(404)
        return candidate

    async def navigation():
        return FileResponse(safe_file("index.html"), headers={"Cache-Control": "no-cache"})

    async def favicon():
        try:
            return FileResponse(safe_file("favicon.svg"))
        except OSError:
            raise HTTPException(404) from None

    async def asset(asset_path: str):
        try:
            path = safe_file("assets/" + asset_path)
        except OSError:
            raise HTTPException(404) from None
        return FileResponse(path)

    for path in SPA_PATHS:
        app.add_api_route(path, navigation, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/favicon.svg", favicon, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route("/assets/{asset_path:path}", asset, methods=["GET", "HEAD"], include_in_schema=False)


INSTANCE_FIELDS = frozenset(("install_id", "build_id", "session_id", "pid", "process_created",
                             "origin", "pipe_name", "control_secret"))


def parse_instance_record(value, *, expected=None):
    """Independent wire parser; never trust a TCP health response as readiness."""
    if not isinstance(value, dict) or set(value) != INSTANCE_FIELDS:
        raise ValueError("invalid instance fields")
    for key in ("install_id", "build_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value[key]):
            raise ValueError("invalid instance identity")
    for key, size in (("session_id", 32), ("control_secret", 64)):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{%d}" % size, value[key]):
            raise ValueError("invalid instance credential")
    if type(value["pid"]) is not int or not 0 < value["pid"] <= 0xFFFFFFFF:
        raise ValueError("invalid instance PID")
    if (not isinstance(value["process_created"], str)
            or not re.fullmatch(r"[0-9]{1,20}", value["process_created"])
            or int(value["process_created"]) <= 0):
        raise ValueError("invalid process creation identity")
    if not isinstance(value["pipe_name"], str) or not re.fullmatch(
            r"\\\\\.\\pipe\\VideoSuite-[A-Za-z0-9_.-]{1,180}", value["pipe_name"]):
        raise ValueError("invalid instance pipe")
    if not isinstance(value["origin"], str):
        raise ValueError("invalid instance origin")
    try:
        parsed = urlsplit(value["origin"])
        port = parsed.port
        valid = parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and 0 < port < 65536
    except (ValueError, TypeError):
        valid = False
    if not valid or value["origin"] != f"http://127.0.0.1:{port}":
        raise ValueError("invalid instance origin")
    if expected is not None:
        if any(key not in value or type(value[key]) is not type(item)
               or not hmac.compare_digest(str(value[key]), str(item)) for key, item in expected.items()):
            raise ValueError("instance identity mismatch")
    return dict(value)


def bind_loopback(preferred_port: int = 8000) -> socket.socket:
    """Prefer 8000; fallback is bind(0), never a probe followed by a rebind."""
    if type(preferred_port) is not int or not 0 <= preferred_port <= 65535:
        raise ValueError("invalid preferred port")
    for port in ([preferred_port, 0] if preferred_port else [0]):
        bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            bound.bind(("127.0.0.1", port))
            bound.listen(128)
            bound.set_inheritable(False)
            return bound
        except OSError as exc:
            bound.close()
            if port == 0 or exc.errno not in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
                raise
    raise OSError("could not bind loopback")


def install_identity(app: FastAPI, record, checks):
    record = parse_instance_record(record)
    app.state.app_identity = record
    app.state.app_checks = list(checks)
    app.state.app_ready = False
    app.state.quit_pending = False

    async def status():
        from jobs import jobs, TERMINAL
        active = []
        for job_id in jobs.ids():
            job = jobs.get(job_id)
            if job and job.get("status") not in TERMINAL:
                active.append(dict(job_id=job_id, kind=job.get("kind", "unknown"),
                                   status=job.get("status"), current=job.get("current")))
        return JSONResponse(dict(schema="videosuite-app-status/1",
            **{key: record[key] for key in ("build_id", "install_id", "session_id", "origin")},
            ready=app.state.app_ready, checks=app.state.app_checks, active_jobs=active,
            quit_pending=app.state.quit_pending), headers={"Cache-Control": "no-store"})

    async def bootstrap(request: Request):
        if (request.headers.get("origin") != record["origin"]
                or request.headers.get("sec-fetch-site") != "same-origin"
                or request.headers.get("sec-fetch-mode") not in ("cors", "same-origin")
                or request.headers.get("sec-fetch-dest") != "empty"):
            raise HTTPException(403, "same-origin browser request required")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "JSON required")
        body = await request.body()
        try:
            if len(body) > 1024 or json.loads(body) != {}:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "empty JSON object required") from None
        return JSONResponse(dict(token=record["control_secret"], session_id=record["session_id"]),
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    app.add_api_route("/app/status", status, methods=["GET"], include_in_schema=False)
    app.add_api_route("/app/bootstrap", bootstrap, methods=["POST"], include_in_schema=False)


def run_bound(app, bound, on_ready):
    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started:
                on_ready()
    server = ReadyServer(uvicorn.Config(app, log_level="info", access_log=False,
                                       timeout_graceful_shutdown=3))
    app.state.app_server = server
    server.run(sockets=[bound])


def serve(install_root: Path, release: dict, ready_pipe: int) -> int:
    """Receive credentials over an inherited private handle, never argv/env/URL."""
    from multiprocessing.connection import PipeConnection
    import proc_guard
    channel = PipeConnection(ready_pipe)
    bound = None
    try:
        # CLI receives before importing main; direct callers pass the same
        # envelope's identity in this private in-memory member.
        identity = release["_identity"]
        if Path(release["install_root"]).absolute() != Path(install_root).absolute():
            raise ValueError("install identity mismatch")
        for name in ("suite_root", "suite_python", "dashboard_root", "dashboard_python", "release_root"):
            if not Path(release[name]).is_absolute():
                raise ValueError("absolute release paths required")
        os.environ["VIDEOSUITE_PYTHON"] = release["suite_python"]
        os.environ["VIDEOSUITE_ROOT"] = release["suite_root"]
        manifest = release.get("manifest", {})
        data_root = os.environ.get("VIDEOLYZER_DATA_ROOT") or str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VideoComparator/app")
        checks = preflight(Path(release["release_root"]), manifest,
                           Path(data_root))
        tool_dirs = []
        for row in manifest.get("tools", []):
            path = Path(release["release_root"]) / row["path"]
            directory = str(path.parent)
            if directory not in tool_dirs:
                tool_dirs.append(directory)
        os.environ["PATH"] = os.pathsep.join([*tool_dirs, os.environ.get("PATH", "")])
        import main
        import suite_version
        from suite_client import get_suite_client
        bound = bind_loopback()
        host, port = bound.getsockname()
        record = parse_instance_record(dict(identity, pid=os.getpid(),
            process_created=proc_guard.process_created(os.getpid()), origin=f"http://{host}:{port}"))
        configure_origin(main.app, record["origin"])
        install_static(main.app, Path(release["dashboard_root"]) / "dist")
        check = suite_version.ensure(suite_version._discover(get_suite_client), refresh=True)
        checks.append(dict(id="suite_handshake", status="ok" if check["status"] == "ok" else "error",
                           message=check["message"], capability="suite"))
        install_identity(main.app, record, checks)
        def ready():
            main.app.state.app_ready = True
            channel.send_bytes(json.dumps(dict(record=record, checks=checks)).encode("utf-8"))
        run_bound(main.app, bound, ready)
        return 0
    finally:
        if bound is not None: bound.close()
        channel.close()


def _main():
    import argparse
    import sys
    from multiprocessing.connection import PipeConnection
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-handle", type=int, required=True)
    args = parser.parse_args()
    os.set_handle_inheritable(args.ready_handle, False)
    channel = PipeConnection(args.ready_handle)
    try:
        if not channel.poll(30):
            raise TimeoutError("launcher bootstrap timed out")
        envelope = json.loads(channel.recv_bytes(20 * 1024 * 1024).decode("utf-8"))
        release = envelope["release"]
        release["_identity"] = envelope["identity"]
        # Ownership of the handle transfers to serve; detach without closing it.
        channel._handle = None
        return serve(Path(release["install_root"]), release, args.ready_handle)
    except Exception:
        # No traceback may reveal credential-bearing local values.
        print("Packaged host startup failed; see launcher log.", file=sys.stderr)
        return 1
    finally:
        channel.close()


if __name__ == "__main__":
    import sys
    sys.modules["app_host"] = sys.modules[__name__]
    raise SystemExit(_main())
