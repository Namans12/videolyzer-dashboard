"""Opt-in public ut_metadata proof, confined to loopback; no public network.

PLAN4B_PUBLIC_LOOPBACK_PYTHON names an existing Python with libtorrent.
The helper never supplies torrent metadata to the downloader's parse seam.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.environ.get('PLAN4B_PUBLIC_LOOPBACK_PYTHON'), reason='opt-in public loopback proof')
def test_public_magnet_metadata_and_selected_quick(tmp_path):
    result = subprocess.run([os.environ['PLAN4B_PUBLIC_LOOPBACK_PYTHON'], str(Path(__file__).resolve()),
                             '--helper', str(tmp_path)], capture_output=True, text=True,
                            encoding='utf-8', timeout=150, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PUBLIC LOOPBACK PASS' in result.stdout


def _helper(root):
    import gc
    import time
    from types import SimpleNamespace
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import libtorrent as lt
    import magnet
    import magnet_suite
    from suite_client import SuiteClient, validate_document

    root = Path(root)
    source = root / 'seed'
    source.mkdir(parents=True)
    clip = source / 'Generated.mkv'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=24', '-t', '1', '-c:v', 'libx264', '-preset',
                    'ultrafast', '-y', str(clip)], check=True, capture_output=True, timeout=15)
    with clip.open('ab') as out:
        out.write(b'\0' * (1024 * 1024))
    fs = lt.file_storage()
    fs.add_file(clip.name, clip.stat().st_size)
    creator = lt.create_torrent(fs, 16 * 1024, lt.create_torrent.v1_only)
    creator.set_priv(False)
    lt.set_piece_hashes(creator, str(source))
    ti = lt.torrent_info(creator.generate())
    assert not ti.priv() and not list(ti.trackers()) and not list(ti.web_seeds())
    original_settings = magnet.session_settings

    def settings(port):
        # Preserve production tuning except transport confinement. Both peers use
        # an IP filter as a second barrier; no DNS bootstrap or public discovery.
        config = original_settings(port if port else 6881)
        config.update(listen_interfaces=f'127.0.0.1:{port}', outgoing_interfaces='127.0.0.1',
                      enable_dht=False, dht_bootstrap_nodes='', enable_lsd=False,
                      enable_upnp=False, enable_natpmp=False, enable_incoming_utp=False,
                      enable_outgoing_utp=False, alert_mask=lt.alert.category_t.all_categories)
        return config

    def confined_session(config):
        assert config['enable_dht'] is False and config['dht_bootstrap_nodes'] == ''
        assert config['listen_interfaces'].startswith('127.0.0.1:')
        session = lt.session(config)
        ip = lt.ip_filter()
        ip.add_rule('0.0.0.0', '255.255.255.255', 1)
        ip.add_rule('::', 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff', 1)
        ip.add_rule('127.0.0.1', '127.0.0.1', 0)
        session.set_ip_filter(ip)
        return session

    seed = confined_session(settings(0))
    params = lt.add_torrent_params()
    params.ti, params.save_path = ti, str(source)
    params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
    seed_handle = seed.add_torrent(params)
    downloads, workdirs, alerts_seen = [], [], []
    deadline = time.monotonic() + 10
    while not seed_handle.status().is_seeding:
        assert time.monotonic() < deadline, 'local seed not ready'
        time.sleep(.05)
    uri = f'magnet:?xt=urn:btih:{ti.info_hash()}&x.pe=127.0.0.1:{seed.listen_port()}'

    class Session:
        delete_files = lt.session.delete_files

        def __init__(self, config):
            self.real = confined_session(config)
            self.handle = None
            downloads.append(self)

        def __getattr__(self, name):
            return getattr(self.real, name)

        def add_torrent(self, params):
            assert params.ti is None, 'metadata must arrive over ut_metadata'
            assert list(params.peers) == [('127.0.0.1', seed.listen_port())]
            assert not params.trackers and not params.url_seeds
            workdirs.append(Path(params.save_path))
            self.handle = self.real.add_torrent(params)
            return self.handle

        def pop_alerts(self):
            alerts = self.real.pop_alerts()
            alerts_seen.extend(type(a).__name__ for a in alerts)
            for a in alerts:
                if type(a).__name__ in ('metadata_received_alert', 'peer_error_alert', 'torrent_error_alert'):
                    print(type(a).__name__, a.message(), flush=True)
            return alerts

        def remove_torrent(self, handle, *args):
            self.payload_bytes = handle.status().total_payload_download
            return self.real.remove_torrent(handle, *args)

    binding = SimpleNamespace(session=Session, parse_magnet_uri=lt.parse_magnet_uri,
                              torrent_flags=lt.torrent_flags, alert=lt.alert,
                              storage_mode_t=lt.storage_mode_t)
    magnet._load_libtorrent = lambda: binding
    magnet.session_settings, magnet._DHT_ROUTERS = settings, []
    magnet.HEAD_BYTES, magnet.TAIL_BYTES = 64 * 1024, 32 * 1024
    magnet.METADATA_TIMEOUT_S, magnet.PIECE_TIMEOUT_S = 12, 12
    classify = magnet._classify_file
    # Only bypass the sample-size policy; actual sizes/pieces/content stay real.
    magnet._classify_file = lambda name, size: classify(name, max(size, 50 * 1024 * 1024))
    client = SuiteClient(argv_prefix=[sys.executable, '-m', 'videosuite'], root=os.environ['VIDEOSUITE_ROOT'])
    detached = []

    def facts(rows, emit, cancel):
        handle = downloads[-1].handle
        wanted = set(magnet._pieces_for_file(ti.piece_length(), 0, clip.stat().st_size, '.mkv'))
        assert {i for i in range(ti.num_pieces()) if handle.have_piece(i)} == wanted
        assert {i for i, p in enumerate(handle.get_piece_priorities()) if p} == wanted
        assert Path(rows[0]['path']).read_bytes() == clip.read_bytes()[:magnet.HEAD_BYTES]
        assert all(p.ip[0] == '127.0.0.1' for p in handle.get_peer_info())
        result = magnet_suite.partial_facts(rows, emit, cancel, client=client)
        assert len(result) == 1 and 'record' in result[0], result
        detached.extend(result)
        return {rows[0]['key']: dict(status='ok', record=result[0]['record'])}

    try:
        start = time.monotonic()
        doc = magnet.fetch_magnet_metadata(uri, metadata_only=True, use_default_scorer=False, listen_port=0,
                                           emit=lambda s: print(s, flush=True))
        print('metadata elapsed', round(time.monotonic() - start, 2), flush=True)
        assert doc['info_hash'] == str(ti.info_hash()) and len(doc['files']) == 1 and not doc['analyses']
        assert 'metadata_received_alert' in alerts_seen
        assert downloads[-1].payload_bytes == 0
        assert not workdirs[-1].exists()
        doc = magnet.run_magnet_job_threaded(uri, True, lambda s: print(s, flush=True), lambda: False,
                    selected_file_index=0, use_default_scorer=False, partial_scorer=facts, listen_port=0)
        assert len(detached) == len(doc['analyses']) == 1 and not workdirs[-1].exists()
        out = magnet_suite.recommend_partials(detached, lambda s: None, lambda: False, results='device',
                device='sony_bravia_8_ii', primary_player='just_player', player='just_player',
                fs=None, lang='eng', audio_output=None, client_factory=lambda: client)
        quick = next(iter(out.values()))['recommendation']
        validate_document('quick', quick)
        assert quick['selected']['player'] == 'just_player'
        print('PUBLIC LOOPBACK PASS', lt.version, flush=True)
    finally:
        for session in downloads:
            session.pause()
            if session.handle is not None and session.handle.is_valid():
                session.remove_torrent(session.handle, lt.session.delete_files)
            session.handle = session.real = None
        downloads.clear()
        seed.pause()
        seed.remove_torrent(seed_handle)
        seed_handle = seed = None
        gc.collect()


if __name__ == '__main__':
    assert sys.argv[1] == '--helper'
    _helper(sys.argv[2])
