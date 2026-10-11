"""D4: honest job states, cancellation and the SSE stream, through main.py.

HTTP tests assert FINAL states only (TestClient runs BackgroundTasks to
completion before returning). Cancellation and "running" behaviour is tested by
calling the runner functions directly (in a worker thread when they must run
concurrently with the test) and setting the flag through the store.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

import main
from jobs import JobStore, TERMINAL

from tests.test_contract_shapes import (
    MAGNET, MAGNET_B, MAGNET_C, make_result, fake_magnet_factory,
)


def job_of(client, jid):
    r = client.get(f"/job/{jid}")
    assert r.status_code == 200
    return r.json()


def events_of(job):
    return [e["msg"] for e in job["events"]]


# ── store wiring ─────────────────────────────────────────────────────────────

def test_main_jobs_is_the_store_and_terminal_is_shared():
    assert isinstance(main._jobs, JobStore)
    assert main.TERMINAL is TERMINAL
    assert not hasattr(main, "_magnet_cancel")


def test_get_job_has_kind_and_error_kind_keys(client, monkeypatch, tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: make_result("a.mkv"))
    jid = client.get("/analyze-path/", params={"path": str(f)}).json()["job_id"]
    job = job_of(client, jid)
    assert job["kind"] == "path" and job["error_kind"] is None

    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    assert job_of(client, r.json()["job_id"])["kind"] == "single"
    r = client.post("/analyze-multiple/", files=[("files", ("a.mkv", b"x"))])
    assert job_of(client, r.json()["job_id"])["kind"] == "batch"

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory())
    assert job_of(client, client.post("/magnet/", json={"magnet": MAGNET}).json()["job_id"])["kind"] == "magnet"
    jid = client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B]}).json()["job_id"]
    assert job_of(client, jid)["kind"] == "compare"


def test_unknown_job_404_text_is_unchanged(client):
    for method, url in (("get", "/job/nope"), ("get", "/job/nope/events"),
                        ("post", "/job/nope/cancel"), ("post", "/magnet/nope/cancel"),
                        ("post", "/compare-magnets/nope/cancel")):
        r = getattr(client, method)(url)
        assert r.status_code == 404, url
        assert r.json() == {"detail": "Job not found."}, url


# ── RB-10: analysis returned None -> error ───────────────────────────────────

def test_single_upload_no_data_is_an_error(client, monkeypatch, isolated_uploads):
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert job["error"] == "analysis timed out or failed"
    assert job["error_kind"] == "timeout"
    assert job["results"] == []
    assert events_of(job)[-1] == "✗ a.mkv — no data extracted"
    assert os.listdir(isolated_uploads) == []         # temp upload still removed


def test_single_upload_result_is_done(client, monkeypatch):
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: make_result("x.mkv"))
    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "done" and job["error"] is None
    assert len(job["results"]) == 1


def test_single_upload_exception_error_kind(client, monkeypatch):
    def boom(path, skip_dovi_scan=False):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(main, "analyze_file", boom)
    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "error" and job["error"] == "ffprobe exploded"
    assert job["error_kind"] == "analysis_failed"


# ── path jobs: RB-11, every file failed, exceptions ──────────────────────────

def test_path_job_empty_folder_is_an_error_and_no_pool_is_created(client, monkeypatch, tmp_path):
    def no_pool(*a, **k):
        raise AssertionError("a thread pool must not be created for an empty folder")

    monkeypatch.setattr(main, "ThreadPoolExecutor", no_pool)
    (tmp_path / "notes.txt").write_bytes(b"x")
    r = client.get("/analyze-path/", params={"path": str(tmp_path)})
    assert r.status_code == 200
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert job["error"] == "no video files found"
    assert job["error_kind"] == "no_files"
    assert job["results"] == []
    assert "max_workers" not in json.dumps(job)


def test_path_job_every_file_failed_is_an_error(client, monkeypatch, tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")

    def flaky(path, skip_dovi_scan=False):
        if path.endswith("a.mkv"):
            return None
        raise RuntimeError("bad file")

    monkeypatch.setattr(main, "analyze_file", flaky)
    r = client.get("/analyze-path/", params={"path": str(tmp_path)})
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "error"
    assert job["error"] == "No files could be analyzed."
    assert job["error_kind"] == "analysis_failed"
    assert job["results"] == []
    assert job["progress"] == "2/2"


def test_path_job_some_files_failed_is_done_with_results(client, monkeypatch, tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")
    monkeypatch.setattr(
        main, "analyze_file",
        lambda p, skip_dovi_scan=False: make_result("a.mkv") if p.endswith("a.mkv") else None)
    job = job_of(client, client.get("/analyze-path/", params={"path": str(tmp_path)}).json()["job_id"])
    assert job["status"] == "done" and job["error"] is None
    assert [r["file"] for r in job["results"]] == ["a.mkv"]


def test_path_job_single_file_none_is_an_error(client, monkeypatch, tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    job = job_of(client, client.get("/analyze-path/", params={"path": str(f)}).json()["job_id"])
    assert job["status"] == "error"
    assert job["error"] == "analysis timed out or failed"
    assert f.exists()


def test_path_job_single_file_exception_is_an_error(client, monkeypatch, tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")

    def boom(path, skip_dovi_scan=False):
        raise RuntimeError("bad file")

    monkeypatch.setattr(main, "analyze_file", boom)
    job = job_of(client, client.get("/analyze-path/", params={"path": str(f)}).json()["job_id"])
    assert job["status"] == "error" and job["error"] == "bad file"
    assert job["error_kind"] == "analysis_failed"


def test_batch_all_fail_error_kind(client, monkeypatch):
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    r = client.post("/analyze-multiple/", files=[("files", ("a.mkv", b"x"))])
    job = job_of(client, r.json()["job_id"])
    assert job["status"] == "error" and job["error"] == "No files could be analyzed."
    assert job["error_kind"] == "analysis_failed"


def test_magnet_error_kinds(client, monkeypatch):
    for exc, kind in ((main.MagnetUnavailable("no libtorrent"), "unavailable"),
                      (TimeoutError("slow"), "timeout"),
                      (RuntimeError("boom"), "internal")):
        def _raise(*a, _e=exc, **k):
            raise _e
        monkeypatch.setattr(main, "run_magnet_job_threaded", _raise)
        job = job_of(client, client.post("/magnet/", json={"magnet": MAGNET}).json()["job_id"])
        assert job["status"] == "error" and job["error_kind"] == kind


# ── cancel routes ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["/job/{}/cancel", "/magnet/{}/cancel",
                                 "/compare-magnets/{}/cancel"])
def test_cancel_routes_set_the_flag_and_return_ok(client, url):
    jid = main._jobs.create("magnet")
    assert main._jobs.is_cancelled(jid) is False
    r = client.post(url.format(jid))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert main._jobs.is_cancelled(jid) is True
    assert main._jobs.get(jid)["status"] == "running"     # the runner ends the job, not the route


@pytest.mark.parametrize("url", ["/job/{}/cancel", "/magnet/{}/cancel",
                                 "/compare-magnets/{}/cancel"])
def test_cancel_of_a_finished_job_is_ok_and_changes_nothing(client, url):
    jid = main._jobs.create("batch")
    main._jobs.finish(jid, results=[{"file": "a"}])
    r = client.post(url.format(jid))
    assert r.status_code == 200 and r.json() == {"ok": True}
    job = main._jobs.get(jid)
    assert job["status"] == "done" and job["error"] is None and job["results"] == [{"file": "a"}]


# ── runner-level cancel tests ────────────────────────────────────────────────

def _save_files(tmp_path, names):
    paths, name_map = [], {}
    for n in names:
        p = tmp_path / f"tmp_{n}"
        p.write_bytes(b"x")
        paths.append(str(p))
        name_map[str(p)] = n
    return paths, name_map


def test_batch_runner_cancel_stops_queued_files(monkeypatch, tmp_path):
    # one worker => files are analysed strictly one after the other
    monkeypatch.setattr(main, "ANALYSIS_WORKERS", 1)
    paths, name_map = _save_files(tmp_path, ["a.mkv", "b.mkv", "c.mkv"])
    jid = main._jobs.create("batch", total=3, progress="0/3")
    analysed: list[str] = []

    def fake(path, skip_dovi_scan=False):
        analysed.append(os.path.basename(path))
        main._jobs.request_cancel(jid)       # the user cancels during the first file
        return make_result(os.path.basename(path))

    monkeypatch.setattr(main, "analyze_file", fake)
    main._run_batch_job(jid, paths, name_map, False)

    job = main._jobs.get(jid)
    assert analysed == ["tmp_a.mkv"]          # two files never analysed
    assert job["status"] == "cancelled"
    assert job["error"] == "cancelled"
    assert len(job["results"]) == 1            # partial result kept
    assert events_of(job)[-1] == "Cancelled."
    assert all(not os.path.exists(p) for p in paths)   # every temp upload removed


def test_batch_runner_cancel_before_start_analyses_nothing(monkeypatch, tmp_path):
    paths, name_map = _save_files(tmp_path, ["a.mkv", "b.mkv"])
    jid = main._jobs.create("batch", total=2, progress="0/2")
    main._jobs.request_cancel(jid)
    calls = []
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: calls.append(p) or make_result("x"))
    main._run_batch_job(jid, paths, name_map, False)
    job = main._jobs.get(jid)
    assert calls == [] and job["status"] == "cancelled" and job["results"] == []
    assert all(not os.path.exists(p) for p in paths)


def test_path_runner_cancel_stops_queued_files(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ANALYSIS_WORKERS", 1)
    for n in ("a.mkv", "b.mkv", "c.mkv"):
        (tmp_path / n).write_bytes(b"x")
    jid = main._jobs.create("path")
    analysed = []

    def fake(path, skip_dovi_scan=False):
        analysed.append(os.path.basename(path))
        main._jobs.request_cancel(jid)
        return make_result(os.path.basename(path))

    monkeypatch.setattr(main, "analyze_file", fake)
    main._run_path_job(jid, str(tmp_path), False)
    job = main._jobs.get(jid)
    assert len(analysed) == 1
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert len(job["results"]) == 1
    assert all((tmp_path / n).exists() for n in ("a.mkv", "b.mkv", "c.mkv"))


def test_path_runner_single_file_cancel_before_start(monkeypatch, tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    jid = main._jobs.create("path")
    main._jobs.request_cancel(jid)
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: pytest.fail("must not be analysed"))
    main._run_path_job(jid, str(f), False)
    assert main._jobs.get(jid)["status"] == "cancelled"
    assert f.exists()


def test_single_runner_cancelled_during_analysis_removes_the_temp_file(monkeypatch, tmp_path):
    f = tmp_path / "tmp_a.mkv"
    f.write_bytes(b"x")
    jid = main._jobs.create("single", total=1, progress="0/1", current="a.mkv")

    def fake(path, skip_dovi_scan=False):
        main._jobs.request_cancel(jid)
        return make_result("tmp_a.mkv", path=path)

    monkeypatch.setattr(main, "analyze_file", fake)
    main._run_single(jid, str(f), "a.mkv", False)
    job = main._jobs.get(jid)
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert not f.exists()


def test_magnet_runner_honours_cancel_in_a_worker_thread(monkeypatch):
    jid = main._jobs.create("magnet", progress="metadata", magnet_files=[], magnet_torrent=None)
    started = threading.Event()

    def fake(magnet_uri, skip_dovi_scan=False, emit=None, cancel_check=None,
             listen_port=None, enable_hdr10plus=False):
        started.set()
        while not cancel_check():             # what magnet.py's libtorrent loops do
            time.sleep(0.01)
        raise RuntimeError("Cancelled")

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    t = threading.Thread(target=main._run_magnet_job, args=(jid, MAGNET, False, False))
    t.start()
    assert started.wait(5)
    assert main._jobs.get(jid)["status"] == "running"
    main._jobs.request_cancel(jid)
    t.join(5)
    assert not t.is_alive()
    job = main._jobs.get(jid)
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert events_of(job)[-1] == "Cancelled."


def test_magnet_runner_cancelled_job_keeps_partial_results(monkeypatch):
    jid = main._jobs.create("magnet", progress="metadata", magnet_files=[], magnet_torrent=None)

    def fake(magnet_uri, skip_dovi_scan=False, emit=None, cancel_check=None,
             listen_port=None, enable_hdr10plus=False):
        main._jobs.request_cancel(jid)
        return fake_magnet_factory()(magnet_uri)

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    main._run_magnet_job(jid, MAGNET, False)
    job = main._jobs.get(jid)
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert len(job["results"]) == 1 and job["magnet_files"]


def test_compare_runner_sequential_cancel_skips_remaining_magnets(monkeypatch):
    jid = main._jobs.create("compare", total=3, progress="0/3", per_magnet=[],
                            comparison=None, winner=None, mode=None,
                            episode_comparison=None, dropped=[])
    seen = []

    def fake(magnet_uri, skip_dovi_scan=False, emit=None, cancel_check=None,
             listen_port=None, enable_hdr10plus=False):
        seen.append(magnet_uri)
        main._jobs.request_cancel(jid)
        return fake_magnet_factory()(magnet_uri)

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    main._run_compare_job(jid, [MAGNET, MAGNET_B], True)
    job = main._jobs.get(jid)
    assert seen == [MAGNET]
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert [m["status"] for m in job["per_magnet"]] == ["done", "pending"]
    assert job["comparison"] is None and job["winner"] is None
    assert any("Cancelled" in m for m in events_of(job))


def test_compare_runner_parallel_cancel_does_not_start_queued_magnets(monkeypatch):
    jid = main._jobs.create("compare", total=6, progress="0/6", per_magnet=[],
                            comparison=None, winner=None, mode=None,
                            episode_comparison=None, dropped=[])
    seen = []
    lock = threading.Lock()

    def fake(magnet_uri, skip_dovi_scan=False, emit=None, cancel_check=None,
             listen_port=None, enable_hdr10plus=False):
        with lock:
            seen.append(magnet_uri)
        main._jobs.request_cancel(jid)
        time.sleep(0.05)
        return fake_magnet_factory()(magnet_uri)

    monkeypatch.setattr(main, "run_magnet_job_threaded", fake)
    magnets = [MAGNET[:-2] + f"{i:02d}" for i in range(6)]
    main._run_compare_job(jid, magnets, True)
    job = main._jobs.get(jid)
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert len(seen) <= main.COMPARE_PARALLEL_LIMIT        # the queued four never started
    assert any(m["status"] == "pending" for m in job["per_magnet"])


def test_compare_runner_done_count_is_exact_under_parallelism(monkeypatch):
    n = 8
    jid = main._jobs.create("compare", total=n, progress=f"0/{n}", per_magnet=[],
                            comparison=None, winner=None, mode=None,
                            episode_comparison=None, dropped=[])
    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory())
    magnets = [MAGNET[:-2] + f"{i:02d}" for i in range(n)]
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        main._run_compare_job(jid, magnets, True)
    finally:
        sys.setswitchinterval(old)
    job = main._jobs.get(jid)
    assert job["status"] == "done"
    assert job["progress"] == f"{n}/{n}"
    assert [m["status"] for m in job["per_magnet"]] == ["done"] * n


# ── SSE ──────────────────────────────────────────────────────────────────────

def test_sse_ends_when_a_job_ends_cancelled(client):
    jid = main._jobs.create("batch")
    main._jobs.append_event(jid, "working")
    main._jobs.request_cancel(jid)
    main._jobs.finish(jid)
    body = client.get(f"/job/{jid}/events").text
    assert body.endswith("data: __done__\n\n")
    assert '"msg": "working"' in body


def test_sse_sees_events_appended_after_the_stream_started(client):
    # The generator must re-read the store each iteration: get() returns copies.
    jid = main._jobs.create("batch")

    def producer():
        time.sleep(0.4)
        for i in range(3):
            main._jobs.append_event(jid, f"late-{i}")
            time.sleep(0.05)
        main._jobs.finish(jid)

    t = threading.Thread(target=producer)
    t.start()
    body = client.get(f"/job/{jid}/events").text
    t.join()
    msgs = [json.loads(f[len("data: "):])["msg"]
            for f in body.split("\n\n") if f.startswith("data: ") and "__done__" not in f]
    assert msgs == ["late-0", "late-1", "late-2"]
    assert body.endswith("data: __done__\n\n")


def test_sse_ends_when_the_job_disappears(client):
    jid = main._jobs.create("batch")

    def producer():
        time.sleep(0.4)
        main._jobs.clear()

    t = threading.Thread(target=producer)
    t.start()
    body = client.get(f"/job/{jid}/events").text
    t.join()
    assert body.endswith("data: __done__\n\n")


# ── retention through the app ────────────────────────────────────────────────

def test_creating_jobs_purges_old_finished_ones_without_timers(client, monkeypatch):
    monkeypatch.setattr(main._jobs, "grace_s", 0.0)
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: make_result("a.mkv"))
    ids = []
    for _ in range(23):
        r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
        ids.append(r.json()["job_id"])
    finished = [i for i in main._jobs.ids() if main._jobs.get(i)["status"] in TERMINAL]
    assert len(finished) == 21                 # 20 kept + the newest, purged on the next create
    assert ids[0] not in main._jobs and ids[-1] in main._jobs


# ── final fix wave: cancel logging, upload file names ────────────────────────

def _tracebacks(caplog, needle):
    return [r for r in caplog.records if r.exc_info and needle in str(r.exc_info[1])]


def test_a_user_cancelled_magnet_job_logs_no_traceback(client, monkeypatch, caplog):
    def cancelled(*a, **k):
        raise RuntimeError("Cancelled")

    monkeypatch.setattr(main, "run_magnet_job_threaded", cancelled)
    with caplog.at_level("ERROR", logger="video-analyzer.api"):
        jid = client.post("/magnet/", json={"magnet": MAGNET}).json()["job_id"]
    assert not [r for r in caplog.records if r.exc_info], caplog.text
    assert job_of(client, jid)["status"] == "error"          # no cancel flag was set: still an honest error


def test_a_user_cancelled_magnet_in_a_comparison_logs_no_traceback(client, monkeypatch, caplog):
    def cancelled(*a, **k):
        raise RuntimeError("Cancelled")

    monkeypatch.setattr(main, "run_magnet_job_threaded", cancelled)
    with caplog.at_level("ERROR", logger="video-analyzer.api"):
        client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B]})
    assert not [r for r in caplog.records if r.exc_info], caplog.text


def test_other_magnet_exceptions_still_log_a_traceback(client, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(main, "run_magnet_job_threaded", boom)
    with caplog.at_level("ERROR", logger="video-analyzer.api"):
        client.post("/magnet/", json={"magnet": MAGNET})
        client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B]})
    assert len(_tracebacks(caplog, "engine exploded")) >= 2


@pytest.mark.parametrize("name,expected", [
    ("a:b.mkv", "b.mkv"),                        # ntpath.basename already drops a leading "a:" (a drive)
    ("film.mkv:Zone.Identifier", "film.mkvZone.Identifier"),     # an alternate data stream name
    ("a<b>c|d?e*f.mkv", "abcdef.mkv"),
    (r"C:\Users\x\film: part 1.mkv", "film part 1.mkv"),
    (r"..\..\evil.mkv", "evil.mkv"),
    ("trailing. ", "trailing"),
    (". .", "upload.bin"),
    (":::", "upload.bin"),
])
def test_upload_file_names_lose_characters_that_are_invalid_on_windows(client, monkeypatch, isolated_uploads, name, expected):
    seen = []

    def fake(path, skip_dovi_scan=False):
        seen.append(path)
        return make_result("x.mkv")

    monkeypatch.setattr(main, "analyze_file", fake)
    r = client.post("/analysis/", files={"file": (name, b"x")})
    assert r.status_code == 200
    (path,) = seen
    base = os.path.basename(path)
    assert os.path.dirname(path) == str(isolated_uploads)
    assert ":" not in base and base.split("_", 1)[1] == expected
    assert os.listdir(isolated_uploads) == []                 # no alternate data stream left behind either
