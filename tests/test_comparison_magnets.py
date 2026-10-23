"""Private synthetic magnets only; no public peers or owner media."""
import copy
import inspect
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

import comparison_contracts as cc
import comparison_runs as cr
import magnet
import magnet_suite as ms
from suite_client import SuiteUnavailable
from test_comparison_runs import FakeSuite, fixture4b, seams, planned, confirmed
from test_magnet_session import _FakeLtFull
from test_comparison_routes import harness, pin
from types import SimpleNamespace

MAGNET = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567'
MAGNET2 = 'magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01'


def request(results='overall', **over):
    raw = dict(input_kind='magnets', results=results, depth='quick',
               magnets=[dict(uri=uri, file_index=None) for uri in (MAGNET, MAGNET2)])
    if results != 'overall':
        raw.update(device='sony_bravia_8_ii', primary_player='just_player', player='all')
    raw.update(over)
    return cc.validate_run_request(raw)


def entries():
    return [dict(key=str(i), path=r['source']['path'], real_name=r['source']['real_name'],
                 real_size=r['container']['size_bytes'], coverage='head', record=copy.deepcopy(r))
            for i, r in enumerate(fixture4b('quick_partial')['records'])]


class PartialSuite(FakeSuite):
    def quick(self, snapshot, **kwargs):
        self._call('quick', dict(snapshot=snapshot, **kwargs))
        doc = fixture4b('quick_partial')
        if kwargs['results'] == 'overall':
            doc.update(results='overall', devices={}, selected=dict(device=None, player=None), alternatives=[])
        return doc


def recommend(client, rows=None, **over):
    opts = dict(results='overall', device=None, primary_player=None, player=None, fs=None, lang='eng', audio_output=None)
    opts.update(over)
    return ms.recommend_partials(rows or entries(), lambda s: None, lambda: False,
                                 client_factory=lambda: client, **opts)


def test_overall_magnets_never_call_score_partials_or_load_devices(monkeypatch):
    monkeypatch.setattr(ms, 'score_partials', lambda *a, **k: pytest.fail('legacy scorer'))
    client = PartialSuite()
    out = recommend(client)
    assert all(v['status'] == 'ok' for v in out.values())
    assert client.work() == ['quick']
    assert not any(n == 'devices' for n, _ in client.calls)
    assert client.kwargs('quick')['device'] is None


@pytest.mark.parametrize('device,player', [('vivobook_pro_16x', 'mpv'), ('galaxy_tab_s9_ultra', 'just_player')])
def test_selected_laptop_tablet_ids_and_players_reach_partial_recommendation(device, player):
    client = PartialSuite()
    recommend(client, results='device', device=device, primary_player=player, player=player)
    assert (client.kwargs('quick')['device'], client.kwargs('quick')['primary_player'],
            client.kwargs('quick')['player']) == (device, player, player)


def test_missing_suite_never_substitutes_legacy_bravia_winner():
    class Missing(PartialSuite):
        def quick(self, *a, **k):
            raise SuiteUnavailable('missing')
    out = recommend(Missing())
    for row in out.values():
        assert row['recommendation'] is None
        assert row['record']['source']['partial'] is True
        assert row['status'] == 'unavailable'


def test_partial_failures_remain_in_result_and_prevent_whole_set_winner():
    client = PartialSuite()
    rows = entries()
    rows[1].pop('record')
    rows[1]['error'] = dict(status='error', error_kind='probe', message='unreadable header')
    out = recommend(client, rows)
    assert out['1']['status'] == 'error'
    snap = client.kwargs('quick')['snapshot']
    assert snap['candidates'][1]['status'] == 'unreadable'


def test_new_magnet_arguments_are_keyword_only():
    for fn in (magnet.fetch_magnet_metadata, magnet.run_magnet_job_threaded):
        for key in ('partial_scorer', 'use_default_scorer', 'selected_file_index', 'metadata_only'):
            assert inspect.signature(fn).parameters[key].kind == inspect.Parameter.KEYWORD_ONLY


