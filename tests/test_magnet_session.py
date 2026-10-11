"""Torrent session hardening (plan 2, task D5). libtorrent is NOT installed here,
so everything is tested through the pure helpers and fakes.

Live behaviour of the real session is NOT covered: the owner must run one real
magnet after this task (see README, "Torrent session hardening").
"""
from __future__ import annotations

import sys
import threading

import pytest

import magnet
import main

MIB = 1024 * 1024

# Every key `session_settings` may emit. These are documented libtorrent
# settings_pack names; the set is frozen here so a typo (an unknown key makes
# `lt.session({...})` raise at runtime) is caught by the tests, not on a live magnet.
ALLOWED_SETTINGS_KEYS = frozenset({
    "listen_interfaces",
    "enable_dht",
    "enable_lsd",
    "enable_natpmp",
    "enable_upnp",
    "connection_speed",
    "peer_connect_timeout",
    "handshake_timeout",
    "request_timeout",
    "min_reconnect_time",
    "announce_to_all_tiers",
    "announce_to_all_trackers",
    "upload_rate_limit",
})


# ── session_settings ─────────────────────────────────────────────────────────

def test_session_settings_keys_are_frozen():
    s = magnet.session_settings(6881)
    assert set(s) == ALLOWED_SETTINGS_KEYS


def test_session_settings_values():
    s = magnet.session_settings(6890)
    assert s["listen_interfaces"] == "0.0.0.0:6890"
    assert s["enable_dht"] is True
    assert s["enable_upnp"] is False
    assert s["enable_natpmp"] is False
    assert s["enable_lsd"] is False
    # Peers choke a ~1 B/s uploader (tit-for-tat), so never cap below 50 KiB/s.
    assert s["upload_rate_limit"] == 51200
    assert s["upload_rate_limit"] >= 50 * 1024


def test_session_settings_never_sets_unchoke_slots_or_max_uploads():
    s = magnet.session_settings(6881)
    assert "max_uploads" not in s
    assert "unchoke_slots_limit" not in s


def test_session_settings_keeps_previous_tuning():
    s = magnet.session_settings(6881)
    assert s["connection_speed"] == 100
    assert s["announce_to_all_tiers"] is True
    assert s["announce_to_all_trackers"] is True


def test_session_settings_is_a_fresh_dict():
    a = magnet.session_settings(6881)
    a["enable_upnp"] = True
    assert magnet.session_settings(6881)["enable_upnp"] is False


@pytest.mark.parametrize("bad", [0, -1, 80, 1023, 65536, "6881", None, True, 6881.0])
def test_session_settings_rejects_invalid_ports(bad):
    with pytest.raises(ValueError):
        magnet.session_settings(bad)


# ── PortAllocator ────────────────────────────────────────────────────────────

def test_allocator_range_and_uniqueness():
    a = magnet.PortAllocator()
    ports = [a.acquire() for _ in range(10)]
    assert len(set(ports)) == 10
    assert all(6881 <= p <= 6999 for p in ports)
    assert ports[0] == 6881  # lowest free first


def test_allocator_release_makes_port_reusable():
    a = magnet.PortAllocator()
    p1 = a.acquire()
    p2 = a.acquire()
    a.release(p1)
    assert a.acquire() == p1
    assert p2 != p1


def test_allocator_release_is_idempotent_and_ignores_unknown():
    a = magnet.PortAllocator()
    p = a.acquire()
    a.release(p)
    a.release(p)
    a.release(12345)
    assert a.in_use() == frozenset()


def test_allocator_exhaustion_raises_and_recovers():
    a = magnet.PortAllocator(first=7000, last=7002)
    got = [a.acquire() for _ in range(3)]
    assert sorted(got) == [7000, 7001, 7002]
    with pytest.raises(magnet.NoFreePort):
        a.acquire()
    a.release(7001)
    assert a.acquire() == 7001


def test_allocator_lease_releases_after_exception():
    a = magnet.PortAllocator()
    with pytest.raises(RuntimeError):
        with a.lease() as port:
            assert port in a.in_use()
            raise RuntimeError("boom")
    assert a.in_use() == frozenset()


def test_allocator_concurrent_acquire_never_shares_a_port():
    a = magnet.PortAllocator()
    seen: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(40)

    def worker():
        barrier.wait()
        p = a.acquire()
        with lock:
            seen.append(p)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 40 and len(set(seen)) == 40


def test_module_allocator_is_process_wide():
    assert isinstance(magnet.port_allocator, magnet.PortAllocator)
    assert main.port_allocator is magnet.port_allocator


# ── tail_is_present (RB-20) ──────────────────────────────────────────────────

def test_tail_present_small_file_without_reading(tmp_path):
    # 60 MiB <= HEAD (32) + TAIL (64): head + tail pieces cover the whole file.
    missing = tmp_path / "does_not_exist.mp4"
    assert magnet.tail_is_present(str(missing), 60 * MIB) is True


