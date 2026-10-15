"""Torrent session hardening (plan 2, task D5). libtorrent is NOT installed here,
so everything is tested through the pure helpers and fakes.

Live behaviour of the real session is NOT covered: the owner must run one real
magnet after this task (see README, "Torrent session hardening").
"""
from __future__ import annotations

import sys
import os
import types
from pathlib import Path
import threading

import pytest

import magnet
import main

MIB = 1024 * 1024


class _FakeLtFull:
    class alert:
        class category_t:
            all_categories = 1

    def __init__(self, ext=".mkv"):
        self.name = "Show/Movie.2160p.DV" + ext
        self.events = []
        self.workdir = None
        outer = self
        class Session:
            delete_files = 1
            def __init__(self, settings): pass
            def add_dht_router(self, *args): pass
            def pop_alerts(self): return []
            def add_torrent(self, params):
                outer.workdir = params.save_path
                p = Path(params.save_path, outer.name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"header")
                return Handle()
            def remove_torrent(self, *args): pass
            def pause(self): pass
        class Handle:
            def status(self): return types.SimpleNamespace(has_metadata=True)
            def torrent_file(self): return Info()
            def prioritize_files(self, *args): pass
            def piece_priority(self, *args): pass
            def set_piece_deadline(self, *args): pass
            def have_piece(self, *args): return True
            def pause(self): outer.events.append("pause")
        class Info:
            def files(self): return self
            def num_files(self): return 1
            def file_path(self, i): return outer.name
            def file_size(self, i): return 40 * 2**30
            def file_offset(self, i): return 0
            def piece_length(self): return 16 * MIB
            def name(self): return "Show"
            def info_hash(self): return "hash"
        self.session = Session

    def parse_magnet_uri(self, uri): return types.SimpleNamespace()


@pytest.mark.parametrize("ext,middle,mode", [(".mkv", False, "ok"), (".mkv", True, "ok"),
                                           (".mp4", False, "ok"), (".mkv", False, "raises"),
                                           (".mkv", False, "none"), (".mkv", False, "emit_raises")])
