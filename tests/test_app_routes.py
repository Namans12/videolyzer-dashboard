import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app_host import configure_origin, install_identity
from app_lifecycle import AppLifecycle
from app_routes import install_app_routes
from jobs import JobStore
from proc_guard import WorkerRegistry

ORIGIN = "http://127.0.0.1:12345"
TOKEN = "a" * 64


@pytest.fixture
def controls(monkeypatch):
    import jobs
    store = JobStore()
    monkeypatch.setattr(jobs, "jobs", store)
    app = FastAPI()
    configure_origin(app, ORIGIN)
    install_identity(app, dict(install_id="test", build_id="test", session_id="b" * 32,
        pid=os.getpid(), process_created="123", origin=ORIGIN,
        pipe_name=r"\\.\pipe\VideoSuite-test", control_secret=TOKEN), [])
    life = AppLifecycle(store, WorkerRegistry(), grace_s=.01, force_s=.01)
    install_app_routes(app, life)
    app.state.app_lifecycle = life
    # A real server would receive should_exit; tests use only this fake.
    from types import SimpleNamespace
    app.state.app_server = SimpleNamespace(should_exit=False)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, life, app


def headers(**extra):
    return {"origin": ORIGIN, "X-VideoSuite-Token": TOKEN, **extra}


def test_control_routes_require_secret_exact_origin_and_json(controls):
    client, _, _ = controls
    for path, body in (("/app/quit", {"mode": "keep"}), ("/app/open-log", {})):
        for h in ({"origin": ORIGIN}, headers(**{"X-VideoSuite-Token": "wrong"}),
                  headers(origin="http://127.0.0.1:12346"), headers(origin="null")):
            assert client.post(path, json=body, headers=h).status_code == 403
        assert client.post(path, content="{}", headers=headers()).status_code == 415
    assert client.post("/app/quit", json={"mode": "keep"}, headers=headers()).status_code == 200


def test_actual_status_serialization_legacy_census_for_frontend(controls):
    client, life, _ = controls
    for kind in ("suite", "path", "magnet", "calibrate-build"):
        life.jobs.create(kind, current=f"{kind} work")
    snapshot = client.get("/app/status").json()
    assert {j["kind"] for j in snapshot["active_jobs"]} == {"suite", "path", "magnet", "calibrate-build"}
    assert TOKEN not in json.dumps(snapshot)
    # The frontend consumes this actual serializer output, never a hand-built census.
    target = Path(os.environ["VIDEOLYZER_DATA_ROOT"]).parent / "app-status.json"
    target.write_text(json.dumps(snapshot), encoding="utf-8")


def test_quit_after_response_and_keep_when_finished(controls):
    client, life, app = controls
    j = life.jobs.create("magnet")
    response = client.post("/app/quit", json={"mode": "when_finished"}, headers=headers())
    assert response.json()["quit_pending"]
    assert client.get("/app/status").json()["quit_pending"]
    assert not app.state.app_server.should_exit
    assert client.post("/app/quit", json={"mode": "keep"}, headers=headers()).json()["accepted"]
    assert not life.jobs.is_cancelled(j)


def test_no_work_quit_requests_server_exit(controls):
    client, _, app = controls
    assert client.post("/app/quit", json={"mode": "stop"}, headers=headers()).json()["accepted"]
    assert app.state.app_server.should_exit


def test_when_finished_exits_only_after_last_legacy_job(controls):
    import time
    client, life, app = controls
    j = life.jobs.create("calibrate-build")
    client.post("/app/quit", json={"mode": "when_finished"}, headers=headers())
    assert not app.state.app_server.should_exit
    life.jobs.finish(j)
    deadline = time.monotonic() + 1
    while not app.state.app_server.should_exit and time.monotonic() < deadline:
        time.sleep(.01)
    assert app.state.app_server.should_exit
    assert life.workers.stopping


def test_failed_stop_reports_error_keeps_server_and_credential_out_of_logs(controls, caplog):
    client, life, app = controls
    life.register_worker("stuck", lambda: None, lambda timeout: False)
    response = client.post("/app/quit", json={"mode": "stop"}, headers=headers())
    assert response.json()["accepted"]
    assert not app.state.app_server.should_exit
    status = client.get("/app/status").json()
    assert status["checks"][-1]["id"] == "shutdown"
    assert status["checks"][-1]["status"] == "error"
    assert life.workers.ids() == ["stuck"]
    assert TOKEN not in caplog.text


def test_open_log_has_no_user_path(controls, monkeypatch, tmp_path):
    client, _, app = controls
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append)
    app.state.app_log_path = str(tmp_path / "app.log")
    Path(app.state.app_log_path).write_text("synthetic log")
    assert client.post("/app/open-log", json={"path": "C:/other"}, headers=headers()).status_code == 400
    assert client.post("/app/open-log", json={}, headers=headers()).json() == {"opened": True}
    assert opened == [app.state.app_log_path]


def test_browser_disconnect_does_not_cancel_workers(controls):
    client, life, _ = controls
    j = life.jobs.create("suite")
    with client.stream("GET", "/app/status") as response:
        assert response.status_code == 200
    assert life.jobs.is_cancelled(j) is False


if __name__ == "__main__":
    from jobs import jobs
    for kind in ("suite", "path", "magnet", "calibrate-build"):
        jobs.create(kind, current=f"{kind} work")
    app = FastAPI()
    install_identity(app, dict(install_id="test", build_id="test", session_id="b" * 32,
        pid=os.getpid(), process_created="123", origin=ORIGIN,
        pipe_name=r"\\.\pipe\VideoSuite-test", control_secret=TOKEN), [])
    with TestClient(app, base_url=ORIGIN) as client:
        print(json.dumps(client.get("/app/status").json()))
