"""Run-owned report registry: real bytes, no generic file serving."""
import hashlib
import subprocess
import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

import comparison_reports as reports
from test_comparison_routes import harness, plan_and_confirm, body
from test_comparison_runs import fixture4b


def register(tmp_path, name='pair.json', data=b'{"pair":true}'):
    reports.forget_run('run-safe')
    root = tmp_path / 'reports'
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(data)
    refs = reports.register_reports('run-safe', root, [path])
    assert len(refs) == 1
    return root, path, refs[0]


def test_report_id_cannot_escape_run_registry_via_traversal_or_junction(tmp_path):
    root, path, ref = register(tmp_path)
    for run, rid in [('run-other', ref['report_id']), ('run-safe', '../pair.json'), ('run-safe', str(path))]:
        with pytest.raises(HTTPException):
            reports.resolve_report(run, rid)
    outside = tmp_path / 'outside'
    outside.mkdir(); (outside / 'secret.json').write_text('private')
    with pytest.raises(HTTPException):
        reports.register_reports('run-safe', root, [outside / 'secret.json'])
    junction = root / 'jump'
    # Native NTFS junction, wholly within this test's owned scratch.
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(HTTPException):
            reports.register_reports('run-safe', root, [junction / 'secret.json'])
    finally:
        junction.rmdir()  # removes only this junction, never its target


def test_report_hash_or_symlink_swap_is_refused(tmp_path):
    root, path, ref = register(tmp_path)
    path.write_bytes(b'changed')
    with pytest.raises(HTTPException):
        reports.serve_report('run-safe', ref['report_id'])
    root, path, ref = register(tmp_path)
    replacement = tmp_path / 'replacement.json'
    replacement.write_bytes(path.read_bytes())
    path.unlink()
    # A same-byte replacement must still be refused by its file identity.
    replacement.replace(path)
    with pytest.raises(HTTPException):
        reports.serve_report('run-safe', ref['report_id'])


def test_html_report_is_sandboxed_and_cannot_call_privileged_api(tmp_path):
    _, _, ref = register(tmp_path, 'pair.html', b'<script>fetch("/app/quit")</script>')
    response = reports.serve_report('run-safe', ref['report_id'])
    csp = response.headers['content-security-policy']
    assert 'sandbox;' in csp and "default-src 'none'" in csp
    assert "script-src 'none'" in csp and "connect-src 'none'" in csp
    assert 'allow-same-origin' not in csp and 'allow-scripts' not in csp
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store'
    assert reports.serve_report('run-safe', ref['report_id'], download=True).headers['content-disposition'].startswith('attachment;')


@pytest.mark.parametrize('name', ['report.json', 'report.txt', 'worker.log'])
def test_non_html_is_served_as_text(tmp_path, name):
    _, _, ref = register(tmp_path, name, b'<script>bad()</script>')
    response = reports.serve_report('run-safe', ref['report_id'])
    assert response.headers['content-type'].startswith('text/plain')
    assert response.body == b'<script>bad()</script>'


def test_real_compare_outputs_and_serialized_urls_resolve_registry(client, harness):
    started, _ = plan_and_confirm(client, body('both', 'full'))
    detail = client.get('/comparison/runs/' + started['run_id']).json()
    refs = detail['result']['reports']
    assert len(refs) == len(fixture4b('compare_all')['pairs'])
    for ref in refs:
        response = client.get(ref['url'])
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == ref['sha256']
        assert client.get(ref['url'] + '?download=1').headers['content-disposition'].startswith('attachment;')
        assert client.get(ref['url'] + '?path=C:/private').status_code == 422
        assert client.get(ref['url'] + '?download=2').status_code == 422


def test_actual_u4_pair_writer_bytes_survive_http_registry(client, harness, tmp_path):
    root = tmp_path / 'actual-emitter'
    code = ("import sys,json;from pathlib import Path;"
            "sys.path.insert(0,r'D:\\Video Comparizer\\tests\\unit');"
            "from test_videosuite_cli import emit_plan4b_documents;"
            "print(json.dumps(emit_plan4b_documents(Path(sys.argv[1]))))")
    emitted = subprocess.run([os.environ['VIDEOSUITE_PYTHON'], '-c', code, str(root)], cwd='D:/Video Comparizer',
                             capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert emitted.returncode == 0, emitted.stderr
    docs = json.loads(emitted.stdout)
    assert docs['compare_all'] == fixture4b('compare_all')
    expected = {}
    def compare(kwargs):
        out = Path(kwargs['out_dir'])
        out.mkdir(parents=True)
        doc = json.loads(json.dumps(docs['compare_all']))
        for pair in doc['pairs']:
            name = Path(pair['report']).name
            source = root / 'full' / 'reports' / name
            assert source.is_file()
            for original in (source, source.with_suffix('.log')):
                if original.is_file():
                    target = out / original.name
                    shutil.copyfile(original, target)
                    expected[target.name] = target.read_bytes()
            pair['report'] = str(out / name)
        return doc
    harness.hooks['compare'] = compare
    started, _ = plan_and_confirm(client, body('both', 'full'))
    detail = client.get('/comparison/runs/' + started['run_id']).json()
    assert detail['run']['stage'] == 'done', detail
    refs = detail['result']['reports']
    assert {r['name'] for r in refs} == set(expected)
    for ref in refs:
        response = client.get(ref['url'])
        assert response.status_code == 200
        assert response.content == expected[ref['name']]
        assert hashlib.sha256(response.content).hexdigest() == ref['sha256']


def test_registered_root_junction_swap_is_refused(tmp_path):
    root, path, ref = register(tmp_path)
    saved = tmp_path / 'saved-reports'
    # Both resolved absolute directories are verified children of owned tmp_path.
    assert root.resolve().parent == saved.resolve().parent == tmp_path.resolve()
    root.rename(saved)
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(root), str(saved)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(HTTPException):
            reports.serve_report('run-safe', ref['report_id'])
    finally:
        root.rmdir()


def test_known_html_and_text_companions_come_from_actual_pair_registration(client, harness):
    import comparison_runs as cr
    started, _ = plan_and_confirm(client, body('overall', 'full'))
    run_id = started['run_id']
    root = Path(cr.store.get(run_id)['reports_dir'])
    first = next(root.glob('*.json'))
    html, text = first.with_suffix('.html'), first.with_suffix('.txt')
    html.write_text('<script>fetch("/app/quit")</script>')
    text.write_text('Detailed synthetic pair numbers')
    cr._register_reports(run_id, {'pairs': [{'report': str(first)}]})
    refs = cr._report_refs(run_id)
    for ref in refs:
        response = client.get(ref['url'])
        assert response.status_code == 200
        if ref['kind'] == 'html':
            assert 'sandbox;' in response.headers['content-security-policy']
        if ref['kind'] == 'text':
            assert response.text == 'Detailed synthetic pair numbers'
    assert {'html', 'text'} <= {r['kind'] for r in refs}