def test_partial_hook_files_pause_and_cleanup(monkeypatch, ext, middle, mode):
    lt = _FakeLtFull(ext)
    monkeypatch.setattr(magnet, "_load_libtorrent", lambda: lt)
    monkeypatch.setattr(magnet, "_check_temp_disk_space", lambda p: None)
    monkeypatch.setattr(magnet.time, "sleep", lambda s: None)
    monkeypatch.setattr(magnet.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(magnet, "_should_middle_sample", lambda *a: middle)
    monkeypatch.setattr(magnet, "tail_is_present", lambda *a: True)
    monkeypatch.setattr(magnet, "analyze_file", lambda *a, **kw: {"score": 5})
    seen = []
    def scorer(entries, emit, cancel):
        assert lt.events == ["pause"]
        e = entries[0]
        assert os.path.isfile(e["path"])
        assert e["real_size"] == 40 * 2**30
        assert e["real_name"] == "Movie.2160p.DV" + ext
        assert e["coverage"] == ("head_tail" if ext == ".mp4" else "head")
        seen.append(e["path"])
        if mode == "raises": raise RuntimeError("scorer surprise")
        return {e["key"]: {"status": "ok"}}
    magnet.set_partial_scorer(None if mode == "none" else scorer)
    broken = [False]
    def emit(message):
        if message.startswith("Scoring "):
            broken[0] = True
        if broken[0]:
            raise RuntimeError("persistent notification failure")
    out = magnet.fetch_magnet_metadata(MAGNET, emit=emit if mode == "emit_raises" else None)
    analysis = out["analyses"][0]
    if mode == "none": assert "suite" not in analysis
    elif mode in ("raises", "emit_raises"): assert analysis["suite"] == dict(status="error", error_kind="internal", message="VideoSuite scoring failed")
    else: assert analysis["suite"] == {"status": "ok"}
    assert not Path(lt.workdir).exists()
    assert all(not Path(p).exists() for p in seen)


def _cli_accepts_real_name(name):
    # Mirrors the checks of videosuite/cli.py `_check_partial` and suite_client.score_partial.
    import suite_client
    bad = ((0x00, 0x1F), (0x7F, 0x9F), (0x2028, 0x202E), (0x2066, 0x2069))
    return (bool(name.strip()) and name == name.strip() and "/" not in name and "\\" not in name
            and len(name) <= 255 and suite_client._text_problem(name) is None
            and not any(lo <= ord(c) <= hi for c in name for lo, hi in bad))


@pytest.mark.parametrize("raw", [
    "Show/Movie.2160p.mkv", "a‮vkm.exe", "ok\x7fname.mkv", "c1\x85\x9fname.mkv", "line sep .mkv",
    "bidi⁦x⁩.mkv", "ctl\x01\x1f.mkv", "back\\slash.mkv", "tab\tname.mkv", " lead.mkv ", "x" * 300 + ".mkv",
    "‮", "", "Folder/", "dir\\/"])
def test_suite_real_name_passes_the_cli_rules(raw):
    name = magnet.suite_real_name(raw)
    assert _cli_accepts_real_name(name), repr(name)
    assert name


def test_suite_real_name_details():
    assert magnet.suite_real_name("Show/Movie.2160p.DV.mkv") == "Movie.2160p.DV.mkv"
    assert magnet.suite_real_name("Movie ук.mkv") == "Movie ук.mkv"
    assert magnet.suite_real_name("a‮vkm.exe") == "a_vkm.exe"
    assert magnet.suite_real_name("ok\x7fname.mkv") == "ok_name.mkv"
    assert magnet.suite_real_name("a\x85b c.mkv") == "a_b_c.mkv"
    long_name = magnet.suite_real_name("x" * 300 + ".mkv")
    assert len(long_name) == 255 and long_name.endswith(".mkv") and long_name.startswith("x" * 251)
    assert magnet.suite_real_name("") == "partial.bin"
    assert magnet.suite_real_name("   ") == "partial.bin"
    assert magnet.suite_real_name("Folder/") == "partial.bin"
    assert magnet.suite_real_name("‮\x00") == "__"


def test_hostile_name_is_sanitised_for_the_suite_but_not_for_display(monkeypatch):
    lt = _FakeLtFull(".mkv")
    lt.name = "Show/a‮vkm.mkv"
    monkeypatch.setattr(magnet, "_load_libtorrent", lambda: lt)
    monkeypatch.setattr(magnet, "_check_temp_disk_space", lambda p: None)
    monkeypatch.setattr(magnet.time, "sleep", lambda s: None)
    monkeypatch.setattr(magnet.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(magnet, "_should_middle_sample", lambda *a: False)
    monkeypatch.setattr(magnet, "tail_is_present", lambda *a: True)
    monkeypatch.setattr(magnet, "analyze_file", lambda *a, **kw: {"score": 5})
    names = []
    def scorer(entries, emit, cancel):
        names.extend(e["real_name"] for e in entries)
        return {e["key"]: {"status": "ok"} for e in entries}
    magnet.set_partial_scorer(scorer)
    out = magnet.fetch_magnet_metadata(MAGNET)
    assert names == ["a_vkm.mkv"]
    assert out["analyses"][0]["file"] == "a‮vkm.mkv"


def test_install_and_job_preserve_suite(client, monkeypatch):
    import magnet_suite
    main.install_magnet_scorer()
    assert magnet._partial_scorer is magnet_suite.score_partials
    analysis = dict(file="movie.mkv", path="movie.mkv", score=5, suite={"status": "ok"})
    monkeypatch.setattr(main, "run_magnet_job_threaded", lambda *a, **kw: dict(analyses=[analysis], files=[]))
    monkeypatch.setattr(main, "rank_results", lambda results: results)
    monkeypatch.setattr(main, "enrich_release", lambda r, **kw: dict(analysis=r))
    response = client.post("/magnet/", json={"magnet": MAGNET})
    job = client.get(f"/job/{response.json()['job_id']}").json()
    assert job["status"] == "done"
    assert job["results"][0]["suite"] == job["enriched"][0]["analysis"]["suite"] == {"status": "ok"}


@pytest.mark.parametrize("installed", [False, True])
def test_outer_join_includes_suite_stage(monkeypatch, installed):
    seen = []
    class Thread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
        def join(self, timeout): seen.append(timeout)
        def is_alive(self): return False
    monkeypatch.setattr(magnet.threading, "Thread", Thread)
    monkeypatch.setattr(magnet, "fetch_magnet_metadata", lambda *a, **kw: {})
    magnet.set_partial_scorer((lambda *a: {}) if installed else None)
    magnet.run_magnet_job_threaded(MAGNET, True, lambda m: None, lambda: False)
    assert seen == [magnet.METADATA_TIMEOUT_S + magnet.PIECE_TIMEOUT_S + magnet.PROBE_MAX_S + 30 + (420 if installed else 0)]

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
