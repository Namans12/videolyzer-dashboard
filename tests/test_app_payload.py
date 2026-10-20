"""The backend allowlist covers every transitive local import, including lazy ones."""
import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def local_import_closure(root, entries):
    pending = list(entries)
    found = set()
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for module in names:
                relative = module.replace(".", "/") + ".py"
                if (root / relative).is_file():
                    pending.append(relative)
    return found


def assert_payload(files, required):
    assert len(files) == len(set(files)), "duplicate payload entry"
    assert required <= set(files), f"missing local imports: {sorted(required - set(files))}"
    for name in files:
        path = Path(name)
        assert not path.is_absolute() and ".." not in path.parts
        assert (ROOT / name).is_file(), name


def test_payload_local_import_closure():
    payload = json.loads((ROOT / "app-payload.json").read_text(encoding="utf-8"))
    required = local_import_closure(ROOT, ["main.py", "app_host.py"])
    assert "app_host.py" in required and "calibration_routes.py" in required
    assert {"app_lifecycle.py", "app_routes.py"} <= required
    assert_payload(payload["files"], required)
    # Each omission, including the host itself, must fail this same assertion.
    for name in required:
        with pytest.raises(AssertionError, match="missing local imports"):
            assert_payload([f for f in payload["files"] if f != name], required)


def test_closure_walks_imports_inside_functions(tmp_path):
    (tmp_path / "entry.py").write_text("def lazy():\n    import helper\n")
    (tmp_path / "helper.py").write_text("from leaf import value\n")
    (tmp_path / "leaf.py").write_text("value = 1\n")
    assert local_import_closure(tmp_path, ["entry.py"]) == {"entry.py", "helper.py", "leaf.py"}


def test_payload_imports_from_extracted_artifact(tmp_path):
    import os
    import shutil
    import subprocess
    import sys
    payload = json.loads((ROOT / "app-payload.json").read_text(encoding="utf-8"))
    extracted = tmp_path / "dashboard"
    extracted.mkdir()
    for name in payload["files"]:
        shutil.copyfile(ROOT / name, extracted / name)
    scratch = tmp_path / "isolated-import"
    scratch.mkdir()
    env = dict(os.environ)
    for key, value in (("VIDEOSUITE_SETTINGS", "settings.json"), ("VIDEOSUITE_CALIBRATION", "calibration.json"),
                       ("VC_CACHE_DIR", "cache"), ("VIDEOLYZER_DATA_ROOT", "app"),
                       ("TORCH_HOME", "torch"), ("HF_HOME", "hf"), ("CLIP_CACHE_DIR", "clip")):
        env[key] = str(scratch / value)
    code = ("import sys,pathlib;sys.path.insert(0,sys.argv[1]);"
            "import main,app_routes,app_lifecycle;"
            "assert all(pathlib.Path(m.__file__).parent==pathlib.Path(sys.argv[1]) "
            "for m in (main,app_routes,app_lifecycle));print('PASS extracted lifecycle imports')")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(extracted)],
        capture_output=True, text=True, timeout=10, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert "PASS extracted lifecycle imports" in result.stdout
    assert not (scratch / "app").exists()


def test_exported_manifest_feeds_comparizer_release_validator(exported, tmp_path, monkeypatch):
    import hashlib
    import zipfile
    monkeypatch.syspath_prepend(r'D:\Video Comparizer\tests\unit')
    from test_app_launcher import release_tree, launcher
    root, pointer, manifest = release_tree(tmp_path)
    release = root / pointer['release']
    with zipfile.ZipFile(exported) as archive:
        component = json.loads(archive.read('dashboard-manifest.json'))
        archive.extractall(release / 'dashboard')
    # Assemble the real R1 release envelope around the actual emitted Dashboard rows.
    manifest['build_id'] = pointer['build_id'] = component['build_id']
    new = release.with_name(component['build_id'])
    release.rename(new)
    pointer['release'] = 'app/releases/' + component['build_id']
    manifest['frontend'] = component['frontend']
    manifest['backend'] = component['backend']
    manifest['sources']['dashboard'] = {k: component['source'][k] for k in ('revision', 'tree_hash')}
    manifest['locks'].update(component['locks'])
    manifest['files'] = [r for r in manifest['files'] if not r['path'].startswith('dashboard/')]
    manifest['files'] += [dict(row, path='dashboard/'+row['path']) for row in component['files']]
    raw = json.dumps(manifest).encode()
    (new / 'release.json').write_bytes(raw)
    pointer['manifest_sha256'] = hashlib.sha256(raw).hexdigest()
    (root / 'app/current.json').write_text(json.dumps(pointer))
    selected = launcher.resolve_release(root)
    assert selected['manifest']['sources']['dashboard'] == manifest['sources']['dashboard']
    (new / 'dashboard/app_host.py').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='size|hash'):
        launcher.resolve_release(root)