def test_tail_present_boundary(tmp_path):
    missing = str(tmp_path / "nope.m2ts")
    assert magnet.tail_is_present(missing, magnet.HEAD_BYTES + magnet.TAIL_BYTES) is True
    # One byte more: now the file must actually be read (missing file -> False).
    assert magnet.tail_is_present(missing, magnet.HEAD_BYTES + magnet.TAIL_BYTES + 1) is False


def test_tail_present_large_file_checks_last_4k(tmp_path):
    size = 200 * MIB
    p = tmp_path / "big.mp4"
    with open(p, "wb") as f:
        f.truncate(size)             # all zeros: tail not downloaded
    assert magnet.tail_is_present(str(p), size) is False
    with open(p, "r+b") as f:
        f.seek(size - 100)
        f.write(b"moov")
    assert magnet.tail_is_present(str(p), size) is True


def test_tail_present_unreadable_is_false(tmp_path):
    assert magnet.tail_is_present(str(tmp_path / "gone.mp4"), 500 * MIB) is False


# ── file_record (RB-21) ──────────────────────────────────────────────────────

def test_file_record_fields_and_gib_alias():
    size = 3 * 1024 ** 3 + 512 * MIB          # 3.5 GiB
    rec = magnet.file_record(4, "Movie/Movie.2160p.mkv", size)
    assert rec["index"] == 4
    assert rec["name"] == "Movie/Movie.2160p.mkv"
    assert rec["size_bytes"] == size
    assert rec["size_gib"] == 3.5
    assert rec["size_gb"] == rec["size_gib"]   # deprecated alias, same GiB number
    assert rec["ext"] == ".mkv"
    assert rec["verdict"] == "good"
    assert rec["reasons"] == []
    assert rec["ffprobe_ok"] is False
    assert rec["analysis_path"] is None


def test_file_record_junk_and_rounding():
    rec = magnet.file_record(0, "Movie/sample.mkv", 10 * MIB)
    assert rec["verdict"] == "bad"
    assert rec["reasons"]
    assert rec["size_gib"] == round(10 * MIB / 1024 ** 3, 3)
    assert rec["size_gb"] == rec["size_gib"]


def test_file_record_reasons_lists_are_independent():
    a = magnet.file_record(0, "a/x.nfo", 10)
    b = magnet.file_record(1, "a/y.nfo", 10)
    a["reasons"].append("x")
    assert "x" not in b["reasons"]


# ── libtorrent stays lazy and behind MagnetUnavailable ───────────────────────

def test_missing_libtorrent_raises_magnet_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "libtorrent", None)   # `import libtorrent` -> ImportError
    with pytest.raises(magnet.MagnetUnavailable):
        magnet.run_magnet_job_threaded(
            "magnet:?xt=urn:btih:" + "a" * 40, True, lambda _m: None, lambda: False,
        )


def test_pure_helpers_do_not_import_libtorrent(monkeypatch):
    monkeypatch.setitem(sys.modules, "libtorrent", None)
    magnet.session_settings(6881)
    magnet.file_record(0, "a.mkv", 100 * MIB)
    magnet.tail_is_present("x.mp4", 10 * MIB)
    a = magnet.PortAllocator()
    a.release(a.acquire())


class _FakeLt:
    """Records what the code hands to ``lt.session`` and aborts at the magnet parse."""

    class alert:
        class category_t:
            all_categories = 0xFFFF

    session_args: list[dict] = []

    class _Session:
        def __init__(self, settings):
            _FakeLt.session_args.append(dict(settings))
            self.paused = False

        def add_dht_router(self, host, port):
            pass

        def pause(self):
            self.paused = True

    def __init__(self):
        _FakeLt.session_args = []

    def session(self, settings):
        return _FakeLt._Session(settings)

    def parse_magnet_uri(self, uri):
        raise RuntimeError("bad uri")


def test_fetch_uses_session_settings_plus_alert_mask(monkeypatch):
    monkeypatch.setattr(magnet, "_load_libtorrent", lambda: _FakeLt())
    monkeypatch.setattr(magnet.time, "sleep", lambda _s: None)
    with pytest.raises(ValueError, match="Invalid magnet URI"):
        magnet.fetch_magnet_metadata("magnet:?xt=urn:btih:zz", listen_port=6907)
    assert len(_FakeLt.session_args) == 1
    passed = _FakeLt.session_args[0]
    assert passed.pop("alert_mask") == _FakeLt.alert.category_t.all_categories
    assert passed == magnet.session_settings(6907)
    assert set(passed) <= ALLOWED_SETTINGS_KEYS


# ── /magnet/ and /compare-magnets/ use the process-wide allocator ────────────

MAGNET = "magnet:?xt=urn:btih:" + "b" * 40


