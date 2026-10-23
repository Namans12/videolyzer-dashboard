"""Characterization tests: freeze TODAY's HTTP/job/SSE contract of main.py.

Written before any refactor. They use fake ``analyze_file`` / ``scan_folder`` /
``run_magnet_job_threaded`` monkeypatched onto ``main`` and assert FINAL states
only (TestClient runs BackgroundTasks to completion before returning).

Later tasks may only ADD keys: key-set assertions are ``required <= actual``.
Where a test freezes behaviour that is arguably a bug, the comment says
"FROZEN ODDITY"; a later task is expected to change it deliberately.
"""
from __future__ import annotations

import json
import os

import pytest

import main

BASE_KEYS = {"status", "progress", "current", "total", "results", "error", "events"}
MAGNET_KEYS = BASE_KEYS | {"magnet_files", "magnet_torrent"}
COMPARE_KEYS = BASE_KEYS | {"per_magnet", "comparison", "winner", "mode",
                            "episode_comparison", "dropped"}

MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
MAGNET_B = "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01"
MAGNET_C = "magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98"


def make_result(name: str, **over) -> dict:
    rec = {
        "file": name,
        "path": os.path.join("somewhere", name),
        "tv_score": 80,
        "score": 70,
        "confidence_score": 90,
        "bitrate_mbps": 20.0,
        "file_size_gb": 10.0,
        "dv_profile": "8.1",
        "rpu": "Yes",
        "bl": "Yes",
        "el": "No",
        "hdr": "Dolby Vision | HDR10",
        "audio_details": "E-AC3 JOC, 5.1, 768 kbps",
        "media_facts": [],
    }
    rec.update(over)
    return rec


@pytest.fixture
def fake_analyze(monkeypatch):
    """analyze_file fake: returns a result named after the file it was given."""
    calls = []

    def _fake(path, skip_dovi_scan=False):
        calls.append((path, skip_dovi_scan))
        name = os.path.basename(path)
        return make_result(name, path=path)

    monkeypatch.setattr(main, "analyze_file", _fake)
    _fake.calls = calls
    return _fake


def fake_magnet_factory(per_magnet_names=None):
    def _fake(magnet_uri, skip_dovi_scan=False, emit=None, cancel_check=None,
              listen_port=None, enable_hdr10plus=False):
        name = (per_magnet_names or {}).get(magnet_uri, "Movie.2024.2160p.WEB-DL.mkv")
        if emit:
            emit("Metadata received")
        return {
            "torrent_name": name,
            "info_hash": magnet_uri[-40:],
            "files": [{"path": name, "size": 123, "verdict": "video"}],
            "analyses": [make_result(name)],
        }

    return _fake


def assert_job_basics(job: dict, status: str = "done"):
    assert job["status"] == status
    assert isinstance(job["events"], list)
    for ev in job["events"]:
        assert set(ev) == {"msg", "ts"}
        assert isinstance(ev["msg"], str)
        assert isinstance(ev["ts"], float)


# ── (c) route response keys ──────────────────────────────────────────────────

def test_health_keys(client, isolated_uploads):
    r = client.get("/health/")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "disk_free_gb", "disk_used_gb", "upload_dir"}
    assert body["status"] == "ok"
    assert body["upload_dir"] == os.path.abspath(str(isolated_uploads))
    assert isinstance(body["disk_free_gb"], (int, float))
    assert isinstance(body["disk_used_gb"], (int, float))


def test_unknown_job_is_404(client):
    for url in ("/job/nope", "/job/nope/events"):
        r = client.get(url)
        assert r.status_code == 404
        assert r.json() == {"detail": "Job not found."}


@pytest.mark.parametrize("url", ["/magnet/nope/cancel", "/compare-magnets/nope/cancel"])
def test_cancel_unknown_job_is_404(client, url):
    r = client.post(url)
    assert r.status_code == 404
    assert r.json() == {"detail": "Job not found."}


# ── (a) job-dict key sets per job kind ───────────────────────────────────────

