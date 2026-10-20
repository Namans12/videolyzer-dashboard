"""Privileged app controls reuse L3's in-memory, per-launch identity."""
import asyncio
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from app_lifecycle import AppLifecycle


def authorize_control(request: Request) -> None:
    identity = getattr(request.app.state, "app_identity", None)
    if not identity:
        raise HTTPException(503, "App controls require the packaged launcher.")
    if (request.headers.get("origin") != identity["origin"]
            or not hmac.compare_digest(request.headers.get("X-VideoSuite-Token", "").encode(),
                                       identity["control_secret"].encode())):
        raise HTTPException(403, "App control authorization required.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON required")


async def control_body(request):
    authorize_control(request)
    raw = await request.body()
    try:
        if len(raw) > 1024:
            raise ValueError()
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except (ValueError, UnicodeError):
        raise HTTPException(400, "JSON object required") from None


def install_app_routes(app: FastAPI, lifecycle: AppLifecycle) -> None:
    app.state.app_lifecycle = lifecycle

    async def finish_quit():
        # BackgroundTask runs only after the final HTTP response body is sent.
        if lifecycle.mode == "stop":
            clean = await asyncio.to_thread(lifecycle.drain)
        else:
            clean = lifecycle.ready_to_quit()
        if clean and lifecycle.mode != "keep":
            # Seal worker creation before uvicorn begins its graceful exit.
            # This also makes a late Keep request fail honestly once committed.
            lifecycle.request_quit("stop")
            server = getattr(app.state, "app_server", None)
            if server:
                server.should_exit = True
        if lifecycle.error:
            checks = getattr(app.state, "app_checks", [])
            checks[:] = [c for c in checks if c.get("id") != "shutdown"]
            checks.append(dict(id="shutdown", status="error", message=lifecycle.error, capability="quit"))

    async def watch_finished():
        try:
            while lifecycle.mode == "when_finished":
                if lifecycle.ready_to_quit():
                    await finish_quit()
                    return
                await asyncio.sleep(.2)
        except asyncio.CancelledError:
            pass

    async def after_response():
        await finish_quit()
        if lifecycle.mode == "when_finished" and not lifecycle.ready_to_quit():
            task = getattr(app.state, "quit_watcher", None)
            if task is None or task.done():
                app.state.quit_watcher = asyncio.create_task(watch_finished())

    async def quit_app(request: Request):
        body = await control_body(request)
        if set(body) != {"mode"} or body["mode"] not in ("keep", "when_finished", "stop"):
            raise HTTPException(400, "Expected mode keep, when_finished or stop")
        result = lifecycle.request_quit(body["mode"])
        app.state.quit_pending = result["quit_pending"]
        return JSONResponse(result, headers={"Cache-Control": "no-store"},
                            background=BackgroundTask(after_response))

    async def open_log(request: Request):
        if await control_body(request) != {}:
            raise HTTPException(400, "empty JSON object required")
        # Only the application chooses this path; no request-supplied path.
        path = getattr(app.state, "app_log_path", None)
        if not path or not Path(path).is_file():
            raise HTTPException(503, "App log unavailable")
        try:
            os.startfile(path)
        except OSError:
            raise HTTPException(503, "Could not open app log") from None
        return JSONResponse(dict(opened=True), headers={"Cache-Control": "no-store"})

    app.add_api_route("/app/quit", quit_app, methods=["POST"])
    app.add_api_route("/app/open-log", open_log, methods=["POST"])