def test_multivideo_torrent_requires_explicit_entry_without_grouping(monkeypatch):
    lt = _FakeLtFull()
    lt.torrent_flags = SimpleNamespace(upload_mode=1)
    lt.parse_magnet_uri = lambda uri: SimpleNamespace(flags=0)
    original = lt.session
    class Session(original):
        def add_torrent(self, params):
            handle = super().add_torrent(params)
            info = handle.torrent_file()
            info.num_files = lambda: 2
            handle.torrent_file = lambda: info
            handle.prioritize_files = lambda *a: pytest.fail('metadata discovery downloaded pieces')
            return handle
    lt.session = Session
    lt.session.delete_files = 1
    monkeypatch.setattr(magnet, '_load_libtorrent', lambda: lt)
    monkeypatch.setattr(magnet, '_check_temp_disk_space', lambda p: None)
    monkeypatch.setattr(magnet.time, 'sleep', lambda s: None)
    out = magnet.fetch_magnet_metadata(MAGNET, metadata_only=True, use_default_scorer=False)
    assert len(out['files']) == 2 and out['analyses'] == []
    assert not Path(lt.workdir).exists()


def test_quick_has_no_middle_sampling_deep_dv_or_full_download(monkeypatch):
    lt = _FakeLtFull()
    lt.torrent_flags = SimpleNamespace(upload_mode=1)
    lt.parse_magnet_uri = lambda uri: SimpleNamespace(flags=0)
    original_session = lt.session
    order = []
    class Session(original_session):
        def add_torrent(self, params):
            assert params.flags == 1
            handle = super().add_torrent(params)
            handle.unset_flags = lambda flags: order.append('resume')
            def files(priorities):
                assert priorities == [1, 0], 'only the selected file must be writable'
                order.append('files')
            handle.prioritize_files = files
            info = handle.torrent_file()
            info.num_files = lambda: 2
            info.num_pieces = lambda: 5120
            info.file_offset = lambda i: i * 40 * 2**30
            handle.torrent_file = lambda: info
            def pieces(priorities):
                assert priorities == [0] * 5120, 'all non-probe pieces must be disabled'
                order.append('zero')
            handle.prioritize_pieces = pieces
            handle.piece_priority = lambda p, priority: order.append((p, priority))
            # Simulate asynchronous file-priority completion, before piece overrides.
            self.pop_alerts = lambda: [type('file_prio_alert', (), {'handle': handle})(),
                                      type('cache_flushed_alert', (), {'handle': handle})()]
            handle.flush_cache = lambda: order.append('flush')
            return handle
    lt.session = Session
    lt.session.delete_files = 1
    monkeypatch.setattr(magnet, '_load_libtorrent', lambda: lt)
    monkeypatch.setattr(magnet, '_check_temp_disk_space', lambda p: None)
    monkeypatch.setattr(magnet.time, 'sleep', lambda s: None)
    monkeypatch.setattr(magnet.proc_guard, 'run_tool', lambda *a, **k: type('R', (), {'returncode': 0})())
    monkeypatch.setattr(magnet, 'analyze_file', lambda *a, **k: pytest.fail('legacy probe/scoring'))
    seen = []
    def facts(rows, emit, cancel):
        seen.extend(rows)
        assert Path(rows[0]['path']).is_file()
        return {rows[0]['key']: {'status': 'ok', 'record': entries()[0]['record']}}
    out = magnet.fetch_magnet_metadata(MAGNET, selected_file_index=0, use_default_scorer=False,
                                      partial_scorer=facts)
    assert out['analyses'][0]['suite']['status'] == 'ok'
    assert seen[0]['real_size'] == 40 * 2**30
    assert not Path(seen[0]['path']).exists()
    assert order == ['files', 'zero', (0, 7), (1, 7), 'resume', 'flush']


def test_magnet_full_and_more_than_eight_links_rejected_before_torrent_work():
    with pytest.raises(cc.ContractError):
        request(depth='full')
    with pytest.raises(cc.ContractError):
        request(magnets=[dict(uri=MAGNET, file_index=0)] * 9)