def test_single_upload_job(client, fake_analyze, isolated_uploads):
    r = client.post("/analysis/", files={"file": ("Movie.2024.mkv", b"x" * 100)})
    assert r.status_code == 200
    assert set(r.json()) == {"job_id", "total"}
    assert r.json()["total"] == 1

    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert BASE_KEYS <= set(job)
    assert_job_basics(job)
    assert job["total"] == 1
    assert job["progress"] == "0/1"          # FROZEN: single upload never updates progress
    assert job["current"] == "Movie.2024.mkv"
    assert job["error"] is None
    assert len(job["results"]) == 1
    res = job["results"][0]
    assert res["file"] == "Movie.2024.mkv"   # original name patched back in
    assert res["batch_rank"] == 1
    assert "Movie.2024.mkv" in res["path"]
    assert [e["msg"] for e in job["events"]] == ["Analyzing Movie.2024.mkv…", "✓ Movie.2024.mkv"]
    # the temp upload was deleted after the analysis
    assert os.listdir(isolated_uploads) == []
    # the analyser was called on a path inside UPLOAD_DIR with skip_dovi_scan False
    (path, fast), = fake_analyze.calls
    assert os.path.dirname(path) == str(isolated_uploads)
    assert path.endswith("_Movie.2024.mkv")
    assert fast is False


def test_single_upload_fast_flag_is_passed(client, fake_analyze):
    client.post("/analysis/?fast=true", files={"file": ("a.mkv", b"x")})
    assert fake_analyze.calls[0][1] is True


def test_single_upload_exception_is_error(client, monkeypatch):
    def boom(path, skip_dovi_scan=False):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(main, "analyze_file", boom)
    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert_job_basics(job, "error")
    assert job["error"] == "ffprobe exploded"
    assert job["results"] == []


def test_single_upload_no_data_is_an_error(client, monkeypatch):
    # D4 (RB-10, was a FROZEN ODDITY: ended "done" with no results): analyze_file
    # returning None means it timed out or extracted nothing, so the job is an error.
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    r = client.post("/analysis/", files={"file": ("a.mkv", b"x")})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["results"] == []
    assert job["error"] == "analysis timed out or failed"


def test_single_upload_rejections(client):
    r = client.post("/analysis/", json={"x": 1})
    assert r.status_code == 400
    assert r.json() == {"detail": "Upload must use multipart/form-data with a file field."}
    r = client.post("/analysis/", files={"other": ("a.mkv", b"x")})
    assert r.status_code == 400
    assert r.json() == {"detail": "No uploaded file found. Use field name 'file'."}


def test_batch_upload_job(client, fake_analyze, isolated_uploads):
    r = client.post("/analyze-multiple/", files=[
        ("files", ("a.mkv", b"x")), ("files", ("b.mkv", b"y")),
    ])
    assert r.status_code == 200
    assert set(r.json()) == {"job_id", "total"}
    assert r.json()["total"] == 2

    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert BASE_KEYS <= set(job)
    assert_job_basics(job)
    assert job["total"] == 2
    assert job["progress"] == "2/2"
    assert job["error"] is None
    assert sorted(x["file"] for x in job["results"]) == ["a.mkv", "b.mkv"]
    assert sorted(x["batch_rank"] for x in job["results"]) == [1, 2]
    msgs = [e["msg"] for e in job["events"]]
    assert msgs[0] == "Starting analysis of 2 file(s)…"
    assert msgs[-1] == "Done — 2/2 analysed successfully."
    assert os.listdir(isolated_uploads) == []


def test_batch_upload_all_fail_is_error(client, monkeypatch):
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    r = client.post("/analyze-multiple/", files=[("files", ("a.mkv", b"x"))])
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert_job_basics(job, "error")
    assert job["error"] == "No files could be analyzed."
    assert job["results"] == []


def test_batch_upload_requires_files(client):
    r = client.post("/analyze-multiple/")
    assert r.status_code == 422
    assert "detail" in r.json()


