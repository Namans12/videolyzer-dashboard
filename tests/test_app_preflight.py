"""Offline release checks run only explicit packaged executables, never a fetch."""
import json
import os
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


# The approved libtorrent-2.0.11-cp311-cp311-win_amd64.whl (installer/acquisitions.json
# python-48) installs a package folder, not a flat extension module. Its complete
# member list, as pip lays it out in site-packages:
APPROVED_LIBTORRENT_WHEEL = r'D:\Video Comparizer\installer\payload\plan4b\wheelhouse\libtorrent-2.0.11-cp311-cp311-win_amd64.whl'
APPROVED_LIBTORRENT_SHA256 = '1c6520d43593da16aaac7dd49e5778be59a4f975e2566eb571779c5527433b24'
APPROVED_LIBTORRENT_MEMBERS = (
    'libtorrent/__init__.cp311-win_amd64.pyd',
    'libtorrent-2.0.11.dist-info/METADATA',
    'libtorrent-2.0.11.dist-info/WHEEL',
    'libtorrent-2.0.11.dist-info/top_level.txt',
    'libtorrent-2.0.11.dist-info/RECORD',
)
PACKAGE_LAYOUT = 'libtorrent/__init__.cp311-win_amd64.pyd'
FLAT_LAYOUT = 'libtorrent.cp311-win_amd64.pyd'