EMIT = r'''
import contextlib, copy, io, json, os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, r'D:\Video Comparizer')
sys.path.insert(0, r'D:\Video Comparizer\tests\unit')
from videosuite import cli
from test_videosuite_comparison_plan import fixed_records, PATHS
mode, root, payload = sys.argv[1], Path(sys.argv[2]), json.loads(sys.argv[3])
if mode == 'facts':
    i = payload['i']
    r = fixed_records()[i]
    p = PATHS[i]
    with patch.object(cli, '_gather_paths', return_value=[p]), patch.object(cli, '_records', side_effect=lambda paths, build, *a: [build(p)]), patch('os.path.getsize', return_value=512):
        assert cli.main(['facts', p, '--real-size', str(payload['real_size']), '--real-name', payload['real_name'], '--coverage', payload['coverage'], '--json'], build_fn=lambda p: copy.deepcopy(r)) == 0
else:
    transport = root / 'detached.json'
    transport.write_text(json.dumps(payload['snapshot']), encoding='utf-8')
    opts = payload['selection']
    args = ['quick', '--records', str(transport), '--results', opts['results'], '--json']
    for key in ('device', 'primary_player', 'player', 'fs', 'lang', 'audio_output'):
        if opts.get(key) is not None:
            args += ['--' + key.replace('_', '-'), opts[key]]
    assert cli.main(args) == 0
'''