def _fake_magnet(seen_ports, in_use_snapshots=None, boom=False):
    def fake(magnet_uri, skip_dovi_scan, emit, cancel_check,
             listen_port=None, enable_hdr10plus=False):
        seen_ports.append(listen_port)
        if in_use_snapshots is not None:
            in_use_snapshots.append(main.port_allocator.in_use())
        if boom:
            raise RuntimeError("torrent exploded")
        return {"torrent_name": "T", "info_hash": "h", "files": [], "analyses": []}
    return fake


def test_single_magnet_gets_allocated_port_and_releases(client, monkeypatch):
    ports: list = []
    snaps: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports, snaps))
    r = client.post("/magnet/", json={"magnet": MAGNET})
    assert r.status_code == 200
    assert ports == [6881]
    assert snaps == [frozenset({6881})]
    assert main.port_allocator.in_use() == frozenset()


def test_single_magnet_releases_port_after_exception(client, monkeypatch):
    ports: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports, boom=True))
    r = client.post("/magnet/", json={"magnet": MAGNET})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert main.port_allocator.in_use() == frozenset()


def test_single_magnet_does_not_reuse_a_port_held_by_another_job(client, monkeypatch):
    held = main.port_allocator.acquire()          # e.g. a compare slot still running
    ports: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports))
    client.post("/magnet/", json={"magnet": MAGNET})
    assert ports and ports[0] != held


def test_single_magnet_with_no_free_port_fails_cleanly(client, monkeypatch):
    monkeypatch.setattr(main, "port_allocator", magnet.PortAllocator(first=7000, last=7000))
    main.port_allocator.acquire()
    ports: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports))
    r = client.post("/magnet/", json={"magnet": MAGNET})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert ports == []


def test_three_simultaneous_jobs_never_share_a_port(monkeypatch):
    """Three real runners overlapping in time (each blocks until all have a port)."""
    main._jobs.clear()
    barrier = threading.Barrier(3)
    ports: list[int] = []
    lock = threading.Lock()

    def fake(magnet_uri, skip_dovi_scan, emit, cancel_check,
             listen_port=None, enable_hdr10plus=False):
        with lock:
            ports.append(listen_port)
        barrier.wait(timeout=10)       # all three hold their port at the same time
        return {"torrent_name": "T", "info_hash": "h", "files": [], "analyses": []}

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    ids = [main._jobs.create("magnet", magnet_files=[], magnet_torrent=None) for _ in range(3)]
    threads = [threading.Thread(target=main._run_magnet_job, args=(j, MAGNET, True, False))
               for j in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(ports) == 3 and len(set(ports)) == 3
    assert main.port_allocator.in_use() == frozenset()


def test_compare_of_eight_magnets_never_shares_a_port(client, monkeypatch):
    ports: list = []
    snaps: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports, snaps))
    magnets = [f"magnet:?xt=urn:btih:{i:040x}" for i in range(8)]
    r = client.post("/compare-magnets/", json={"magnets": magnets})
    assert r.status_code == 200
    assert len(ports) == 8
    assert all(6881 <= p <= 6999 for p in ports)
    # at no moment did two slots hold the same port, and slots held distinct ports
    # at the time of each call
    assert all(p in snap for p, snap in zip(ports, snaps))
    assert main.port_allocator.in_use() == frozenset()


def test_compare_port_does_not_collide_with_a_running_single_job(client, monkeypatch):
    held = main.port_allocator.acquire()       # a single-magnet job holding 6881
    ports: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports))
    client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET.replace("b", "c")]})
    assert len(ports) == 2 and held not in ports
    assert main.port_allocator.in_use() == frozenset({held})


def test_two_overlapping_compare_jobs_never_share_ports(monkeypatch):
    main._jobs.clear()
    barrier = threading.Barrier(2)
    ports: list[int] = []
    lock = threading.Lock()

    def fake(magnet_uri, skip_dovi_scan, emit, cancel_check,
             listen_port=None, enable_hdr10plus=False):
        with lock:
            ports.append(listen_port)
        barrier.wait(timeout=10)
        return {"torrent_name": "T", "info_hash": "h", "files": [], "analyses": []}

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    ids = [main._jobs.create("compare", per_magnet=[], comparison=None, winner=None,
                             mode=None, episode_comparison=None, dropped=[], total=1)
           for _ in range(2)]
    threads = [threading.Thread(target=main._run_compare_job, args=(j, [MAGNET], True, False))
               for j in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(ports) == 2 and len(set(ports)) == 2


def test_compare_releases_port_after_exception(client, monkeypatch):
    ports: list = []
    monkeypatch.setattr(main, "run_magnet_job_threaded", _fake_magnet(ports, boom=True))
    client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET.replace("b", "c")]})
    assert len(ports) == 2
    assert main.port_allocator.in_use() == frozenset()
