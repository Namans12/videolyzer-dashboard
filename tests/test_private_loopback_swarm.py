"""Opt-in real libtorrent in an isolated interpreter; never use public torrents.

PLAN4B_LOOPBACK_PYTHON must name an existing Python with libtorrent installed.
Normal Dashboard pytest skips this integration proof, rather than faking it.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.environ.get('PLAN4B_LOOPBACK_PYTHON'), reason='opt-in private loopback libtorrent proof')
def test_private_loopback_swarm_only_downloads_selected_probe_pieces(tmp_path):
    binding_python = os.environ['PLAN4B_LOOPBACK_PYTHON']
    result = subprocess.run([binding_python, str(Path(__file__).resolve()), '--helper', str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PRIVATE LOOPBACK PASS' in result.stdout
    print(result.stdout)


def _helper(root):
    import gc
    import json
    import time
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import libtorrent as lt
    import magnet
    import magnet_suite
    from suite_client import SuiteClient, validate_document

    root = Path(root)
    source = root / 'seed'
    bundle = source / 'PrivateFilm'
    bundle.mkdir(parents=True)
    for ext in ('mkv', 'mp4'):
        path = bundle / ('Film.' + ext)
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=24', '-t', '2', '-c:v', 'libx264', '-preset',
                        'ultrafast', '-y', str(path)], check=True, capture_output=True, timeout=15)
        # Tiny valid video plus harmless trailing bytes: each file exceeds probe bounds.
        if ext == 'mkv':
            with path.open('ab') as out:
                out.write(b'\0' * (1024 * 1024))
    fs = lt.file_storage()
    for path in sorted(bundle.iterdir()):
        fs.add_file('PrivateFilm/' + path.name, path.stat().st_size)
    creator = lt.create_torrent(fs, 16 * 1024, lt.create_torrent.v1_only)
    creator.set_priv(True)
    lt.set_piece_hashes(creator, str(source))
    ti = lt.torrent_info(creator.generate())
    assert ti.priv() and not list(ti.trackers()) and not list(ti.web_seeds())
    uri = lt.make_magnet_uri(ti)
    assert '&tr=' not in uri and '&ws=' not in uri

    def settings(port=0):
        return dict(listen_interfaces=f'127.0.0.1:{port}', outgoing_interfaces='127.0.0.1',
                    enable_dht=False, dht_bootstrap_nodes='', enable_lsd=False,
                    enable_upnp=False, enable_natpmp=False, enable_incoming_utp=False,
                    enable_outgoing_utp=False, alert_mask=lt.alert.category_t.all_categories)

    def confined_session(config):
        assert config['enable_dht'] is False and config['enable_lsd'] is False
        session = lt.session(config)
        ip = lt.ip_filter()
        ip.add_rule('0.0.0.0', '255.255.255.255', 1)
        ip.add_rule('::', 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff', 1)
        ip.add_rule('127.0.0.1', '127.0.0.1', 0)
        session.set_ip_filter(ip)
        return session

    seed = confined_session(settings())
    downloads, workdirs = [], []
    params = lt.add_torrent_params()
    params.ti, params.save_path = ti, str(source)
    params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
    seed_handle = seed.add_torrent(params)
    end = time.monotonic() + 10
    while not seed_handle.status().is_seeding:
        assert time.monotonic() < end, 'seed never became ready'
        time.sleep(.05)
    port = seed.listen_port()
    assert port > 0
    def parse(magnet_uri):
        params = lt.parse_magnet_uri(magnet_uri)
        # Private torrents intentionally do not advertise ut_metadata. Supply
        # our generated metadata locally; payload still transfers over real TCP.
        params.ti = ti
        params.peers = [('127.0.0.1', port)]
        params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
        return params
    class Session:
        delete_files = lt.session.delete_files
        def __init__(self, config):
            self.real = confined_session(config)
            self.handle = None
            downloads.append(self)
        def __getattr__(self, name):
            return getattr(self.real, name)
        def add_torrent(self, params):
            workdirs.append(Path(params.save_path))
            self.handle = self.real.add_torrent(params)
            self.handle.connect_peer(('127.0.0.1', port))
            return self.handle
        def pop_alerts(self):
            alerts = self.real.pop_alerts()
            if os.environ.get('PLAN4B_SWARM_DEBUG'):
                for a in alerts:
                    print(type(a).__name__, a.message(), flush=True)
            return alerts
    # Only transport bootstrap is confined/injected. The production downloader,
    # priorities, disk/probe retention, partial_facts and Quick CLI are real.
    binding = SimpleNamespace(session=Session, parse_magnet_uri=parse,
                              torrent_flags=lt.torrent_flags, alert=lt.alert,
                              storage_mode_t=lt.storage_mode_t)
    magnet._load_libtorrent = lambda: binding
    magnet._DHT_ROUTERS = []
    magnet.session_settings = settings
    magnet.HEAD_BYTES, magnet.TAIL_BYTES = 64 * 1024, 32 * 1024
    magnet.METADATA_TIMEOUT_S, magnet.PIECE_TIMEOUT_S = 10, 10
    classify = magnet._classify_file
    # Admit tiny generated clips through the legacy 50 MiB anti-sample filter;
    # all real sizes, offsets, disk bytes and piece counts remain unchanged.
    magnet._classify_file = lambda name, size: classify(name, max(size, 50 * 1024 * 1024))
    client = SuiteClient(argv_prefix=[sys.executable, '-m', 'videosuite'], root=os.environ['VIDEOSUITE_ROOT'])
    detached, observed = [], []
    def facts(rows, emit, cancel):
        handle = downloads[-1].handle
        entry = rows[0]
        index = next(i for i in range(ti.num_files()) if ti.files().file_path(i).endswith(entry['real_name']))
        original = source / ti.files().file_path(index)
        retained = Path(entry['path'])
        assert retained.read_bytes()[:magnet.HEAD_BYTES] == original.read_bytes()[:magnet.HEAD_BYTES]
        if original.suffix == '.mp4':
            assert retained.read_bytes()[-magnet.TAIL_BYTES:] == original.read_bytes()[-magnet.TAIL_BYTES:]
        wanted = set(magnet._pieces_for_file(ti.piece_length(), ti.files().file_offset(index),
                                            ti.files().file_size(index), original.suffix, False))
        priorities = handle.get_piece_priorities()
        assert handle.get_file_priorities() == [1 if i == index else 0 for i in range(ti.num_files())]
        assert {i for i, p in enumerate(priorities) if p} == wanted
        assert {i for i in range(ti.num_pieces()) if handle.have_piece(i)} == wanted
        assert handle.status().total_payload_download > 0
        assert all(p.ip[0] == '127.0.0.1' for p in handle.get_peer_info())
        result = magnet_suite.partial_facts(rows, emit, cancel, client=client)
        assert len(result) == 1 and 'record' in result[0], result
        record = result[0]['record']
        assert record['source']['partial'] and record['video']['codec'] in ('h264', 'avc', 'AVC'), record
        assert record['container']['size_bytes'] == original.stat().st_size
        detached.extend(result)
        observed.append(dict(entry=entry['real_name'], pieces=sorted(wanted), total_pieces=ti.num_pieces()))
        return {entry['key']: dict(status='ok', record=record)}
    try:
        for index in range(ti.num_files()):
            doc = magnet.run_magnet_job_threaded(uri, True, lambda m: None, lambda: False,
                selected_file_index=index, use_default_scorer=False, partial_scorer=facts, listen_port=0)
            assert len(doc['analyses']) == 1 and len(detached) == index + 1
            assert not workdirs[-1].exists()
        for alternatives in (False, True):
            out = magnet_suite.recommend_partials(detached, lambda m: None, lambda: False,
                results='both', device='sony_bravia_8_ii', primary_player='just_player',
                player='all' if alternatives else 'just_player', fs=None, lang='eng', audio_output=None,
                client_factory=lambda: client)
            quick = next(iter(out.values()))['recommendation']
            validate_document('quick', quick)
            assert quick['selected']['player'] == 'just_player' and quick['overall'] is not None
            assert len(quick['alternatives']) == (2 if alternatives else 0)
        # Native cancellation after metadata/priority installation and finite piece
        # timeout both traverse the actual session cleanup path.
        for ending in ('cancel', 'timeout'):
            phase = [False]
            timed_out = []
            def emit(message):
                if message.startswith('Downloading '): phase[0] = True
                if 'Timed out' in message: timed_out.append(message)
            if ending == 'cancel':
                try:
                    magnet.run_magnet_job_threaded(uri, True, emit, lambda: phase[0],
                        selected_file_index=0, use_default_scorer=False, partial_scorer=facts, listen_port=0)
                except RuntimeError as exc:
                    assert 'Cancelled' in str(exc)
                else:
                    raise AssertionError('cancellation did not stop downloader')
            else:
                magnet.PIECE_TIMEOUT_S = -1
                magnet.run_magnet_job_threaded(uri, True, emit, lambda: False,
                    selected_file_index=0, use_default_scorer=False, partial_scorer=lambda *a: {}, listen_port=0)
                assert timed_out, 'piece timeout path was not exercised'
            assert not workdirs[-1].exists()
        # Suite budget exhaustion with actual detached swarm facts preserves facts.
        out = magnet_suite.recommend_partials(detached, lambda m: None, lambda: False,
            results='overall', device=None, primary_player=None, player=None, fs=None, lang='eng',
            audio_output=None, client_factory=lambda: client, clock=iter([0, 301]).__next__)
        assert all(v['error_kind'] == 'timeout' and v['record'] for v in out.values())
        print('PRIVATE LOOPBACK PASS', lt.version, json.dumps(observed), flush=True)
    finally:
        for session in downloads:
            session.pause()
            if session.handle.is_valid():
                session.remove_torrent(session.handle, lt.session.delete_files)
            session.handle = None
            session.real = None
        downloads.clear()
        seed.remove_torrent(seed_handle)
        seed.pause()
        seed_handle = seed = None
        gc.collect()


if __name__ == '__main__':
    assert sys.argv[1] == '--helper'
    _helper(sys.argv[2])
