"""Native UI is replaced at the common-dialog seam; tests never open a window."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import native_chooser as nc
from test_app_routes import controls, headers


def test_chooser_requires_secret_origin_json_and_is_single_flight(controls, monkeypatch):
    client, _, _ = controls
    entered, release = threading.Event(), threading.Event()
    calls = []
    def dialog(owner):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return []
    monkeypatch.setattr(nc, '_dialog', dialog)
    for h in ({}, headers(origin='null'), headers(**{'X-VideoSuite-Token': 'wrong'})):
        assert client.post('/app/choose-files', json={}, headers=h).status_code == 403
    assert client.post('/app/choose-files', content='{}', headers=headers()).status_code == 415
    assert client.post('/app/choose-files', json={'path': 'C:/x'}, headers=headers()).status_code == 400
    assert calls == []
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, '/app/choose-files', json={}, headers=headers())
        assert entered.wait(5)
        assert client.post('/app/choose-files', json={}, headers=headers()).status_code == 409
        release.set()
        assert first.result().json() == {'paths': [], 'cancelled': True}
    assert len(calls) == 1
    assert client.post('/app/choose-files', json={}, headers=headers()).status_code == 200
    assert calls[0] == calls[1]  # one dedicated native UI thread across requests


def test_multiselect_buffer_keeps_long_unicode_paths():
    folder = 'D:\\' + 'long\\' * 60 + 'Кино & 100%'
    assert nc._parse_selection(folder + '\0А.mkv\0Б.mp4\0\0') == [folder + '\\А.mkv', folder + '\\Б.mp4']
    assert nc._parse_selection('D:\\Кино.mkv\0\0') == ['D:\\Кино.mkv']
    assert nc._parse_selection('\0\0') == []


def test_chosen_paths_still_pass_path_guard_and_duplicate_checks(controls, monkeypatch, tmp_path):
    client, _, _ = controls
    a, b = tmp_path / 'A.mkv', tmp_path / 'B.mkv'
    a.write_bytes(b'a'); b.write_bytes(b'b')
    paths = [str(a), str(b)]
    monkeypatch.setattr(nc, '_dialog', lambda owner: paths)
    assert client.post('/app/choose-files', json={}, headers=headers()).json()['paths'] == paths
    for invalid in ([str(a)], paths * 26, [str(a), str(a).upper()], [str(a), str(a) + ':stream'],
                    [str(a), r'\\server\share\B.mkv']):
        paths[:] = invalid
        assert client.post('/app/choose-files', json={}, headers=headers()).status_code in (400, 404, 422)
    other = tmp_path / 'other'
    other.mkdir(); (other / 'A.mkv').write_bytes(b'x')
    paths[:] = [str(a), str(other / 'A.mkv')]
    assert client.post('/app/choose-files', json={}, headers=headers()).status_code == 400


def test_chooser_refuses_during_quit_and_returns_paste_fallback_on_failure(controls, monkeypatch):
    client, life, _ = controls
    def unavailable(owner):
        raise OSError('synthetic native failure')
    monkeypatch.setattr(nc, '_dialog', unavailable)
    response = client.post('/app/choose-files', json={}, headers=headers())
    assert response.status_code == 503 and 'paste individual paths' in response.text
    life.request_quit('when_finished')
    assert client.post('/app/choose-files', json={}, headers=headers()).status_code == 409


def test_open_dialog_does_not_hold_asyncio_or_interpreter_shutdown(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    code = r'''
import asyncio, threading
from fastapi import FastAPI
from starlette.requests import Request
import native_chooser as nc
import app_routes
from types import SimpleNamespace
from app_lifecycle import AppLifecycle
from jobs import JobStore
from proc_guard import WorkerRegistry
entered = threading.Event()
def dialog(owner):
    entered.set()
    threading.Event().wait()  # deliberately never dismissed; no real window
nc._dialog = dialog
async def run():
    app = FastAPI()
    app.state.app_identity = dict(origin='http://127.0.0.1:12345', control_secret='a'*64)
    app.state.app_server = SimpleNamespace(should_exit=False)
    app_routes.install_app_routes(app, AppLifecycle(JobStore(), WorkerRegistry()))
    route = next(r.endpoint for r in app.routes if r.path == '/app/choose-files')
    async def receive():
        return dict(type='http.request', body=b'{}')
    request = Request(dict(type='http', app=app, headers=[(b'origin', b'http://127.0.0.1:12345'),
        (b'x-videosuite-token', b'a'*64), (b'content-type', b'application/json')]), receive)
    task = asyncio.create_task(route(request))
    for _ in range(200):
        if entered.is_set(): break
        await asyncio.sleep(.01)
    assert entered.is_set()
    quit_route = next(r.endpoint for r in app.routes if r.path == '/app/quit')
    async def quit_body():
        return dict(type='http.request', body=b'{"mode":"stop"}')
    response = await quit_route(Request(request.scope, quit_body))
    await response.background()
    assert app.state.app_server.should_exit
    # Uvicorn also waits for active HTTP requests before interpreter shutdown:
    # the choose request must end on Quit without the dialog being dismissed.
    from fastapi import HTTPException
    try:
        await asyncio.wait_for(task, .5)
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError('Open chooser should end when Quit starts')
asyncio.run(run())
print('shutdown finished', flush=True)
'''
    child = subprocess.Popen([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        try:
            stdout, stderr = child.communicate(timeout=7)
        except subprocess.TimeoutExpired:
            child.kill()  # this test started this exact process
            stdout, stderr = child.communicate()
            pytest.fail('Open dialog kept asyncio/interpreter alive: ' + stderr)
        assert child.returncode == 0, stderr
        assert 'shutdown finished' in stdout
    finally:
        if child.poll() is None:
            child.kill(); child.communicate()