def _release(tmp_path, native_layout=PACKAGE_LAYOUT):
    import hashlib
    root = tmp_path / 'app/releases/new'
    root.mkdir(parents=True)
    manifest = dict(runtimes={'suite': 'runtimes/suite', 'dashboard': 'runtimes/dashboard'},
                    locks={}, suite={'version': '0.7.0', 'capabilities': ['offline_payload', 'compare_all']}, tools=[], required_free_bytes=0)
    for kind in ('suite', 'dashboard'):
        py = tmp_path / 'runtimes' / kind / 'Scripts/python.exe'
        py.parent.mkdir(parents=True)
        py.write_bytes(b'synthetic interpreter')
        lock = root / ('suite/installer/requirements-suite-plan4b.txt' if kind == 'suite' else 'dashboard/requirements-app-lock.txt')
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b'fixture==1.0\n')
        manifest['locks'][kind+'_sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
    site = tmp_path / 'runtimes/dashboard/Lib/site-packages'
    site.mkdir(parents=True)
    native = site.joinpath(*native_layout.split('/')) if native_layout else None
    if native is not None:
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_bytes(b'synthetic extension')
    for name in ('ffmpeg', 'ffprobe', 'dovi_tool', 'mediainfo'):
        tool = root / ('tools/'+name+'.exe')
        tool.parent.mkdir(exist_ok=True)
        tool.write_bytes(name.encode())
        manifest['tools'].append(dict(name=name, path=tool.relative_to(root).as_posix(), sha256=hashlib.sha256(tool.read_bytes()).hexdigest()))
    return root, manifest, native


def test_explicit_private_probes_tools_models_and_magnet_status(tmp_path, monkeypatch):
    root, manifest, native = _release(tmp_path)
    calls = []
    suite_report = dict(v='0.7.0', caps=['offline_payload', 'compare_all'])
    def probe(python, code, *args, timeout):
        assert 0 < timeout <= pf.MODEL_PROBE_TIMEOUT_S
        calls.append((str(python), code, args))
        if 'verify_magnet_prerequisites' in code:
            return dict(status='missing', message=pf.MAGNET_UNAVAILABLE)
        if 'from videosuite.cli' in code:
            return dict(tool_version=suite_report['v'], capabilities=suite_report['caps'])
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
    for report in (dict(v='0.6.0', caps=['offline_payload', 'compare_all']),
                   dict(v='0.7.0', caps=['offline_payload']),
                   dict(v='0.7.0', caps=['offline_payload', 'compare_all', 'extra'])):
        suite_report.update(report)
        suite_check = next(c for c in pf.preflight(root, manifest, tmp_path / 'data3') if c['id'] == 'suite')
        assert suite_check['status'] != 'ok' and 'mismatch' in suite_check['message']


def _magnet_check(tmp_path, monkeypatch, root, manifest):
    """Run the real preflight with fake children; return the magnet row and the
    module path the isolated magnet probe was given (None if never started)."""
    probed = []
    def probe(python, code, *args, timeout):
        if 'verify_magnet_prerequisites' in code:
            probed.append(Path(args[1]))
            return dict(status='ok', message='Packaged libtorrent and existing System32 OpenSSL 1.1 verified')
        if 'from videosuite.cli' in code:
            return dict(tool_version='0.7.0', capabilities=['offline_payload', 'compare_all'])
        return 'Verified synthetic probe'
    monkeypatch.setattr(pf, '_probe', probe)
    monkeypatch.setattr(pf, '_run', lambda argv, **k: 'libvmaf' if 'ffmpeg' in argv[0] else 'version')
    checks = pf.preflight(root, manifest, tmp_path / 'data')
    magnet = next(c for c in checks if c['id'] == 'magnet_runtime')
    return magnet, (probed[0] if probed else None), checks


def test_approved_libtorrent_wheel_installs_a_package_folder():
    """Pins the fixture below to the real approved artifact (read-only)."""
    import hashlib
    import zipfile
    wheel = Path(APPROVED_LIBTORRENT_WHEEL)
    assert wheel.is_file(), 'approved staged libtorrent wheel missing'
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == APPROVED_LIBTORRENT_SHA256
    inventory = json.loads(Path(r'D:\Video Comparizer\installer\acquisitions.json').read_text(encoding='utf-8'))
    row, = [r for r in inventory['artifacts'] if r.get('name') == 'libtorrent']
    assert row['sha256'] == APPROVED_LIBTORRENT_SHA256
    with zipfile.ZipFile(wheel) as archive:
        assert tuple(archive.namelist()) == APPROVED_LIBTORRENT_MEMBERS
        record = archive.read('libtorrent-2.0.11.dist-info/RECORD').decode('utf-8')
    assert any(line.startswith(PACKAGE_LAYOUT + ',') for line in record.splitlines())
    assert FLAT_LAYOUT not in record


def test_magnet_check_finds_libtorrent_in_the_real_installed_wheel_layout(tmp_path, monkeypatch):
    """Regression (installed QA F1): the approved wheel's real site-packages layout
    must pass, and the isolated probe must load that exact packaged file."""
    root, manifest, _ = _release(tmp_path, native_layout=None)
    site = tmp_path / 'runtimes/dashboard/Lib/site-packages'
    for member in APPROVED_LIBTORRENT_MEMBERS:
        target = site.joinpath(*member.split('/'))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'member of the approved wheel: ' + member.encode())
    assert not (site / FLAT_LAYOUT).exists()
    magnet, module, checks = _magnet_check(tmp_path, monkeypatch, root, manifest)
    assert magnet['status'] == 'ok', magnet
    assert module == site / 'libtorrent' / '__init__.cp311-win_amd64.pyd'
    assert all(c['status'] == 'ok' for c in checks if c['id'] not in ('data', 'space')), checks


@pytest.mark.parametrize('layout', [PACKAGE_LAYOUT, FLAT_LAYOUT])
def test_magnet_check_accepts_either_packaged_cp311_layout(tmp_path, monkeypatch, layout):
    root, manifest, native = _release(tmp_path, native_layout=layout)
    magnet, module, _ = _magnet_check(tmp_path, monkeypatch, root, manifest)
    assert magnet['status'] == 'ok' and module == native


@pytest.mark.parametrize('decoy', [None, 'libtorrent/__init__.cp310-win_amd64.pyd',
                                   'libtorrent/__init__.py', 'libtorrent.cp312-win_amd64.pyd'])
