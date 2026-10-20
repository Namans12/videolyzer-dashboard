"""Offline release checks run only explicit packaged executables, never a fetch."""
import json
from pathlib import Path

import pytest

import app_preflight as pf


def test_preflight_checks_both_runtimes_tools_models_torrent_space_without_fetch(tmp_path, monkeypatch):
    import socket
    import urllib.request
    downloads = []
    def forbidden(*args, **kwargs):
        downloads.append(args)
        raise AssertionError('preflight attempted network access')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    checks = pf.preflight(tmp_path, {}, tmp_path / 'data')
    indexed = {c['id']: c for c in checks}
    assert {'suite_runtime', 'dashboard_runtime', 'models', 'magnet_runtime',
            'ffmpeg', 'ffprobe', 'dovi_tool', 'mediainfo', 'data', 'space'} <= indexed.keys()
    assert all(set(c) == {'id', 'status', 'message', 'capability'} for c in checks)
    assert indexed['mediainfo']['capability'] == 'mediainfo_cli'
    assert indexed['mediainfo']['status'] == 'missing'
    assert indexed['models']['status'] != 'ok'
    assert downloads == []


def test_missing_cp311_libtorrent_is_release_failure(tmp_path):
    checks = pf.preflight(tmp_path, {}, tmp_path / 'data')
    releasable = pf.releasable(checks)
    assert releasable is False
    magnet = next(c for c in checks if c['id'] == 'magnet_runtime')
    assert magnet['status'] == 'missing'
    assert magnet['capability'] == 'magnets'


def test_magnet_system_prerequisite_failure_keeps_file_checks_separate():
    checks = [dict(id='magnet_runtime', status='missing', capability='magnets',
                   message=pf.MAGNET_UNAVAILABLE)]
    assert pf.full_available(checks)
    assert not pf.releasable(checks)


def test_preflight_probe_after_stop_starts_no_child(monkeypatch):
    import proc_guard
    import subprocess
    import sys
    registry = proc_guard.WorkerRegistry()
    registry.stopping = True
    monkeypatch.setattr(proc_guard, 'workers', registry)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError('preflight started a child after Stop')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(OSError, match='shutting down'):
        pf._probe(sys.executable, 'print("null")')
    assert calls == []


def test_running_preflight_probe_is_owned_and_cancellable(tmp_path, monkeypatch):
    import proc_guard
    import subprocess
    import sys
    import threading
    import time
    registry = proc_guard.WorkerRegistry()
    monkeypatch.setattr(proc_guard, 'workers', registry)
    children, errors = [], []
    original = subprocess.Popen
    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', start)
    ready = tmp_path / 'probe-ready'
    def probe():
        try:
            pf._probe(sys.executable, 'import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30); print("null")', ready)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(exc)
    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        assert registry.ids(), 'running preflight probe is not tracked'
        with registry.lock:
            registry.stopping = True
        registry.signal()
        thread.join(5)
        assert not thread.is_alive()
        assert children and all(child.poll() is not None for child in children)
        assert registry.wait_all(2)
        assert errors
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(5)
        thread.join(5)


def test_preflight_refuses_reparse_or_traversal_runtime_before_execution(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pf, '_run', lambda *a, **k: calls.append(a))
    checks = pf.preflight(tmp_path, {'runtimes': {'suite': '../foreign', 'dashboard': '../foreign'}}, tmp_path / 'data')
    assert calls == []
    assert all(c['status'] != 'ok' for c in checks if c['id'].endswith('_runtime'))


def test_explicit_private_probes_tools_models_and_magnet_status(tmp_path, monkeypatch):
    import hashlib
    root = tmp_path / 'app/releases/new'
    root.mkdir(parents=True)
    manifest = dict(runtimes={'suite': 'runtimes/suite', 'dashboard': 'runtimes/dashboard'},
                    locks={}, suite={'version': '0.6.0'}, tools=[], required_free_bytes=0)
    for kind in ('suite', 'dashboard'):
        py = tmp_path / 'runtimes' / kind / 'Scripts/python.exe'
        py.parent.mkdir(parents=True)
        py.write_bytes(b'synthetic interpreter')
        lock = root / ('suite/installer/requirements-suite-plan4b.txt' if kind == 'suite' else 'dashboard/requirements-app-lock.txt')
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b'fixture==1.0\n')
        manifest['locks'][kind+'_sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
    native = tmp_path / 'runtimes/dashboard/Lib/site-packages/libtorrent.cp311-win_amd64.pyd'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'synthetic extension')
    for name in ('ffmpeg', 'ffprobe', 'dovi_tool', 'mediainfo'):
        tool = root / ('tools/'+name+'.exe')
        tool.parent.mkdir(exist_ok=True)
        tool.write_bytes(name.encode())
        manifest['tools'].append(dict(name=name, path=tool.relative_to(root).as_posix(), sha256=hashlib.sha256(tool.read_bytes()).hexdigest()))
    calls = []
    def probe(python, code, *args):
        calls.append((str(python), code, args))
        if 'verify_magnet_prerequisites' in code:
            return dict(status='missing', message=pf.MAGNET_UNAVAILABLE)
        if 'from videosuite.cli' in code:
            return dict(tool_version='0.6.0', capabilities=['offline_payload'])
        return 'Verified synthetic probe'
    monkeypatch.setattr(pf, '_probe', probe)
    monkeypatch.setattr(pf, '_run', lambda argv, **k: 'libvmaf' if 'ffmpeg' in argv[0] else 'version')
    checks = pf.preflight(root, manifest, tmp_path / 'data')
    indexed = {c['id']: c for c in checks}
    assert len(calls) == 5
    assert all(str(tmp_path / 'runtimes') in p for p, _, _ in calls)
    assert pf.full_available(checks)
    assert indexed['magnet_runtime']['message'] == pf.MAGNET_UNAVAILABLE
    assert indexed['mediainfo']['status'] == 'ok'
    native.unlink()
    checks = pf.preflight(root, manifest, tmp_path / 'data2')
    assert next(c for c in checks if c['id'] == 'magnet_runtime')['message'].startswith('Required packaged cp311')
    assert not pf.releasable(checks)
