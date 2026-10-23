"""Explicit offline payload export and first-run diagnostics. No acquisition code."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List
import zipfile

MAGNET_UNAVAILABLE = 'Magnet links are unavailable on this PC (OpenSSL 1.1 files not found); file comparison still works'

# Wall-clock budgets (seconds) for the preflight's sequential children: two
# runtime closures, models/CUDA, suite version, magnet ABI and four packaged
# tools. Every child gets min(its own budget, time left in the total), so the
# whole preflight is bounded: 8 x 90 + 300 = 1020 s fits PREFLIGHT_TOTAL_BUDGET_S.
# Cross-repository contract: Comparizer's installer allows one preflight
# 1800 s (tools/verify_app_release.py BUDGETS['preflight'], pinning this total
# as DASHBOARD_PREFLIGHT_TOTAL_S) and its stable launcher waits 1800 s for
# readiness (setup/app_launcher.py READINESS_TIMEOUT_S), which also covers the
# suite handshake and server start after this preflight.
PROBE_TIMEOUT_S = 90
MODEL_PROBE_TIMEOUT_S = 300  # cold CUDA initialisation and model hashing
PREFLIGHT_TOTAL_BUDGET_S = 1200
_monotonic = time.monotonic


class _Budget:
    def __init__(self, total):
        self.deadline = _monotonic() + total

    def take(self, wanted):
        remaining = self.deadline - _monotonic()
        if remaining <= 0:
            raise ValueError('preflight time budget exhausted before this check; nothing was started')
        return min(wanted, remaining)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _safe(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/'))
            or any(ord(c) < 32 for c in name)):
        raise ValueError('unsafe payload path')
    path = Path(root).joinpath(*name.split('/'))
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            s = ancestor.lstat()
            if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
                raise ValueError('reparse payload path refused')
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('payload path escaped root')
    return path


def _run(argv, *, timeout=PROBE_TIMEOUT_S):
    import proc_guard
    process = proc_guard.run_tool(argv, timeout=timeout)
    if process.returncode:
        raise ValueError('packaged executable check failed')
    return process.stdout


def _probe(python, code, *args, timeout=PROBE_TIMEOUT_S):
    if not Path(python).is_file():
        raise FileNotFoundError('private Python executable missing')
    # -I ignores ambient Python paths. Set acquisition guards in this child,
    # without changing the shared host environment while another job runs.
    offline = ("import os; os.environ.pop('PYTHONPATH', None); os.environ.pop('PYTHONHOME', None); "
               "os.environ.update(PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1', "
               "HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', PYTHONDONTWRITEBYTECODE='1'); ")
    return json.loads(_run([str(python), '-I', '-B', '-c', offline + code, *map(str, args)], timeout=timeout))


# Where pip can place the packaged cp311 libtorrent extension in a runtime's
# site-packages. The approved libtorrent-2.0.11-cp311-cp311-win_amd64.whl installs
# a package folder (RECORD: libtorrent/__init__.cp311-win_amd64.pyd); a flat
# extension module is the other layout CPython imports. The package folder is
# tried first, as CPython's own import order does.
LIBTORRENT_CP311_LAYOUTS = ('libtorrent/__init__.cp311-win_amd64.pyd',
                            'libtorrent.cp311-win_amd64.pyd')


def packaged_libtorrent(runtime_root):
    """The packaged cp311 libtorrent binary of a private runtime, or None.

    Each candidate goes through the same traversal/reparse refusal as every
    other payload path, so a junctioned package folder is never accepted."""
    for layout in LIBTORRENT_CP311_LAYOUTS:
        module = _safe(runtime_root, 'Lib/site-packages/' + layout)
        if module.is_file():
            return module
    return None


def full_available(checks):
    return all(c['status'] == 'ok' for c in checks if c['capability'] in {'full', 'suite', 'files'})


def releasable(checks):
    return bool(checks) and all(c['status'] == 'ok' for c in checks)


def preflight(release_root: Path, manifest: Dict[str, Any], data_root: Path) -> List[Dict[str, str]]:
    root = Path(release_root).absolute()
    checks = []
    budget = _Budget(PREFLIGHT_TOTAL_BUDGET_S)

    def check(name, capability, action):
        try:
            message = action() or 'Verified packaged support'
            checks.append(dict(id=name, status='ok', message=str(message), capability=capability))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            checks.append(dict(id=name, status='missing' if isinstance(exc, (FileNotFoundError, KeyError)) else 'error',
                               message=str(exc), capability=capability))

    install = root.parents[2] if root.parent.name == 'releases' and root.parent.parent.name == 'app' else root
    runtimes = {}
    for kind in ('suite', 'dashboard'):
        def runtime(kind=kind):
            name = manifest['runtimes'][kind]
            if name != '.venv' and not name.startswith('runtimes/'):
                raise ValueError('runtime must be private to the install')
            runtime_root = _safe(install, name)
            python = _safe(runtime_root, 'Scripts/python.exe')
            component = root / kind
            lock = root / ('suite/installer/requirements-suite-plan4b.txt' if kind == 'suite' else 'dashboard/requirements-app-lock.txt')
            expected_hash = manifest['locks'][kind + '_sha256']
            if _hash(lock) != expected_hash:
                raise ValueError('runtime lock hash mismatch')
            # Validate the complete installed name/version closure without pip or a resolver.
            code = ("import sys,json,re;from importlib import metadata;from pathlib import Path;"
                    "assert sys.version_info[:3]==(3,11,9);"
                    "norm=lambda n:re.sub(r'[-_.]+','-',n).lower();"
                    "expected={norm(m[0]):m[1] for m in re.findall(r'^([A-Za-z0-9_.-]+)==([^ \\n]+)',Path(sys.argv[1]).read_text(),re.M)};"
                    "actual={norm(d.metadata['Name']):d.version for d in metadata.distributions()};"
                    "assert expected==actual,(sorted(expected.items()),sorted(actual.items()));"
                    "print(json.dumps({'python':sys.version.split()[0]}))")
            _probe(python, code, lock, timeout=budget.take(PROBE_TIMEOUT_S))
            runtimes[kind] = (runtime_root, python, component)
            return 'Private Python 3.11.9 and complete pinned runtime closure verified'
        check(kind + '_runtime', 'full' if kind == 'suite' else 'files', runtime)

    def models():
        _, python, suite = runtimes['suite']
        code = ("import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from videosuite.offline_runtime import verify_offline_assets;"
                "m=json.loads(Path(sys.argv[2]).read_text());"
                "errors=verify_offline_assets(m,Path(sys.argv[2]).parent);"
                "assert not errors,errors;import cv2,torch;"
                "assert cv2.cuda.getCudaEnabledDeviceCount()>0;"
                "assert torch.cuda.is_available();"
                "assert '3050 Ti' in torch.cuda.get_device_name(0);"
                "print(json.dumps('Pinned models and RTX 3050 Ti CUDA runtime verified'))")
        return _probe(python, code, suite, root / 'release.json', timeout=budget.take(MODEL_PROBE_TIMEOUT_S))
    check('models', 'full', models)

    def suite_version():
        _, python, suite = runtimes['suite']
        code = ("import sys,json;sys.path.insert(0,sys.argv[1]);from videosuite.cli import main;"
                "sys.argv=['videosuite','version','--json'];main()")
        version = _probe(python, code, suite, timeout=budget.take(PROBE_TIMEOUT_S))
        declared = manifest['suite'].get('capabilities')
        probed = version.get('capabilities')
        if (version.get('tool_version') != manifest['suite']['version']
                or not isinstance(declared, list) or not isinstance(probed, list)
                or 'offline_payload' not in probed or set(declared) != set(probed)):
            raise ValueError('suite version/capability mismatch')
        return 'Packaged suite version and declared capabilities verified'
    check('suite', 'suite', suite_version)

    def magnet():
        runtime_root, python, _ = runtimes['dashboard']
        module = packaged_libtorrent(runtime_root)
        if module is None:
            raise FileNotFoundError('Required packaged cp311 libtorrent is missing; release refused')
        code = ("import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                "from videosuite.offline_runtime import verify_magnet_prerequisites;"
                "print(json.dumps(verify_magnet_prerequisites(Path(sys.argv[2]),vc_runtime=Path(sys.argv[3]),python_dlls=Path(sys.base_prefix)/'DLLs')))")
        result = _probe(python, code, root / 'suite', module, root / 'tools/vc-runtime',
                        timeout=budget.take(PROBE_TIMEOUT_S))
        if result['status'] != 'ok':
            raise FileNotFoundError(result['message'])
        return result['message']
    check('magnet_runtime', 'magnets', magnet)

    tools = {r.get('name'): r for r in manifest.get('tools', []) if isinstance(r, dict)}
    for name, capability, arguments in (
        ('ffmpeg', 'full', ['-hide_banner', '-filters']), ('ffprobe', 'files', ['-version']),
        ('dovi_tool', 'dolby_vision', ['--version']), ('mediainfo', 'mediainfo_cli', ['--Version'])):
        def tool(name=name, arguments=arguments):
            if name not in tools:
                raise FileNotFoundError(f'Packaged {name} executable missing')
            row = tools[name]
            path = _safe(root, row['path'])
            if _hash(path) != row['sha256']:
                raise ValueError(f'Packaged {name} SHA256 mismatch')
            output = _run([str(path), *arguments], timeout=budget.take(PROBE_TIMEOUT_S))
            if name == 'ffmpeg' and 'libvmaf' not in output:
                raise ValueError('Packaged ffmpeg lacks required libvmaf')
            return f'Packaged {name} verified'
        check(name, capability, tool)

    def writable():
        data = Path(data_root).absolute()
        # Apply the same ancestor refusal to the explicit scratch/data root.
        _safe(data.parent, data.name)
        for name in ('uploads', 'reports', 'logs'):
            directory = _safe(data, name)
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory) as handle:
                handle.write(b'preflight')
        return 'App data, uploads, reports and logs are writable'
    check('data', 'files', writable)

    def space():
        required = manifest['required_free_bytes']
        if type(required) is not int or required < 0:
            raise ValueError('invalid required free space')
        if shutil.disk_usage(data_root).free < required:
            raise ValueError('Insufficient app scratch/report space')
        return 'Required app scratch/report free space available'
    check('space', 'files', space)
    return checks


def export_payload(source: Path, destination: Path, build_id: str, revision: str,
                   snapshot_manifest: Path, *, require_committed=False) -> Path:
    root, destination = Path(source).absolute(), Path(destination)
    if not destination.is_absolute() or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', build_id):
        raise ValueError('absolute destination and safe build ID required')
    actual_revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or actual_revision != revision:
        raise ValueError('source revision mismatch')
    snapshot = json.loads(Path(snapshot_manifest).read_text(encoding='utf-8'))
    if snapshot['revision'] != revision or Path(snapshot['root']).resolve() != root.resolve():
        raise ValueError('source snapshot identity mismatch')
    dirty = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True).strip())
    if require_committed and dirty:
        raise ValueError('production artifact requires owner-committed clean source')
    current_names = {p for p in subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z',
        '--cached', '--others', '--exclude-standard']).decode('utf-8').split('\0') if p}
    if current_names != set(snapshot['files']):
        raise ValueError('source snapshot file set changed')
    for name, row in snapshot['files'].items():
        if row.get('missing') or _hash(_safe(root, name)) != row['sha256']:
            raise ValueError(f'stale source snapshot: {name}')
    payload = json.loads((root / 'app-payload.json').read_text(encoding='utf-8'))
    files = payload['files']
    if len(files) != len(set(files)) or any('/' in p or not p.endswith('.py') for p in files):
        raise ValueError('backend allowlist must contain unique flat Python modules')
    required, pending = set(), ['main.py', 'app_host.py']
    while pending:
        name = pending.pop()
        if name in required:
            continue
        required.add(name)
        for node in ast.walk(ast.parse(_safe(root, name).read_text(encoding='utf-8'))):
            modules = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                       [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for module in modules:
                local = module.replace('.', '/') + '.py'
                if (root / local).is_file():
                    pending.append(local)
    if not required <= set(files):
        raise ValueError('backend local import closure incomplete')
    names = set(files) | {'app-payload.json', 'requirements-app-lock.txt', 'package-lock.json'}
    dist = _safe(root, 'dist')
    if not (dist / 'index.html').is_file():
        raise ValueError('build frontend before export')
    for path in dist.rglob('*'):
        if path.is_file():
            names.add(path.relative_to(root).as_posix())
    rows = []
    for name in sorted(names):
        path = _safe(root, name)
        rows.append(dict(path=name, size=path.stat().st_size, sha256=_hash(path)))
    manifest = dict(schema='videosuite-dashboard-artifact/1', build_id=build_id,
        source=dict(revision=revision, tree_hash=hashlib.sha256(json.dumps(snapshot['files'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(), snapshot_sha256=_hash(snapshot_manifest), dirty=dirty),
        frontend=dict(build_id=build_id, entry='dist/index.html'), backend=dict(build_id=build_id, entry='app_host.py'),
        locks=dict(dashboard_sha256=_hash(root / 'requirements-app-lock.txt'), npm_sha256=_hash(root / 'package-lock.json')),
        files=rows)
    _safe(destination.parent, destination.name)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f'dashboard-{build_id}.zip'
    sidecar = archive.with_suffix('.manifest.json')
    checksum = archive.with_suffix('.sha256')
    if any(p.exists() for p in (archive, sidecar, checksum)):
        raise ValueError('artifact destination already exists')
    raw = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as output:
        for row in rows:
            output.write(_safe(root, row['path']), row['path'])
        output.writestr('dashboard-manifest.json', raw)
    sidecar.write_bytes(raw)
    checksum.write_text(_hash(archive) + '  ' + archive.name + '\n', encoding='ascii', newline='\n')
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', action='store_true', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--build-id', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--snapshot-manifest', type=Path, required=True)
    parser.add_argument('--require-committed', action='store_true')
    args = parser.parse_args()
    print(export_payload(Path(__file__).absolute().parent, args.destination, args.build_id,
                         args.revision, args.snapshot_manifest, require_committed=args.require_committed))


if __name__ == '__main__':
    main()