def test_path_job_single_file(client, fake_analyze, tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    r = client.get("/analyze-path/", params={"path": str(f)})
    assert r.status_code == 200
    assert set(r.json()) == {"job_id"}          # NOTE: no "total" for path jobs

    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert BASE_KEYS <= set(job)
    assert_job_basics(job)
    assert job["total"] == 1
    assert job["progress"] == "0/1"             # FROZEN: single-file path job never updates progress
    assert job["error"] is None
    assert [x["file"] for x in job["results"]] == ["Movie.mkv"]
    assert job["results"][0]["batch_rank"] == 1
    msgs = [e["msg"] for e in job["events"]]
    assert msgs == ["Job started", "Analyzing Movie.mkv…", "✓ Movie.mkv", "Done."]
    assert f.exists()                           # real files are never deleted


def test_path_job_folder(client, fake_analyze, tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.mp4").write_bytes(b"x")
    (sub / "notes.txt").write_bytes(b"x")       # ignored: not a video extension
    r = client.get("/analyze-path/", params={"path": str(tmp_path)})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert_job_basics(job)
    assert job["total"] == 2
    assert job["progress"] == "2/2"
    assert sorted(x["file"] for x in job["results"]) == ["a.mkv", "b.mp4"]
    msgs = [e["msg"] for e in job["events"]]
    assert msgs[0] == "Job started"
    assert msgs[1].startswith("Scanning folder: ")
    assert msgs[-1] == "Done."


def test_path_job_empty_folder_is_an_error(client, fake_analyze, tmp_path):
    # D4 (RB-11, was a FROZEN ODDITY: ThreadPoolExecutor(max_workers=0) raised
    # ValueError and the job ended "done" with error text "max_workers..."): an
    # empty folder is checked before the pool exists and ends "error".
    r = client.get("/analyze-path/", params={"path": str(tmp_path)})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["results"] == []
    assert job["error"] == "no video files found"


def test_path_job_exception_is_an_error(client, monkeypatch, tmp_path):
    # D4 (was a FROZEN ODDITY: a failing single-file analysis ended "done"):
    # it now ends "error" with the same error text.
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")

    def boom(path, skip_dovi_scan=False):
        raise RuntimeError("bad file")

    monkeypatch.setattr(main, "analyze_file", boom)
    r = client.get("/analyze-path/", params={"path": str(f)})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["error"] == "bad file"
    assert job["results"] == []


def test_path_job_missing_path_is_404(client, tmp_path):
    # D3: a missing path is now 404 "path not found" (was 400 "File or folder does not exist.").
    r = client.get("/analyze-path/", params={"path": str(tmp_path / "nope.mkv")})
    assert r.status_code == 404
    assert r.json() == {"detail": "path not found"}


def test_path_job_strips_quotes_and_whitespace(client, fake_analyze, tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    r = client.get("/analyze-path/", params={"path": f'  "{f}"  '})
    assert r.status_code == 200


def test_scan_folder_route(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "scan_folder",
                        lambda path, skip_dovi_scan=False, file_filter=None: [make_result("a.mkv")])
    r = client.get("/scan-folder/", params={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["file"] == "a.mkv"   # bare list, no job

    monkeypatch.setattr(main, "scan_folder",
                        lambda path, skip_dovi_scan=False, file_filter=None: [])
    r = client.get("/scan-folder/", params={"path": str(tmp_path)})
    assert r.status_code == 404
    assert r.json() == {"detail": "No supported video files found in this folder."}

    r = client.get("/scan-folder/", params={"path": str(tmp_path / "nope")})
    # D3: a missing folder is now 404 "path not found" (was 400 "Folder does not exist.").
    assert r.status_code == 404
    assert r.json() == {"detail": "path not found"}


def test_magnet_job(client, monkeypatch):
    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory())
    r = client.post("/magnet/", json={"magnet": MAGNET})
    assert r.status_code == 200
    assert set(r.json()) == {"job_id"}

    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert MAGNET_KEYS <= set(job)
    assert "enriched" in job                  # added by the done path (additive field)
    assert_job_basics(job)
    assert job["error"] is None
    assert job["total"] == 1
    assert job["progress"] == "1/1"
    assert job["magnet_torrent"] == {"name": "Movie.2024.2160p.WEB-DL.mkv",
                                     "info_hash": MAGNET[-40:]}
    assert job["magnet_files"] == [{"path": "Movie.2024.2160p.WEB-DL.mkv",
                                    "size": 123, "verdict": "video"}]
    assert len(job["results"]) == 1 and job["results"][0]["batch_rank"] == 1
    enr = job["enriched"][0]
    assert {"name", "magnet_uri", "analysis", "intel", "verification", "badges"} <= set(enr)
    assert enr["magnet_uri"] == MAGNET
    msgs = [e["msg"] for e in job["events"]]
    assert msgs[0] == "Magnet job started"
    assert msgs[-1] == "Done — 1 playable / 1 total file(s)."


def test_magnet_job_failures_are_error(client, monkeypatch):
    for exc in (main.MagnetUnavailable("no libtorrent"), TimeoutError("slow"),
                RuntimeError("boom")):
        def _raise(*a, _e=exc, **k):
            raise _e
        monkeypatch.setattr(main, "run_magnet_job_threaded", _raise)
        r = client.post("/magnet/", json={"magnet": MAGNET})
        job = client.get(f"/job/{r.json()['job_id']}").json()
        assert MAGNET_KEYS <= set(job)        # init-only keys survive a failed job
        assert_job_basics(job, "error")
        assert job["error"] == str(exc)
        assert job["results"] == []


def test_magnet_rejections(client):
    r = client.post("/magnet/", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Invalid JSON body: ")
    r = client.post("/magnet/", json={})
    assert r.status_code == 400
    assert r.json() == {"detail": "Missing `magnet` field in JSON body."}
    r = client.post("/magnet/", json={"magnet": "http://x"})
    assert r.status_code == 400
    assert r.json() == {"detail": "Not a magnet URI — must start with 'magnet:?'."}


def test_magnet_cancel_known_job(client, monkeypatch):
    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory())
    jid = client.post("/magnet/", json={"magnet": MAGNET}).json()["job_id"]
    r = client.post(f"/magnet/{jid}/cancel")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_compare_job_sequential(client, monkeypatch):
    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory({
        MAGNET: "Movie.2024.2160p.WEB-DL.mkv",
        MAGNET_B: "Movie.2024.2160p.BluRay.REMUX.mkv",
    }))
    r = client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B]})
    assert r.status_code == 200
    assert set(r.json()) == {"job_id", "total"}
    assert r.json()["total"] == 2

    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert COMPARE_KEYS <= set(job)
    assert_job_basics(job)
    assert job["total"] == 2 and job["progress"] == "2/2"
    assert job["mode"] == "movie"
    assert job["episode_comparison"] is None
    assert job["dropped"] == []
    assert job["error"] is None
    assert job["results"] == []               # compare jobs never fill "results"
    assert len(job["per_magnet"]) == 2
    pm = job["per_magnet"][0]
    assert set(pm) == {"index", "magnet", "status", "error", "torrent", "files",
                       "analyses", "events"}
    assert pm["status"] == "done"
    assert set(job["comparison"]) == {"releases", "comparison_matrix", "winner"}
    assert {"winner_index", "winner_name", "reasons"} <= set(job["winner"])
    assert job["winner"] == job["comparison"]["winner"]
    assert len(job["comparison"]["releases"]) == 2
    assert {"composite_score", "score_reasons", "release_recommendation"} <= set(
        job["comparison"]["releases"][0])
    msgs = [e["msg"] for e in job["events"]]
    assert msgs[0] == "Comparing 2 magnet(s) — sequential."
    assert msgs[-1] == "Done — comparison built."
    assert any(m.startswith("[#1] ") for m in msgs)