def test_magnet_check_refuses_when_no_packaged_cp311_binary_exists(tmp_path, monkeypatch, decoy):
    root, manifest, _ = _release(tmp_path, native_layout=decoy)
    magnet, module, checks = _magnet_check(tmp_path, monkeypatch, root, manifest)
    assert module is None, 'no probe may start without the packaged cp311 binary'
    assert magnet['status'] == 'missing'
    assert magnet['message'] == 'Required packaged cp311 libtorrent is missing; release refused'
    assert not pf.releasable(checks)


def test_magnet_check_refuses_a_reparse_libtorrent_package_folder(tmp_path, monkeypatch):
    import subprocess
    root, manifest, _ = _release(tmp_path, native_layout=None)
    real = tmp_path / 'elsewhere/libtorrent'
    real.mkdir(parents=True)
    (real / '__init__.cp311-win_amd64.pyd').write_bytes(b'foreign extension')
    link = tmp_path / 'runtimes/dashboard/Lib/site-packages/libtorrent'
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(real)], capture_output=True)
    if made.returncode:
        pytest.skip('cannot create a junction here')
    try:
        magnet, module, _ = _magnet_check(tmp_path, monkeypatch, root, manifest)
        assert module is None and magnet['status'] != 'ok'
    finally:
        os.rmdir(link)  # removes the junction only, never its target


def _timed_children(tmp_path, monkeypatch, total):
    """Real _probe/_run argument paths with a fake clock: each child uses its whole budget."""
    root, manifest, _ = _release(tmp_path)
    clock, children = [0.0], []
    def run(argv, *, timeout):
        children.append(timeout)
        clock[0] += timeout
        code = ' '.join(map(str, argv))
        if 'verify_magnet_prerequisites' in code:
            return json.dumps(dict(status='ok', message='magnet ok'))
        if 'from videosuite.cli' in code:
            return json.dumps(dict(tool_version='0.7.0', capabilities=['offline_payload', 'compare_all']))
        if '-I' in argv:
            return json.dumps('probe ok')
        return 'libvmaf'
    monkeypatch.setattr(pf, '_run', run)
    monkeypatch.setattr(pf, '_monotonic', lambda: clock[0])
    monkeypatch.setattr(pf, 'PREFLIGHT_TOTAL_BUDGET_S', total)
    return pf.preflight(root, manifest, tmp_path / 'data'), children


def test_preflight_children_have_named_budgets_within_a_bounded_total(tmp_path, monkeypatch):
    """Nine sequential children: 8 x 90 s probes + 300 s cold model/CUDA probe.
    The total (the cross-repo contract with Comparizer's installer smoke budget
    and launcher readiness wait, both 1800 s) bounds them all."""
    assert pf.PROBE_TIMEOUT_S == 90 and pf.MODEL_PROBE_TIMEOUT_S == 300
    assert 8 * pf.PROBE_TIMEOUT_S + pf.MODEL_PROBE_TIMEOUT_S <= pf.PREFLIGHT_TOTAL_BUDGET_S == 1200
    checks, children = _timed_children(tmp_path, monkeypatch, pf.PREFLIGHT_TOTAL_BUDGET_S)
    assert children == [90, 90, 300, 90, 90, 90, 90, 90, 90]
    assert all(c['status'] == 'ok' for c in checks if c['id'] not in ('data', 'space')), checks


def test_exhausted_preflight_budget_refuses_remaining_checks_without_children(tmp_path, monkeypatch):
    checks, children = _timed_children(tmp_path, monkeypatch, 500)
    # runtimes 90+90, models 300, then the suite probe gets the 20 s left.
    assert children == [90, 90, 300, 20]
    indexed = {c['id']: c for c in checks}
    for name in ('magnet_runtime', 'ffmpeg', 'ffprobe', 'dovi_tool', 'mediainfo'):
        assert indexed[name]['status'] == 'error' and 'time budget' in indexed[name]['message']
    assert not pf.releasable(checks)
