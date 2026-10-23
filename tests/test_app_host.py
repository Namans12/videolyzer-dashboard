"""Built routes and production policy on the actual backend app (no listener)."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

import main
from app_host import configure_origin, install_static


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    app = main.app
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    monkeypatch.setattr(app, "user_middleware", list(app.user_middleware))
    monkeypatch.setattr(app, "middleware_stack", None)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / ".vite").mkdir()
    (dist / "index.html").write_text('<html><script type="module" src="/assets/tiny.js"></script></html>')
    (dist / "assets" / "tiny.js").write_text('console.log("built");')
    (dist / ".vite" / "manifest.json").write_text(json.dumps({
        "index.html": {"file": "assets/tiny.js", "isEntry": True}
    }))
    (dist / "private.txt").write_text("not shipped to browsers")
    (dist / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    install_static(app, dist)
    configure_origin(app, "http://127.0.0.1:49152")
    with TestClient(app, base_url="http://127.0.0.1:49152") as client:
        yield client


def test_spa_fallback_never_catches_api_or_missing_assets(hosted):
    assert hosted.get("/suite/missing").status_code == 404
    assert hosted.get("/assets/missing.js").status_code == 404
    for path in ("/app/missing", "/comparison/missing", "/job/missing", "/health/missing",
                 "/analysis/missing", "/magnet/missing", "/private.txt", "/.vite/manifest.json",
                 "/main.py", "/missing.js", "/unknown-page"):
        assert hosted.get(path).status_code == 404, path
    for path in ("/", "/analyze", "/suite", "/calibration"):
        response = hosted.get(path)
        assert response.status_code == 200, path
        assert '<script type="module"' in response.text
    asset = hosted.get("/assets/tiny.js")
    assert asset.status_code == 200
    assert asset.text == 'console.log("built");'
    assert "javascript" in asset.headers["content-type"]
    assert hosted.get("/favicon.svg").status_code == 200
    assert hosted.get("/health/").json()["status"] == "ok"
    assert hosted.post("/analyze", json={}).status_code == 405


def test_production_rejects_other_loopback_origin(hosted):
    foreign_port = hosted.get("/health/", headers={"Origin": "http://127.0.0.1:8000"})
    assert foreign_port.status_code == 403
    assert "access-control-allow-origin" not in foreign_port.headers
    for origin in ("http://localhost:49152", "null", "https://evil.example"):
        assert hosted.get("/health/", headers={"Origin": origin}).status_code == 403
    same = hosted.get("/health/", headers={"Origin": "http://127.0.0.1:49152"})
    assert same.status_code == 200
    assert same.headers["access-control-allow-origin"] == "http://127.0.0.1:49152"
    for host in ("localhost:49152", "127.0.0.1:8000", "evil.example"):
        assert hosted.get("/health/", headers={"Host": host}).status_code == 400
    preflight = hosted.options("/suite/score", headers={
        "Origin": "http://127.0.0.1:8000", "Access-Control-Request-Method": "POST"
    })
    assert preflight.status_code in (400, 403)
    assert "access-control-allow-origin" not in preflight.headers
    wrong_host = hosted.options("/suite/score", headers={
        "Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:49152",
        "Access-Control-Request-Method": "POST"
    })
    assert wrong_host.status_code == 400


def test_static_refuses_escaped_asset(hosted, tmp_path):
    (tmp_path / "outside.js").write_text("secret")
    # Traversal must never expose a sibling or fall back to index.
    assert hosted.get("/assets/%2e%2e/private.txt").status_code == 404


@pytest.mark.parametrize("origin", ["http://evil.example:80", "http://127.0.0.1:0",
                                    "http://127.0.0.1:49152/path", "http://user@127.0.0.1:49152"])
def test_invalid_production_origin_fails_closed(origin):
    with pytest.raises(ValueError):
        configure_origin(copy.copy(main.app), origin)


def test_bound_socket_is_the_socket_uvicorn_serves(monkeypatch):
    import asyncio
    import app_host
    from types import SimpleNamespace
    bound = object()
    served = []
    ready = []
    class Server:
        started = False
        def __init__(self, config): pass
        async def startup(self, sockets=None):
            served.extend(sockets)
            self.started = True
        def run(self, sockets=None):
            asyncio.run(self.startup(sockets=sockets))
    monkeypatch.setattr(app_host.uvicorn, "Server", Server)
    monkeypatch.setattr(app_host.uvicorn, "Config", lambda *a, **kw: SimpleNamespace())
    app_host.run_bound(main.app, bound, lambda: ready.append(True))
    assert served[0] is bound
    assert ready == [True]


def _check_occupied_port_launcher(monkeypatch, tmp_path, inject_effect=None):
    """Real bind/parser/launcher orchestration; every OS effect is a fake."""
    import app_host
    from pathlib import Path
    from types import SimpleNamespace
    # This cross-repository check uses the unchanged launcher under review.
    monkeypatch.syspath_prepend(str(Path(r"D:\Video Comparizer") / "setup"))
    import app_launcher as launcher

    foreign_pid, owned_pid = 9999, 123
    foreign_origin = "http://127.0.0.1:8000"
    authenticated_origin = "http://127.0.0.1:49321"
    opened, killed, sockets, binds, published, events = [], [], [], [], [], []

    def open_browser(origin):
        opened.append(origin)
        return True

    def terminate(pid):
        killed.append(pid)

    class Socket:
        def __init__(self, *a):
            self.port = None
            sockets.append(self)
        def setsockopt(self, *a): pass
        def bind(self, address):
            binds.append(address)
            assert address[0] == "127.0.0.1"
            if address[1] == 8000:
                raise OSError(10048, "occupied by impostor")
            assert address[1] == 0
            self.port = 49321
        def getsockname(self): return ("127.0.0.1", self.port)
        def listen(self, *a): pass
        def set_inheritable(self, *a): pass
        def close(self): events.append("socket-closed")

    class Process:
        pid = owned_pid
        alive = True
        def poll(self): return None if self.alive else 0
        def kill(self):
            terminate(self.pid)
            self.alive = False
        def wait(self, timeout=None):
            self.alive = False
            events.append("owned-waited")
            return 0

    class Channel:
        def send_bytes(self, data): self.bootstrap = json.loads(data)
        def poll(self, timeout): return True
        def recv_bytes(self, maximum):
            record = app_host.parse_instance_record(dict(self.bootstrap["identity"],
                pid=owned_pid, process_created="133000000000000001", origin=authenticated_origin),
                expected=self.bootstrap["identity"])
            if scenario == "foreign-pid":
                record.update(pid=foreign_pid, origin=foreign_origin)
            elif scenario == "wrong-session":
                record.update(session_id="c"*32, origin=foreign_origin)
            return json.dumps(dict(record=record, checks=[])).encode()
        def close(self): events.append("channel-closed")

    monkeypatch.setattr(app_host.socket, "socket", Socket)
    monkeypatch.setattr(launcher, "current_user_sid", lambda: "synthetic-sid")
    monkeypatch.setattr(launcher, "data_root", lambda: tmp_path / "app")
    monkeypatch.setattr(launcher, "resolve_release", lambda root: dict(
        install_id="i", build_id="b", install_root=str(root)))
    monkeypatch.setattr(launcher.os, "set_handle_inheritable", lambda *args: None)
    monkeypatch.setattr(launcher, "process_created", lambda pid: "133000000000000001")
    monkeypatch.setattr(launcher, "SupervisorJob", lambda: SimpleNamespace(
        assign=lambda process: False, close=lambda: events.append("job-closed")))

    class Tree:
        """Fake of the launcher's verified process tree (venv redirector layout): a PID
        that is not the launched child or its verified descendant is refused."""
        def __init__(self, process):
            self.members = {process.pid: process}
        def discover(self): pass
        def verify(self, pid, created):
            if pid not in self.members:
                raise ValueError("readiness PID is outside launched process tree")
            return self.members[pid]
        def close(self):
            return []

    monkeypatch.setattr(launcher, "ProcessTree", Tree)

    def spawn(*args):
        bound = app_host.bind_loopback()
        assert bound is sockets[-1]
        assert bound.getsockname() == ("127.0.0.1", 49321)
        bound.close()
        if inject_effect:
            # Mutation seam shares the exact observers used by browser and kill.
            inject_effect(open_browser, terminate)
        return Process()

    monkeypatch.setattr(launcher, "spawn_backend", spawn)
    for scenario in ("valid", "foreign-pid", "wrong-session"):
        for observed in (opened, killed, sockets, binds, published, events):
            observed.clear()
        channel = Channel()
        child = SimpleNamespace(fileno=lambda: 42, close=lambda: events.append("child-closed"))
        lease = SimpleNamespace(primary=True, pipe_name=r"\\.\pipe\VideoSuite-test",
            publish=lambda record: published.append(record), close=lambda: events.append("lease-closed"))
        monkeypatch.setattr(launcher, "private_duplex", lambda sid: (channel, child))
        monkeypatch.setattr(launcher, "acquire_instance", lambda *args: lease)
        dialogs = []
        result = launcher.launch(tmp_path, open_browser=open_browser, dialog=dialogs.append)
        assert foreign_pid not in killed, "forbidden foreign termination callback"
        assert foreign_origin not in opened, "forbidden foreign browser callback"
        assert binds == [("127.0.0.1", 8000), ("127.0.0.1", 0)]
        assert len(sockets) == 2
        assert events.count("socket-closed") == 2
        assert "channel-closed" in events and "child-closed" in events
        assert events.count("job-closed") == 1
        assert events[-1] == "lease-closed"
        if scenario == "valid":
            assert result == 0 and dialogs == []
            assert opened == [authenticated_origin] and killed == []
            assert len(published) == 1 and published[0]["pid"] == owned_pid
        else:
            assert result == 1 and len(dialogs) == 1
            assert opened == [] and published == []
            assert killed == [owned_pid]
        assert events.count("owned-waited") == 1


def test_occupied_port_and_impostor_never_open_or_kill_foreign_server(monkeypatch, tmp_path):
    _check_occupied_port_launcher(monkeypatch, tmp_path)


@pytest.mark.parametrize("effect", ["terminate", "open"])
def test_occupied_port_assertions_detect_forbidden_callbacks(monkeypatch, tmp_path, effect):
    def inject(open_browser, terminate):
        if effect == "terminate":
            terminate(9999)
        else:
            open_browser("http://127.0.0.1:8000")
    with pytest.raises(AssertionError, match="forbidden foreign"):
        _check_occupied_port_launcher(monkeypatch, tmp_path, inject)


def test_bootstrap_requires_exact_origin_json_fetch_metadata_and_has_no_store(monkeypatch):
    import app_host
    from fastapi import FastAPI
    app = FastAPI()
    record = dict(install_id="i", build_id="b", session_id="a"*32, pid=123,
                  process_created="133000000000000001", origin="http://127.0.0.1:49152",
                  pipe_name=r"\\.\pipe\VideoSuite-test", control_secret="b"*64)
    app_host.install_identity(app, record, [])
    configure_origin(app, record["origin"])
    headers = {"Origin": record["origin"], "Sec-Fetch-Site": "same-origin",
               "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    with TestClient(app, base_url=record["origin"]) as client:
        for name in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"):
            bad = dict(headers)
            bad.pop(name)
            assert client.post("/app/bootstrap", json={}, headers=bad).status_code == 403
        response = client.post("/app/bootstrap", json={}, headers=headers)
        assert response.json() == {"token": record["control_secret"], "session_id": record["session_id"]}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/app/bootstrap", content="{}", headers=dict(headers, **{"Content-Type":"text/plain"})).status_code == 415
        assert client.post("/app/bootstrap", json={"extra": 1}, headers=headers).status_code == 400
        assert record["control_secret"] not in client.get("/app/status").text


def test_serve_publishes_private_readiness_only_after_startup_and_check(tmp_path, monkeypatch):
    import os
    import app_host
    import suite_client
    import suite_version
    import multiprocessing.connection
    from starlette.datastructures import State
    from types import SimpleNamespace
    app = main.app
    monkeypatch.setattr(app.router, "routes", list(app.router.routes))
    monkeypatch.setattr(app, "user_middleware", list(app.user_middleware))
    monkeypatch.setattr(app, "middleware_stack", None)
    monkeypatch.setattr(app, "state", State())
    dist = tmp_path / "dashboard/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("built fixture")
    identity = dict(install_id="i", build_id="b", session_id="a"*32,
                    pipe_name=r"\\.\pipe\VideoSuite-test", control_secret="b"*64)
    release = dict(install_root=str(tmp_path), dashboard_root=str(tmp_path / "dashboard"),
        suite_root=str(tmp_path / "suite"), suite_python=str(tmp_path / "suite/python.exe"),
        dashboard_python=str(tmp_path / "dashboard/pythonw.exe"), release_root=str(tmp_path),
        _identity=identity)
    monkeypatch.setenv("VIDEOSUITE_PYTHON", "prior")
    monkeypatch.setenv("VIDEOSUITE_ROOT", "prior")
    events, messages = [], []
    channel = SimpleNamespace(send_bytes=lambda data: messages.append(json.loads(data)),
                              close=lambda: events.append("channel-closed"))
    monkeypatch.setattr(multiprocessing.connection, "PipeConnection", lambda handle: channel)
    bound = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 49321), close=lambda: events.append("socket-closed"))
    monkeypatch.setattr(app_host, "bind_loopback", lambda: bound)
    monkeypatch.setattr(app_host, "preflight", lambda *a: [])
    monkeypatch.setattr(suite_client, "get_suite_client", lambda: object())
    def checked(*args, **kwargs):
        events.append("preflight")
        return dict(status="ok", message="suite available")
    monkeypatch.setattr(suite_version, "ensure", checked)
    def run(app, socket, ready):
        assert socket is bound
        assert messages == [] and events == ["preflight"]
        assert os.environ["VIDEOSUITE_PYTHON"] == release["suite_python"]
        events.append("ASGI-started")
        ready()
    monkeypatch.setattr(app_host, "run_bound", run)
    assert app_host.serve(tmp_path, release, 123) == 0
    assert events == ["preflight", "ASGI-started", "socket-closed", "channel-closed"]
    assert messages[0]["record"]["pid"] == os.getpid()
    assert messages[0]["record"]["origin"] == "http://127.0.0.1:49321"
    assert app.state.app_ready is True