def test_compare_job_parallel_mode_message(client, monkeypatch):
    monkeypatch.setattr(main, "run_magnet_job_threaded", fake_magnet_factory())
    r = client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B, MAGNET_C]})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert_job_basics(job)
    assert job["events"][0]["msg"] == "Comparing 3 magnet(s) — parallel (limit 2)."
    assert job["total"] == 3 and job["progress"] == "3/3"


def test_compare_job_no_analyses_is_error(client, monkeypatch):
    def _raise(*a, **k):
        raise TimeoutError("no peers")

    monkeypatch.setattr(main, "run_magnet_job_threaded", _raise)
    r = client.post("/compare-magnets/", json={"magnets": [MAGNET, MAGNET_B]})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert COMPARE_KEYS <= set(job)           # init-only keys survive a failed job
    assert_job_basics(job, "error")
    assert job["error"] == "No magnet produced analyzable files."
    assert len(job["dropped"]) == 2
    assert set(job["dropped"][0]) == {"index", "magnet", "name", "reason"}
    assert job["dropped"][0]["reason"] == "no peers"
    assert job["events"][-1]["msg"] == "Done — no comparable releases."


def test_compare_rejections(client):
    cases = [
        ({}, "Provide a non-empty `magnets` array in the JSON body."),
        ({"magnets": []}, "Provide a non-empty `magnets` array in the JSON body."),
        ({"magnets": "x"}, "Provide a non-empty `magnets` array in the JSON body."),
        ({"magnets": [MAGNET] * 9}, "Up to 8 magnets at a time."),
        ({"magnets": [1]}, "magnets[0] is not a string."),
        ({"magnets": ["http://x"]}, "magnets[0] is not a magnet URI."),
    ]
    for body, detail in cases:
        r = client.post("/compare-magnets/", json=body)
        assert r.status_code == 400, body
        assert r.json() == {"detail": detail}, body