class RealPartialSuite(FakeSuite):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.failed = None

    def emit(self, mode, payload):
        result = subprocess.run([os.environ['VIDEOSUITE_PYTHON'], '-c', EMIT, mode, str(self.root), json.dumps(payload)],
                                cwd='D:/Video Comparizer', capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stderr
        from suite_client import parse_document
        return parse_document('facts' if mode == 'facts' else 'quick', result.stdout.encode('utf-8'))

    def facts_partial(self, path, **kwargs):
        i = ord(kwargs['real_name'][0]) - ord('A')
        self._call('facts_partial', dict(path=path, **kwargs))
        if self.failed == i:
            raise RuntimeError('bad synthetic header')
        return self.emit('facts', dict(i=i, **{k: kwargs[k] for k in ('real_size', 'real_name', 'coverage')}))

    def quick(self, snapshot, **kwargs):
        self._call('quick', dict(snapshot=snapshot, **kwargs))
        return self.emit('quick', dict(snapshot=snapshot, selection={k: v for k, v in kwargs.items() if k != 'cancel'}))


def fake_torrent(monkeypatch, root, *, multiple=False):
    calls = []
    original_facts = ms.partial_facts
    def controlled_facts(*args, **kwargs):
        rows = original_facts(*args, **kwargs)
        # Test-only facts identity seam: failures use the same virtual submitted identity
        # as real record.new_record. No post-emission id/digest normalization.
        for row in rows:
            if 'error' in row:
                row['path'] = 'C:\\plan4b-fixture-inputs\\' + row['real_name']
        return rows
    monkeypatch.setattr(ms, 'partial_facts', controlled_facts)
    def torrent(uri, skip_dovi, emit, cancel, **kwargs):
        calls.append(kwargs)
        i = 0 if uri == MAGNET else 1
        files = [dict(index=i, name=chr(65+i)+'.mkv', size_bytes=(i+1)*40960, verdict='good')]
        if multiple:
            files += [dict(index=2, name='C.mkv', size_bytes=122880, verdict='good')]
        if not kwargs.get('metadata_only'):
            assert kwargs['use_default_scorer'] is False and kwargs['selected_file_index'] == i
            probe = root / (chr(65+i)+'.mkv')
            probe.write_bytes(b'x' * 512)
            try:
                kwargs['partial_scorer']([dict(key=files[0]['name'], path=str(probe), real_name=files[0]['name'],
                                                real_size=files[0]['size_bytes'], coverage='head')], emit, cancel)
            finally:
                probe.unlink()
        return dict(files=files, analyses=[])
    monkeypatch.setattr(magnet, 'run_magnet_job_threaded', torrent)
    return calls


@pytest.mark.parametrize('results', ['overall', 'device'])
def test_partial_just_player_primary_with_all_alternatives_reaches_real_quick_emitter(client, harness, tmp_path, monkeypatch, results):
    import main
    from suite_client import get_suite_client
    real = RealPartialSuite(tmp_path)
    main.app.dependency_overrides[get_suite_client] = lambda: real
    calls = fake_torrent(monkeypatch, tmp_path)
    raw = request(results, magnets=[dict(uri=MAGNET, file_index=0),
                  dict(uri='magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01', file_index=1)])
    started = client.post('/comparison/plans', json=raw).json()
    url = '/comparison/runs/' + started['run_id']
    snap = client.get(url).json()['run']
    assert snap['stage'] == 'awaiting_confirmation'
    assert len(calls) == 2 and all(c['metadata_only'] for c in calls)
    response = client.post('/comparison/runs', json=dict(run_id=snap['run_id'], plan_revision=snap['plan_revision']))
    assert response.status_code == 200, response.text
    detail = client.get(url).json()
    result = detail['result']
    assert result and detail['run']['stage'] == 'done', json.dumps(detail, indent=2)
    assert result['facts'][0]['source']['partial'] is True
    assert result['facts'][0]['container']['size_bytes'] == 40960
    assert result['facts'][0]['source']['probed_bytes'] == 512
    assert len([n for n, k in real.calls if n == 'quick']) == 1
    assert not any(n in ('score', 'score_partial') for n, k in real.calls)
    if results == 'device':
        assert result['device']['selected']['player'] == 'just_player'
        assert len(result['device']['alternatives']) == 2
        # Only the physical settings snapshot prefix may be normalized after real emission.
        doc = result['device']
        if doc.get('settings'):
            doc['settings']['path'] = '<SCRATCH>\\settings.json'
    else:
        assert result['device'] is None
        assert not any(n == 'devices' for n, k in real.calls)
    cc.validate_run_result(result)
    pin('magnet_quick_' + results + '.json', detail)


def test_selection_required_and_partial_failure_http_fixtures(client, harness, tmp_path, monkeypatch):
    import main
    from suite_client import get_suite_client
    real = RealPartialSuite(tmp_path)
    main.app.dependency_overrides[get_suite_client] = lambda: real
    calls = fake_torrent(monkeypatch, tmp_path, multiple=True)
    started = client.post('/comparison/plans', json=request()).json()
    url = '/comparison/runs/' + started['run_id']
    detail = client.get(url).json()
    assert detail['run']['stage'] == 'selection_required' and len(calls) == 2
    assert client.post('/comparison/runs', json=dict(run_id=started['run_id'], plan_revision=detail['run']['plan_revision'])).status_code == 409
    pin('magnet_selection_required.json', detail)
    fake_torrent(monkeypatch, tmp_path)
    real.failed = 1
    raw = request(magnets=[dict(uri=MAGNET, file_index=0), dict(uri='magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01', file_index=1)])
    started = client.post('/comparison/plans', json=raw).json()
    url = '/comparison/runs/' + started['run_id']
    snap = client.get(url).json()['run']
    client.post('/comparison/runs', json=dict(run_id=snap['run_id'], plan_revision=snap['plan_revision']))
    detail = client.get(url).json()
    result = detail['result']
    assert result['coverage']['complete'] is False and result['overall']['winner_ids'] == []
    assert result['snapshot']['candidates'][1]['status'] == 'failed'
    assert result['failures'] and len(result['facts']) == 1
    pin('magnet_partial_failure.json', detail)


def test_partial_source_size_coverage_and_unknown_facts_survive_cleanup(tmp_path):
    client = RealPartialSuite(tmp_path)
    rows = entries()[:2]
    for row in rows:
        row.pop('record')
    out = recommend(client, rows)
    for row in out.values():
        record = row['record']
        assert record['source']['partial'] and record['source']['caveats']
        assert record['source']['probed_bytes'] == 512
        assert record['container']['size_bytes'] > 512
    assert out['0']['recommendation']['measurement'] == 'metadata'


def test_magnet_cancel_or_outer_timeout_closes_session_and_cleans_retained_probes(monkeypatch):
    import threading
    entered, exited = threading.Event(), threading.Event()
    def fetch(*a, **kwargs):
        entered.set()
        while not kwargs['cancel_check']():
            exited.wait(.001)
        exited.set()
        raise RuntimeError('Cancelled')
    monkeypatch.setattr(magnet, 'fetch_magnet_metadata', fetch)
    for name in ('METADATA_TIMEOUT_S', 'PIECE_TIMEOUT_S', 'PROBE_MAX_S'):
        monkeypatch.setattr(magnet, name, -10)
    with pytest.raises(TimeoutError):
        magnet.run_magnet_job_threaded(MAGNET, True, lambda s: None, lambda: False, use_default_scorer=False)
    assert entered.is_set() and exited.is_set()


def test_false_alternatives_selects_only_just_player_in_real_emitter(tmp_path):
    real = RealPartialSuite(tmp_path)
    out = recommend(real, results='device', device='sony_bravia_8_ii', primary_player='just_player', player='just_player')
    doc = out['0']['recommendation']
    assert doc['selected']['player'] == 'just_player'
    assert list(doc['devices']) == ['sony_bravia_8_ii/just_player'] and doc['alternatives'] == []


@pytest.mark.parametrize('cancelled,kind,message', [(True, 'failed', 'cancelled'),
                                                 (False, 'timeout', 'time budget (300 s) used up')])
def test_partial_budget_and_cancel_preserve_explicit_errors(cancelled, kind, message):
    client = PartialSuite()
    rows = entries()[:1]
    if cancelled:
        clock = lambda: 0.0
    else:
        ticks = iter([0.0, 301.0])
        clock = lambda: next(ticks, 301.0)
    out = ms.recommend_partials(rows, lambda s: None, lambda: cancelled, results='overall',
                               device=None, primary_player=None, player=None, fs=None, lang='eng',
                               audio_output=None, client_factory=lambda: client, clock=clock)
    assert out['0']['error_kind'] == kind and message in out['0']['message']
    assert out['0']['record'] == rows[0]['record'] and out['0']['recommendation'] is None
    assert client.work() == []
    rows[0].pop('record')
    facts = ms.partial_facts(rows, lambda s: None, lambda: cancelled, client=client,
                             clock=lambda: 0.0 if cancelled else 301.0, start=0.0)
    assert facts[0]['error']['error_kind'] == kind and message in facts[0]['error']['message']


@pytest.mark.parametrize('fail', [None, 'metadata', 'probe'])
def test_unified_metadata_and_probe_lease_ports_alongside_legacy(seams, tmp_path, monkeypatch, fail):
    calls = fake_torrent(monkeypatch, tmp_path)
    original = magnet.run_magnet_job_threaded
    legacy = magnet.port_allocator.acquire()
    seen = []
    def torrent(*args, **kwargs):
        port = kwargs.get('listen_port')
        assert port is not None and port != legacy
        assert port in magnet.port_allocator.in_use()
        seen.append(port)
        if fail == ('metadata' if kwargs.get('metadata_only') else 'probe'):
            raise RuntimeError('synthetic network failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(magnet, 'run_magnet_job_threaded', torrent)
    try:
        run_id = planned(request(), RealPartialSuite(tmp_path))
        if fail != 'metadata':
            client = RealPartialSuite(tmp_path)
            job_id, lease = confirmed(run_id, client, slot='score_rank')
            cr.execute_run(run_id, client, lease=lease)
            assert len(seen) == 4
        assert seen and magnet.port_allocator.in_use() == {legacy}
    finally:
        magnet.port_allocator.release(legacy)


def test_both_magnets_keep_overall_when_settings_change_while_reading_facts(seams, tmp_path, monkeypatch):
    import jobs
    fake_torrent(monkeypatch, tmp_path)
    settings = tmp_path / 'owner.json'
    settings.write_text('{}')
    client = RealPartialSuite(tmp_path)
    client.settings_path = str(settings)
    original = client.facts_partial
    def facts(*args, **kwargs):
        result = original(*args, **kwargs)
        settings.write_text('{"changed":true}')
        return result
    monkeypatch.setattr(client, 'facts_partial', facts)
    run_id = planned(request('both'), client)
    job_id, lease = confirmed(run_id, client, slot='score_rank')
    cr.execute_run(run_id, client, lease=lease)
    result = cr.store.result(run_id)
    assert jobs.jobs.get(job_id)['error_kind'] == 'settings_changed'
    assert result['overall'] is not None and result['device'] is None and result['coverage']['complete']
    assert client.kwargs('quick')['results'] == 'overall'