@pytest.fixture(scope='module')
def exported(tmp_path_factory):
    import hashlib
    import app_preflight
    root = tmp_path_factory.mktemp('export')
    # Use the actual current source snapshot; no stale L1 baseline can identify L6.
    import subprocess
    snapshot_tool = Path(r'D:\Video Comparizer\.superpowers\sdd\2026-10-18-plan4b-one-app\snapshot.py')
    subprocess.run([r'C:\Users\naman\AppData\Local\Programs\Python\Python310\python.exe', '-B',
        str(snapshot_tool), 'capture', '--repo', str(ROOT), '--output', str(root / 'snapshot.json')], check=True)
    revision = json.loads((root / 'snapshot.json').read_text())['revision']
    archive = app_preflight.export_payload(ROOT, root / 'out', 'l6-test', revision, root / 'snapshot.json')
    return archive


def test_payload_excludes_uploads_venvs_reports_movies_and_node_modules(exported):
    import zipfile
    with zipfile.ZipFile(exported) as archive:
        forbidden_entries = [p for p in archive.namelist() if any(
            c in {'uploads', '.venv', 'reports', 'node_modules', '__pycache__'} or
            Path(c).suffix.lower() in {'.mkv', '.mp4', '.avi'} for c in Path(p).parts)]
    assert forbidden_entries == []


def test_payload_manifest_matches_frontend_backend_and_lock_hashes(exported):
    import hashlib
    import zipfile
    with zipfile.ZipFile(exported) as archive:
        manifest = json.loads(archive.read('dashboard-manifest.json'))
        assert manifest['frontend'] == {'build_id': 'l6-test', 'entry': 'dist/index.html'}
        assert manifest['backend'] == {'build_id': 'l6-test', 'entry': 'app_host.py'}
        assert manifest['source']['revision'] == '4ef0b36d004ba291f8d15c4d3768b09dabfe81e6'
        assert set(archive.namelist()) == {r['path'] for r in manifest['files']} | {'dashboard-manifest.json'}
        for row in manifest['files']:
            data = archive.read(row['path'])
            assert row['size'] == len(data)
            assert row['sha256'] == hashlib.sha256(data).hexdigest()
        for key, name in [('dashboard_sha256', 'requirements-app-lock.txt'), ('npm_sha256', 'package-lock.json')]:
            assert manifest['locks'][key] == hashlib.sha256(archive.read(name)).hexdigest()


def test_payload_local_import_closure_and_extracted_backend_imports(exported, tmp_path):
    import os
    import subprocess
    import sys
    import zipfile
    with zipfile.ZipFile(exported) as archive:
        archive.extractall(tmp_path / 'extracted')
    root = tmp_path / 'extracted'
    payload = json.loads((root / 'app-payload.json').read_text())
    required = local_import_closure(root, ['main.py', 'app_host.py'])
    assert {'app_host.py', 'app_lifecycle.py', 'app_routes.py', 'app_preflight.py'} <= required
    assert_payload(payload['files'], required)
    for name in required:
        with pytest.raises(AssertionError):
            assert_payload([p for p in payload['files'] if p != name], required)
    code = ("import sys,pathlib;sys.path.insert(0,sys.argv[1]);import main,app_host,app_preflight;"
            "assert all(pathlib.Path(m.__file__).parent == pathlib.Path(sys.argv[1]) "
            "for m in (main,app_host,app_preflight));"
            "assert not any('Video Comparizer' in p or 'node_modules' in p for p in sys.path)")
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(root)], env=env,
                            cwd=root, capture_output=True, text=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