# ── (b) SSE line format ──────────────────────────────────────────────────────

def test_sse_line_format(client, fake_analyze, tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    jid = client.get("/analyze-path/", params={"path": str(f)}).json()["job_id"]
    r = client.get(f"/job/{jid}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"

    body = r.text
    assert body.endswith("data: __done__\n\n")
    frames = body.split("\n\n")
    assert frames[-1] == ""                    # trailing terminator split
    frames = frames[:-1]
    assert frames[-1] == "data: __done__"
    events = []
    for fr in frames[:-1]:
        assert fr.startswith("data: ") and "\n" not in fr
        events.append(json.loads(fr[len("data: "):]))
    assert all(set(e) == {"msg", "ts"} for e in events)
    assert [e["msg"] for e in events] == ["Job started", "Analyzing Movie.mkv…",
                                          "✓ Movie.mkv", "Done."]
    # frames are JSON as produced by json.dumps with default separators
    assert frames[0] == "data: " + json.dumps(events[0])


def test_sse_stream_ends_on_error_status(client, monkeypatch):
    monkeypatch.setattr(main, "analyze_file", lambda p, skip_dovi_scan=False: None)
    jid = client.post("/analyze-multiple/",
                      files=[("files", ("a.mkv", b"x"))]).json()["job_id"]
    body = client.get(f"/job/{jid}/events").text
    assert body.endswith("data: __done__\n\n")


# ── (d) rank_results ordering ────────────────────────────────────────────────

def test_rank_results_order_three_records():
    a = {"file": "a.mkv", "tv_score": 90, "score": 10, "confidence_score": 10, "bitrate_mbps": 5}
    b = {"file": "b.mkv", "tv_score": 90, "score": 20, "confidence_score": 10, "bitrate_mbps": 5}
    c = {"file": "c.mkv", "tv_score": 95, "score": 1, "confidence_score": 1, "bitrate_mbps": 1}
    ranked = main.rank_results([a, b, c])
    assert [r["file"] for r in ranked] == ["c.mkv", "b.mkv", "a.mkv"]   # tv_score, then score
    assert [r["batch_rank"] for r in ranked] == [1, 2, 3]


def test_rank_results_tiebreaks_and_missing_keys():
    base = {"tv_score": 50, "score": 50, "confidence_score": 50, "bitrate_mbps": 10}
    x = dict(base, file="x.mkv", confidence_score=60)
    y = dict(base, file="y.mkv", bitrate_mbps=30)
    z = dict(base, file="z.mkv")
    ranked = main.rank_results([z, y, x])
    # confidence_score outranks bitrate; file name (descending) is the last tiebreak
    assert [r["file"] for r in ranked] == ["x.mkv", "y.mkv", "z.mkv"]
    same = main.rank_results([dict(base, file="m.mkv"), dict(base, file="n.mkv")])
    assert [r["file"] for r in same] == ["n.mkv", "m.mkv"]
    # missing keys default to 0 / ""
    assert [r["batch_rank"] for r in main.rank_results([{}, {"tv_score": 1}])] == [1, 2]


def test_rank_results_sorts_in_place_and_returns_same_list():
    items = [{"file": "a", "tv_score": 1}, {"file": "b", "tv_score": 2}]
    out = main.rank_results(items)
    assert out is items
    assert items[0]["file"] == "b"


def test_batch_job_results_are_ranked(client, monkeypatch):
    scores = {"low.mkv": 10, "high.mkv": 99, "mid.mkv": 50}
    monkeypatch.setattr(
        main, "analyze_file",
        lambda path, skip_dovi_scan=False: make_result(
            os.path.basename(path).split("_", 1)[1],
            tv_score=scores[os.path.basename(path).split("_", 1)[1]]))
    r = client.post("/analyze-multiple/", files=[
        ("files", ("low.mkv", b"x")), ("files", ("high.mkv", b"x")),
        ("files", ("mid.mkv", b"x")),
    ])
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert [x["file"] for x in job["results"]] == ["high.mkv", "mid.mkv", "low.mkv"]
    assert [x["batch_rank"] for x in job["results"]] == [1, 2, 3]


# ── (e) Plan 4B suite documents: the real emitted fixtures carry the frozen shared-contract keys ──

SUITE4B = os.path.join(os.path.dirname(__file__), "fixtures", "suite4b")
PLAN_KEYS = {"schema", "tool_version", "results", "selection", "mode", "records", "candidates", "pairs", "estimate",
             "records_digest", "failures"}
CANDIDATE_KEYS = {"path", "id", "status", "reason", "size", "mtime_ns"}
ADDITIVE_KEYS = {"results", "selection", "candidates", "records_digest", "coverage"}
COVERAGE_KEYS = {"expected_pairs", "scheduled_pairs", "completed_pairs", "missing_pairs"}
QUICK_KEYS = {"schema", "tool_version", "results", "measurement", "records", "overall", "devices", "selected",
              "alternatives", "failures", "settings"}
ORDER_KEYS = {"groups", "winner_ids", "coverage_complete"}
GROUP_KEYS = {"rank", "ids", "status", "basis", "limitations"}


def _suite4b(name: str) -> dict:
    with open(os.path.join(SUITE4B, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_suite4b_plan_and_compare_keep_legacy_and_additive_keys():
    plan = _suite4b("compare_plan")
    assert set(plan) == PLAN_KEYS
    assert all(set(c) == CANDIDATE_KEYS for c in plan["candidates"])
    assert set(plan["estimate"]) == {"pairs", "minutes"} and all(set(p) == {"a", "b"} for p in plan["pairs"])
    compare = _suite4b("compare_all")
    assert {"mode", "estimate", "shortlist", "dropped", "pairs", "failed"} | ADDITIVE_KEYS <= set(compare)
    assert set(compare["coverage"]) == COVERAGE_KEYS and compare["mode"] == "accurate"
    for name in ("rank_overall", "rank_device"):
        rank = _suite4b(name)
        assert {"quality", "same_picture_groups", "not_compared", "missing_pairs", "weights", "devices"} | ADDITIVE_KEYS <= set(rank)
        assert set(rank["coverage"]) == COVERAGE_KEYS
    assert _suite4b("rank_overall")["devices"] == {}
    device = _suite4b("rank_device")
    assert device["selected"] == {"device": "sony_bravia_8_ii", "player": "just_player"}
    assert device["evaluated_players"] == ["builtin_usb", "kodi", "just_player"]


def test_suite4b_quick_documents_follow_the_quick_contract():
    expectations = {"quick_overall": ("overall", True, False), "quick_device": ("device", False, True),
                    "quick_both": ("both", True, True), "quick_partial": ("both", True, True),
                    "quick_just_player_alternatives": ("both", True, True)}
    for name, (results, has_overall, has_devices) in expectations.items():
        doc = _suite4b(name)
        assert set(doc) == QUICK_KEYS, name
        assert doc["results"] == results and doc["measurement"] == "metadata"
        assert (doc["overall"] is not None) == has_overall and bool(doc["devices"]) == has_devices
        orders = ([doc["overall"]] if doc["overall"] else []) + list(doc["devices"].values())
        for order in orders:
            assert set(order) == ORDER_KEYS
            assert all(set(g) == GROUP_KEYS for g in order["groups"])
    partial = _suite4b("quick_partial")
    assert all(r["source"]["partial"] is True for r in partial["records"])
    assert partial["overall"]["winner_ids"] == []
