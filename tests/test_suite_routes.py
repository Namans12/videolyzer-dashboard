"""Tests for the /suite routes (plan 2, task D11).

No real subprocess: routes get a fake client through ``app.dependency_overrides[get_suite_client]``;
the cancel test drives the real ``SuiteClient`` with a fake process (injected ``popen``/``killer``).
TestClient runs BackgroundTasks before the response returns, so HTTP tests assert final job
states only; "running" behaviour is tested by calling the runners in a worker thread."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import threading
from pathlib import Path

import pytest

import jobs as jobs_module
import main
import suite_routes
from suite_client import (ERROR_KINDS, SuiteArgumentError, SuiteClient, SuiteError, SuiteUnavailable,
                          get_suite_client)

FIX = Path(__file__).parent / "fixtures" / "suite"
UNAVAILABLE = "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"


def fixture_doc(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# ── fakes ────────────────────────────────────────────────────────────────────────

class FakeClient:
    """Stands in for SuiteClient. ``results[name]`` is a document, an exception to raise, or a callable."""

    def __init__(self, **results):
        self.results = results
        self.calls: list[tuple[str, tuple, dict]] = []

    def _go(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        value = self.results.get(name)
        if callable(value):
            return value(*args, **kwargs)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value) if value is not None else fixture_doc(f"{name}.json")

    def devices(self):
        return self._go("devices", (), {})

    def score(self, *args, **kwargs):
        return self._go("score", args, kwargs)

    def rank(self, *args, **kwargs):
        return self._go("rank", args, kwargs)

    def compare(self, *args, **kwargs):
        return self._go("compare", args, kwargs)


class VersionFakeClient(FakeClient):
    def version(self):
        return self._go("version", (), {})


@pytest.mark.parametrize("value,status", [("0.4.0", "ok"), ("0.2.9", "too_old"),
                                          (SuiteUnavailable("secret"), "unavailable")])
def test_version_route_returns_200_and_refreshes(client, value, status):
    import suite_version
    f = VersionFakeClient(version=value if isinstance(value, Exception) else
                          dict(fixture_doc("version.json"), tool_version=value))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    response = client.get("/suite/version")
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert client.get("/suite/version").json() == response.json()
    assert len(f.calls) == 1
    assert client.get("/suite/version?refresh=true").status_code == 200
    assert len(f.calls) == 2
    assert suite_version.cached()["status"] == status


def test_version_route_factory_unavailable_is_cached_with_short_ttl(client, monkeypatch):
    import suite_version
    now = [0.0]
    monkeypatch.setattr(suite_version, "_now", lambda: now[0])
    calls = []
    def factory():
        calls.append(True)
        raise SuiteUnavailable("secret")
    main.app.dependency_overrides[get_suite_client] = factory
    response = client.get("/suite/version")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["message"] == UNAVAILABLE
    assert suite_version.cached() == response.json()
    now[0] = 5
    assert client.get("/suite/version").json() == response.json()
    now[0] = 11
    assert client.get("/suite/version").json()["checked_at"] == 11
    assert client.get("/suite/version?refresh=true").status_code == 200
    assert suite_version.cached()["checked_at"] == 11


def test_devices_version_gate_returns_reinstall_message(client):
    f = VersionFakeClient(version=dict(fixture_doc("version.json"), tool_version="0.2.9"))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    response = client.get("/suite/devices")
    assert response.status_code == 502
    assert response.json() == {"detail": "The installed VideoSuite is 0.2.9; the Dashboard needs 0.3.0 "
                              "or later. Reinstall the Comparizer (run the latest Video Comparator "
                              "installer), then restart the Dashboard."}
    assert [call[0] for call in f.calls] == ["version"]


@pytest.mark.parametrize("exc,kind", [(RuntimeError("secret"), "internal"),
                                     (SuiteError("contract", "secret"), "contract"),
                                     (SuiteError("usage", "secret"), "usage"),
                                     (SuiteError("unavailable", "secret"), "unavailable")])
def test_version_discovery_errors_are_safe_and_short_cached(client, monkeypatch, exc, kind):
    import suite_version
    now = [0.0]
    monkeypatch.setattr(suite_version, "_now", lambda: now[0])
    def factory():
        raise exc
    main.app.dependency_overrides[get_suite_client] = factory
    response = client.get("/suite/version")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == ("unavailable" if kind == "unavailable" else "error")
    assert result["message"] == (UNAVAILABLE if kind == "unavailable" else
                                  f"VideoSuite did not answer the version check ({kind}).")
    assert suite_version.cached() == result
    now[0] = 5
    assert client.get("/suite/version").json() == result
    now[0] = 11
    assert client.get("/suite/version").json()["checked_at"] == 11


@pytest.mark.parametrize("field,value", [("tool_version", object()), ("capabilities", object()),
                                        ("capabilities", [object()]), ("capabilities", None),
                                        ("schemas", {"x": object()}), ("schemas", None),
                                        ("engine_tag", object())])
def test_version_malformed_metadata_never_causes_http_500(client, field, value):
    doc = fixture_doc("version.json")
    doc[field] = value
    main.app.dependency_overrides[get_suite_client] = lambda: VersionFakeClient(version=doc)
    response = client.get("/suite/version")
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["message"] == "VideoSuite did not answer the version check (contract)."


@pytest.mark.parametrize("value", ["0.4.0", SuiteUnavailable("missing"), "v1"])
def test_devices_other_version_statuses_keep_working(client, value):
    f = VersionFakeClient(version=value if isinstance(value, Exception) else
                          dict(fixture_doc("version.json"), tool_version=value))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    assert client.get("/suite/devices").json() == fixture_doc("devices.json")
    assert [call[0] for call in f.calls] == ["version", "devices"]


@pytest.fixture
def fake(client):
    """A FakeClient wired in as the suite client; the override is removed afterwards."""
    f = FakeClient()
    main.app.dependency_overrides[get_suite_client] = lambda: f
    yield f
    main.app.dependency_overrides.pop(get_suite_client, None)


@pytest.fixture
def videos(tmp_path):
    a, b = tmp_path / "a.mkv", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    return [str(a), str(b)]


@pytest.fixture(autouse=True)
def no_allowed_roots(monkeypatch):
    monkeypatch.delenv("VIDEOLYZER_ALLOWED_ROOTS", raising=False)


def last_job():
    ids = jobs_module.jobs.ids()
    assert len(ids) == 1, ids
    return jobs_module.jobs.get(ids[0])


def no_jobs():
    return len(jobs_module.jobs) == 0


# ── /suite/devices ───────────────────────────────────────────────────────────────

def test_devices_returns_the_document_and_caches_it_for_60_seconds(client, fake, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(suite_routes, "_clock", lambda: now[0])
    r1 = client.get("/suite/devices")
    assert r1.status_code == 200
    assert r1.json() == fixture_doc("devices.json")
    now[0] += 59
    assert client.get("/suite/devices").json() == r1.json()
    assert [c[0] for c in fake.calls] == ["devices"]            # second call came from the cache
    now[0] += 2                                                 # 61 s after the first fetch
    client.get("/suite/devices")
    assert [c[0] for c in fake.calls] == ["devices", "devices"]


def test_devices_cache_is_cleared_by_clean_state_helper(client, fake):
    client.get("/suite/devices")
    suite_routes.reset_state()
    client.get("/suite/devices")
    assert len(fake.calls) == 2


@pytest.mark.parametrize("route", ["/suite/devices", "/suite/settings"])
def test_explicit_refresh_rereads_shared_cache_and_failure_is_not_cached(client, fake, route):
    original = client.get(route)
    assert original.status_code == 200
    assert client.get(route).json() == original.json()
    assert len(fake.calls) == 1
    changed = fixture_doc("devices.json")
    changed["tool_version"] = "0.5.1"
    fake.results["devices"] = changed
    assert client.get(route + "?refresh=true").json()["tool_version"] == "0.5.1"
    assert len(fake.calls) == 2
    assert client.get("/suite/devices").json() == changed
    assert len(fake.calls) == 2
    fake.results["devices"] = SuiteError("timeout", "secret")
    assert client.get(route + "?refresh=true").status_code == 504
    fake.results["devices"] = fixture_doc("devices.json")
    assert client.get(route).json() == original.json()
    assert len(fake.calls) == 4


def test_devices_cache_is_empty_at_the_start_of_every_test(client, fake):
    # clean_state must have cleared what the previous tests cached
    client.get("/suite/devices")
    assert len(fake.calls) == 1


def test_devices_unavailable_is_503_with_the_exact_detail(client):
    def boom():
        raise SuiteUnavailable("The VideoSuite installation was not found (secret C:\\path).")
    main.app.dependency_overrides[get_suite_client] = boom
    try:
        r = client.get("/suite/devices")
    finally:
        main.app.dependency_overrides.pop(get_suite_client, None)
    assert r.status_code == 503
    assert r.json() == {"detail": UNAVAILABLE}


@pytest.mark.parametrize("kind,status", [("contract", 502), ("failed", 502), ("usage", 502), ("no_files", 502),
                                         ("ffprobe_missing", 502), ("timeout", 504), ("unavailable", 503)])
def test_devices_suite_errors_map_to_502_504(client, fake, kind, status):
    fake.results["devices"] = SuiteError(kind, "short message", exit_code=9, stderr_tail="SECRET-STDERR C:\\x")
    r = client.get("/suite/devices")
    assert r.status_code == status
    assert "SECRET-STDERR" not in r.text
    assert "C:\\x" not in r.text


def test_devices_usage_error_says_the_suite_is_too_old(client, fake):
    fake.results["devices"] = SuiteError("usage", "videosuite devices exited with code 2.", exit_code=2,
                                         stderr_tail="usage: videosuite ... invalid choice: 'devices' SECRET")
    r = client.get("/suite/devices")
    assert r.status_code == 502
    assert r.json() == {"detail": "VideoSuite does not support 'devices'; reinstall the Comparizer "
                                  "(videosuite 0.3.0 or later)"}
    assert "SECRET" not in r.text


def test_devices_other_502_details_are_unchanged(client, fake):
    fake.results["devices"] = SuiteError("failed", "m", exit_code=9)
    assert client.get("/suite/devices").json() == {"detail": "VideoSuite failed."}
    fake.results["devices"] = SuiteError("contract", "m")
    assert client.get("/suite/devices").json() == {"detail": "VideoSuite returned an unusable response."}


def test_devices_errors_are_not_cached(client, fake):
    fake.results["devices"] = SuiteError("failed", "x")
    assert client.get("/suite/devices").status_code == 502
    fake.results["devices"] = None
    assert client.get("/suite/devices").status_code == 200


def test_unavailable_on_job_post_is_503_and_no_job(client, videos):
    def boom():
        raise SuiteUnavailable("nope")
    main.app.dependency_overrides[get_suite_client] = boom
    try:
        for url, body in [("/suite/score", {"paths": videos}),
                          ("/suite/rank", {"paths": videos, "reports_dir": str(Path(videos[0]).parent)}),
                          ("/suite/compare", {"paths": videos, "out_dir": str(Path(videos[0]).parent / "o"), "mode": "fast"})]:
            r = client.post(url, json=body)
            assert r.status_code == 503, url
            assert r.json() == {"detail": UNAVAILABLE}
    finally:
        main.app.dependency_overrides.pop(get_suite_client, None)
    assert no_jobs()


# ── job creation: happy paths ────────────────────────────────────────────────────

def test_score_creates_a_job_that_ends_done_with_the_cli_json(client, fake, videos):
    r = client.post("/suite/score", json={"paths": videos, "device": "sony_bravia_8_ii", "player": "builtin_usb",
                                         "fs": "exfat", "lang": "eng", "deep_dv": True})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    job = client.get(f"/job/{job_id}").json()
    assert job["status"] == "done"
    assert job["kind"] == "suite_score"
    assert job["results"] == fixture_doc("score.json")
    assert job["error"] is None and job["error_kind"] is None
    name, args, kwargs = fake.calls[0]
    assert name == "score"
    assert list(args[0]) == [str(Path(p).resolve()) for p in videos]
    assert callable(kwargs.pop("cancel"))
    assert kwargs == {"device": "sony_bravia_8_ii", "player": "builtin_usb", "fs": "exfat", "lang": "eng", "deep_dv": True}


def test_score_with_only_paths_passes_no_optional_values(client, fake, videos):
    r = client.post("/suite/score", json={"paths": videos})
    assert r.status_code == 200
    kwargs = dict(fake.calls[0][2])
    assert callable(kwargs.pop("cancel"))
    assert kwargs == {}


def test_rank_creates_a_job_that_ends_done(client, fake, videos, tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    r = client.post("/suite/rank", json={"paths": videos, "reports_dir": str(reports), "wq": 0.7, "wp": 0.3,
                                        "lang": "eng"})
    assert r.status_code == 200
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert (job["status"], job["kind"]) == ("done", "suite_rank")
    assert job["results"] == fixture_doc("rank.json")
    name, args, kwargs = fake.calls[0]
    assert name == "rank"
    assert args[1] == str(reports.resolve())
    assert callable(kwargs.pop("cancel"))
    assert kwargs == {"wq": 0.7, "wp": 0.3, "lang": "eng"}


def test_compare_creates_a_job_that_ends_done_with_progress_events(client, fake, videos, tmp_path):
    def compare(paths, out_dir, mode, top, *, on_line=None, cancel=None, **kw):
        on_line("2 file(s) shortlisted")
        on_line("pair 1/1")
        return fixture_doc("compare.json")
    fake.results["compare"] = compare
    out = tmp_path / "out"
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(out), "mode": "accurate", "top": 3})
    assert r.status_code == 200
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert (job["status"], job["kind"]) == ("done", "suite_compare")
    assert job["results"] == fixture_doc("compare.json")
    msgs = [e["msg"] for e in job["events"]]
    assert "2 file(s) shortlisted" in msgs and "pair 1/1" in msgs
    _, args, kwargs = fake.calls[0]
    assert args[1] == str((tmp_path / "out").resolve())
    assert args[2:4] == ("accurate", 3)
    assert callable(kwargs["on_line"]) and callable(kwargs["cancel"])


def test_compare_top_is_optional(client, fake, videos, tmp_path):
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"})
    assert r.status_code == 200
    assert client.get(f"/job/{r.json()['job_id']}").json()["status"] == "done"


# ── request validation ───────────────────────────────────────────────────────────

def test_unc_path_is_400_and_creates_no_job(client, fake):
    r = client.post("/suite/score", json={"paths": ["\\\\server\\share\\a.mkv"]})
    assert r.status_code == 400
    assert "server" not in r.text
    assert no_jobs() and fake.calls == []


@pytest.mark.parametrize("raw", ["relative\\a.mkv", "C:a.mkv", "\\\\?\\C:\\a.mkv", "C:\\a.mkv::$DATA", "NUL"])
def test_bad_paths_are_rejected_without_a_job(client, fake, raw):
    r = client.post("/suite/score", json={"paths": [raw]})
    assert r.status_code in (400, 404)
    assert no_jobs()


def test_one_bad_path_among_good_ones_rejects_the_whole_request(client, fake, videos):
    r = client.post("/suite/score", json={"paths": videos + ["\\\\h\\s\\c.mkv"]})
    assert r.status_code == 400
    assert no_jobs()


def test_folder_and_non_video_paths_are_400(client, fake, tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    assert client.post("/suite/score", json={"paths": [str(tmp_path)]}).status_code == 400
    assert client.post("/suite/score", json={"paths": [str(txt)]}).status_code == 400
    assert no_jobs()


def test_missing_file_is_404_without_a_job(client, fake, tmp_path):
    r = client.post("/suite/score", json={"paths": [str(tmp_path / "gone.mkv")]})
    assert r.status_code == 404
    assert no_jobs()


def test_allowed_roots_are_enforced(client, fake, videos, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(other))
    r = client.post("/suite/score", json={"paths": videos})
    assert r.status_code == 403
    assert no_jobs()


def test_51_paths_is_422_and_50_is_accepted(client, fake, videos):
    r = client.post("/suite/score", json={"paths": [videos[0]] * 51})
    assert r.status_code == 422
    assert no_jobs()
    r = client.post("/suite/score", json={"paths": [videos[0]] * 50})
    assert r.status_code == 200


def test_empty_paths_is_422(client, fake):
    assert client.post("/suite/score", json={"paths": []}).status_code == 422
    assert client.post("/suite/score", json={}).status_code == 422


@pytest.mark.parametrize("url,extra", [("/suite/score", {}), ("/suite/rank", {"reports_dir": "C:\\"}),
                                       ("/suite/compare", {"out_dir": "C:\\o", "mode": "fast"})])
def test_extra_field_is_422(client, fake, videos, url, extra):
    r = client.post(url, json={"paths": videos, "bogus": 1, **extra})
    assert r.status_code == 422
    assert no_jobs()


@pytest.mark.parametrize("field,value", [
    ("lang", "eng&calc"), ("lang", "e"), ("lang", ""), ("lang", 5),
    ("device", "a&b"), ("device", "x y"), ("device", ""), ("player", "../x"), ("player", "a" * 65),
    ("fs", "ext4"), ("fs", "NTFS"), ("deep_dv", {"a": 1}),
])
def test_score_value_validation_is_422(client, fake, videos, field, value):
    r = client.post("/suite/score", json={"paths": videos, field: value})
    assert r.status_code == 422, r.text
    assert no_jobs()


@pytest.mark.parametrize("value", ["true", "yes", 1, 0, "1"])
def test_deep_dv_must_be_a_real_boolean(client, fake, videos, value):
    r = client.post("/suite/score", json={"paths": videos, "deep_dv": value})
    assert r.status_code == 422, r.text
    assert no_jobs()


def test_deep_dv_false_is_accepted_and_passed_on(client, fake, videos):
    assert client.post("/suite/score", json={"paths": videos, "deep_dv": False}).status_code == 200
    assert fake.calls[0][2]["deep_dv"] is False


def test_reports_dir_and_out_dir_are_capped_at_4096_characters(client, fake, videos):
    long = "C:\\" + "a" * 4100
    r = client.post("/suite/rank", json={"paths": videos, "reports_dir": long})
    assert r.status_code == 422
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": long, "mode": "fast"})
    assert r.status_code == 422
    assert no_jobs()


def test_422_for_a_bad_lang_does_not_crash_the_error_encoder(client, fake, videos):
    r = client.post("/suite/score", json={"paths": videos, "lang": "eng&calc"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


@pytest.mark.parametrize("body", [{"wq": 1001}, {"wq": -1}, {"wp": 1000.5}, {"wq": "0.5"}, {"wq": True},
                                  {"wq": 0, "wp": 0}])
def test_rank_weights_are_validated(client, fake, videos, tmp_path, body):
    reports = tmp_path / "r"
    reports.mkdir()
    r = client.post("/suite/rank", json={"paths": videos, "reports_dir": str(reports), **body})
    assert r.status_code == 422, r.text
    assert no_jobs()


def test_rank_weights_at_the_limits_are_accepted(client, fake, videos, tmp_path):
    reports = tmp_path / "r"
    reports.mkdir()
    r = client.post("/suite/rank", json={"paths": videos, "reports_dir": str(reports), "wq": 0, "wp": 1000})
    assert r.status_code == 200


@pytest.mark.parametrize("body", [{"mode": "slow"}, {"mode": "fast", "top": 0}, {"mode": "fast", "top": 7},
                                  {"mode": "fast", "top": True}, {"mode": "fast", "top": "3"}, {}])
def test_compare_value_validation_is_422(client, fake, videos, tmp_path, body):
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), **body})
    assert r.status_code == 422, r.text
    assert no_jobs()


def test_out_dir_with_ampersand_is_400(client, fake, videos, tmp_path):
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "out&calc"), "mode": "fast"})
    assert r.status_code == 400
    assert "calc" not in r.text
    assert no_jobs()


@pytest.mark.parametrize("name", ["out%x", "out^x", 'out"x', "out!x", "out|x"])
def test_out_dir_with_shell_characters_is_400(client, fake, videos, tmp_path, name):
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / name), "mode": "fast"})
    assert r.status_code == 400
    assert no_jobs()


def test_out_dir_parent_must_exist_and_unc_is_rejected(client, fake, videos, tmp_path):
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "nope" / "out"), "mode": "fast"})
    assert r.status_code == 404
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": "\\\\h\\s\\out", "mode": "fast"})
    assert r.status_code == 400
    assert no_jobs()


def test_out_dir_outside_the_allowed_roots_is_403(client, fake, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    video = allowed / "x.mkv"
    video.write_bytes(b"x")
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(allowed))
    ok = client.post("/suite/compare", json={"paths": [str(video)], "out_dir": str(allowed / "out"), "mode": "fast"})
    assert ok.status_code == 200
    jobs_module.jobs.clear()
    r = client.post("/suite/compare", json={"paths": [str(video)], "out_dir": str(tmp_path / "out"), "mode": "fast"})
    assert r.status_code == 403
    assert no_jobs()


def test_out_dir_that_is_a_file_is_400(client, fake, videos, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(f), "mode": "fast"})
    assert r.status_code == 400
    assert no_jobs()


def test_reports_dir_must_be_an_existing_folder(client, fake, videos, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    for bad, status in [(str(tmp_path / "missing"), 404), (str(f), 400), ("\\\\h\\s\\r", 400)]:
        r = client.post("/suite/rank", json={"paths": videos, "reports_dir": bad})
        assert r.status_code == status, bad
    assert no_jobs()


def test_reports_dir_with_ampersand_is_400(client, fake, videos, tmp_path):
    d = tmp_path / "rep&x"
    d.mkdir()
    r = client.post("/suite/rank", json={"paths": videos, "reports_dir": str(d)})
    assert r.status_code == 400
    assert no_jobs()


def test_text_plain_post_is_415_and_cross_site_is_403(client, fake, videos):
    body = json.dumps({"paths": videos})
    r = client.post("/suite/score", content=body, headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = client.post("/suite/score", json={"paths": videos}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/suite/score", json={"paths": videos}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.get("/suite/devices", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert no_jobs() and fake.calls == []


# ── job outcomes ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ERROR_KINDS)
@pytest.mark.parametrize("route", ["score", "rank", "compare"])
def test_every_suite_error_kind_ends_the_job_with_that_kind(client, fake, videos, tmp_path, route, kind):
    tail = "line\n" * 200 + "SECRET-TAIL-END"
    fake.results[route] = SuiteError(kind, "videosuite failed.", exit_code=3, stderr_tail=tail)
    reports = tmp_path / "rep"
    reports.mkdir()
    bodies = {"score": {"paths": videos}, "rank": {"paths": videos, "reports_dir": str(reports)},
              "compare": {"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"}}
    r = client.post(f"/suite/{route}", json=bodies[route])
    assert r.status_code == 200
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["error_kind"] == kind
    assert job["error"] == "videosuite failed."
    assert "SECRET-TAIL-END" not in job["error"]
    assert job["error_detail"].endswith("SECRET-TAIL-END")
    assert len(job["error_detail"]) <= 500


@pytest.mark.parametrize("route", ["score", "rank", "compare"])
def test_the_cli_exit_code_is_stored_in_the_job(client, fake, videos, tmp_path, route):
    fake.results[route] = SuiteError("failed", "videosuite failed.", exit_code=7, stderr_tail="x")
    reports = tmp_path / "rep"
    reports.mkdir()
    bodies = {"score": {"paths": videos}, "rank": {"paths": videos, "reports_dir": str(reports)},
              "compare": {"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"}}
    r = client.post(f"/suite/{route}", json=bodies[route])
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["exit_code"] == 7


def test_an_error_without_an_exit_code_stores_none(client, fake, videos):
    fake.results["score"] = SuiteError("unavailable", "could not start")
    r = client.post("/suite/score", json={"paths": videos})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job.get("exit_code") is None


def test_failed_pairs_stores_the_document_exit_code_and_a_finished_progress(client, fake, videos, tmp_path):
    from suite_client import SuiteDocument
    doc = SuiteDocument(fixture_doc("compare.json"))
    doc["failed"] = 2
    doc.exit_code = 1
    fake.results["compare"] = doc
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert (job["status"], job["error_kind"], job["exit_code"], job["progress"]) == ("error", "failed_pairs", 1, "1/1")


def test_a_short_stderr_tail_is_kept_whole_in_error_detail(client, fake, videos):
    fake.results["score"] = SuiteError("failed", "m", stderr_tail="ffprobe exploded")
    r = client.post("/suite/score", json={"paths": videos})
    assert client.get(f"/job/{r.json()['job_id']}").json()["error_detail"] == "ffprobe exploded"


def test_compare_with_failed_pairs_keeps_the_results_and_ends_error(client, fake, videos, tmp_path):
    doc = fixture_doc("compare.json")
    doc["failed"] = 1
    fake.results["compare"] = doc
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["error_kind"] == "failed_pairs"
    assert job["results"] == doc
    assert "1" in job["error"]


def test_unexpected_exception_ends_the_job_internal_without_leaking_text(client, fake, videos):
    fake.results["score"] = RuntimeError("boom C:\\secret")
    r = client.post("/suite/score", json={"paths": videos})
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error"
    assert job["error_kind"] == "internal"
    assert "secret" not in json.dumps(job)


def test_argument_error_inside_the_runner_is_a_usage_error():
    job_id = jobs_module.jobs.create("suite_score")
    client = FakeClient(score=SuiteArgumentError("lang is not a valid language code"))
    suite_routes.run_score_job(job_id, client, ["C:\\a.mkv"])
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error_kind"]) == ("error", "usage")


def test_cancel_route_works_for_a_suite_job(client, fake, videos):
    job_id = jobs_module.jobs.create("suite_score")
    r = client.post(f"/job/{job_id}/cancel")
    assert r.status_code == 200
    assert jobs_module.jobs.is_cancelled(job_id)


# ── runners called directly: concurrency and cancel ──────────────────────────────

class BlockingClient:
    def __init__(self, doc_name="compare.json"):
        self.started = threading.Event()
        self.release = threading.Event()
        self.doc_name = doc_name
        self.calls = 0

    def _block(self, **kw):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10)
        return fixture_doc(self.doc_name)

    def compare(self, *a, **k):
        return self._block()

    def score(self, *a, **k):
        return self._block()

    def rank(self, *a, **k):
        return self._block()


def start_in_thread(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def test_second_compare_while_one_runs_is_409_and_the_slot_is_freed_afterwards(client, fake, videos, tmp_path):
    blocker = BlockingClient()
    job1 = jobs_module.jobs.create("suite_compare")
    t = start_in_thread(suite_routes.run_compare_job, job1, blocker, videos, str(tmp_path / "o1"), "fast", 4)
    assert blocker.started.wait(10)
    try:
        r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o2"), "mode": "fast"})
        assert r.status_code == 409
        assert r.json() == {"detail": "a comparison is already running"}
        assert len(jobs_module.jobs) == 1                        # the refused request created no job
        # a score job is not blocked by a running compare
        assert client.post("/suite/score", json={"paths": videos}).status_code == 200
    finally:
        blocker.release.set()
        t.join(10)
    assert jobs_module.jobs.get(job1)["status"] == "done"
    r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o3"), "mode": "fast"})
    assert r.status_code == 200


@pytest.mark.parametrize("holder", ["compare", "deep_dv"])
def test_d4_heavy_jobs_cross_block_but_allow_ordinary_score(client, fake, videos, tmp_path, holder):
    blocker = BlockingClient("compare.json" if holder == "compare" else "score.json")
    job_id = jobs_module.jobs.create("suite_compare" if holder == "compare" else "suite_score")
    if holder == "compare":
        thread = start_in_thread(suite_routes.run_compare_job, job_id, blocker, videos, str(tmp_path / "o"))
    else:
        thread = start_in_thread(suite_routes.run_score_job, job_id, blocker, videos, deep_dv=True)
    try:
        assert blocker.started.wait(5)
        detail = ("a comparison is already running" if holder == "compare" else
                  "a deep Dolby Vision scan is already running") + (
                  "; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time")
        if holder == "deep_dv":
            response = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "out"), "mode": "fast"})
            assert response.status_code == 409
            assert response.json() == {"detail": detail}
        response = client.post("/suite/score", json={"paths": videos, "deep_dv": True})
        assert response.status_code == 409
        assert response.json() == {"detail": detail}
        other = FakeClient()
        refused = jobs_module.jobs.create("suite_score")
        suite_routes.run_score_job(refused, other, videos, deep_dv=True)
        job = jobs_module.jobs.get(refused)
        assert (job["error_kind"], job["error"]) == ("busy", detail)
        assert other.calls == []
        assert client.post("/suite/score", json={"paths": videos}).status_code == 200
    finally:
        blocker.release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert jobs_module.jobs.get(job_id)["status"] == "done"
    assert client.post("/suite/score", json={"paths": videos, "deep_dv": True}).status_code == 200
    assert client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "out"), "mode": "fast"}).status_code == 200


def test_d4_deep_score_releases_both_slots_after_exception(client, fake, videos, tmp_path):
    fake.results["score"] = RuntimeError("fake failure")
    response = client.post("/suite/score", json={"paths": videos, "deep_dv": True})
    assert response.status_code == 200
    assert jobs_module.jobs.get(response.json()["job_id"])["error_kind"] == "internal"
    assert suite_routes._slots.try_acquire("compare") is None
    assert suite_routes._slots.try_acquire("score_rank") is None
    assert suite_routes._slots.try_acquire("score_rank") is None


def test_d4_score_cap_refusal_does_not_take_heavy_slot():
    slots = suite_routes._Slots()
    assert slots.acquire("score_rank") and slots.acquire("score_rank")
    assert slots.try_acquire("score_rank+deep_dv") == "too many suite jobs are running"
    assert slots.try_acquire("compare") is None


def test_d4_heavy_refusal_does_not_take_score_slot():
    slots = suite_routes._Slots()
    assert slots.acquire("compare")
    assert slots.try_acquire("score_rank+deep_dv") == (
        "a comparison is already running; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time")
    assert slots.acquire("score_rank") and slots.acquire("score_rank")


def test_d4_heavy_check_and_take_is_atomic():
    slots = suite_routes._Slots()
    barrier = threading.Barrier(3)
    results = []
    def take(slot):
        barrier.wait(timeout=5)
        results.append((slot, slots.try_acquire(slot)))
    threads = [start_in_thread(take, slot) for slot in ("compare", "score_rank+deep_dv")]
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert len(results) == 2
    winners = [slot for slot, detail in results if detail is None]
    assert len(winners) == 1
    slots.release(winners[0])
    assert slots.try_acquire("score_rank+deep_dv") is None
    assert slots.try_acquire("score_rank") is None
    assert slots.try_acquire("score_rank") == "too many suite jobs are running"


def test_the_compare_slot_is_released_after_an_error_too(client, fake, videos, tmp_path):
    fake.results["compare"] = RuntimeError("x")
    for _ in range(2):
        r = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"})
        assert r.status_code == 200


def test_runner_called_while_a_compare_runs_ends_its_job_busy(videos, tmp_path):
    blocker = BlockingClient()
    job1 = jobs_module.jobs.create("suite_compare")
    t = start_in_thread(suite_routes.run_compare_job, job1, blocker, videos, str(tmp_path / "o1"), "fast", 4)
    assert blocker.started.wait(10)
    try:
        job2 = jobs_module.jobs.create("suite_compare")
        other = BlockingClient()
        suite_routes.run_compare_job(job2, other, videos, str(tmp_path / "o2"), "fast", 4)
        j2 = jobs_module.jobs.get(job2)
        assert (j2["status"], j2["error_kind"]) == ("error", "busy")
        assert other.calls == 0
    finally:
        blocker.release.set()
        t.join(10)
    assert jobs_module.jobs.get(job1)["status"] == "done"


def test_only_two_score_or_rank_jobs_run_at_once(client, fake, videos, tmp_path):
    blockers = [BlockingClient("score.json"), BlockingClient("rank.json")]
    j1, j2 = jobs_module.jobs.create("suite_score"), jobs_module.jobs.create("suite_rank")
    t1 = start_in_thread(suite_routes.run_score_job, j1, blockers[0], videos)
    reports = tmp_path / "rep"
    reports.mkdir()
    t2 = start_in_thread(suite_routes.run_rank_job, j2, blockers[1], videos, str(reports))
    assert blockers[0].started.wait(10) and blockers[1].started.wait(10)
    try:
        for url, body in [("/suite/score", {"paths": videos}), ("/suite/rank", {"paths": videos, "reports_dir": str(reports)})]:
            r = client.post(url, json=body)
            assert r.status_code == 409, url
            assert r.json() == {"detail": "too many suite jobs are running"}
        # a compare is still allowed next to them (its own slot)
        assert client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"}).status_code == 200
    finally:
        for b in blockers:
            b.release.set()
        t1.join(10)
        t2.join(10)
    assert client.post("/suite/score", json={"paths": videos}).status_code == 200


# fake process for the cancel test (same shape as the D10 tests use)

class HangingProc:
    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"2 file(s) shortlisted\n")
        self.pid = 4242
        self.returncode = None
        self.killed = False

    def die(self, rc=1):
        self.returncode = rc

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("fake", timeout)

    def kill(self):
        self.die(-9)


def test_cancelling_a_running_compare_kills_the_process_and_ends_the_job_cancelled(videos, tmp_path):
    proc = HangingProc()
    started = threading.Event()
    killed: list[int] = []

    def popen(argv, **kwargs):
        started.set()
        return proc

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    real = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=popen, killer=killer)
    job_id = jobs_module.jobs.create("suite_compare")
    t = start_in_thread(suite_routes.run_compare_job, job_id, real, videos, str(tmp_path / "o"), "fast", 4)
    assert started.wait(10)
    assert jobs_module.jobs.request_cancel(job_id)
    t.join(15)
    assert not t.is_alive()
    assert killed == [4242]
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "cancelled"
    assert job["error"] == "cancelled"
    # the slot is free again
    other = jobs_module.jobs.create("suite_compare")
    suite_routes.run_compare_job(other, FakeClient(), videos, str(tmp_path / "o"), "fast", 4)
    assert jobs_module.jobs.get(other)["status"] == "done"


def _cancel_via_real_client(kind, videos, tmp_path):
    """Run a score/rank job with the real client and a hanging fake process; cancel it; return (job, killed, slots_ok)."""
    proc = HangingProc()
    started = threading.Event()
    killed: list[int] = []
    lists: list[Path] = []

    def popen(argv, **kwargs):
        lists.append(Path(argv[argv.index("--list") + 1]))
        assert lists[0].exists()
        started.set()
        return proc

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    real = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=popen, killer=killer)
    job_id = jobs_module.jobs.create(f"suite_{kind}")
    if kind == "score":
        t = start_in_thread(suite_routes.run_score_job, job_id, real, videos)
    else:
        reports = tmp_path / "rep"
        reports.mkdir()
        t = start_in_thread(suite_routes.run_rank_job, job_id, real, videos, str(reports))
    assert started.wait(10)
    assert jobs_module.jobs.request_cancel(job_id)
    t.join(15)
    assert not t.is_alive()
    assert killed == [4242]
    assert not lists[0].parent.exists()                         # the list dir was removed
    return jobs_module.jobs.get(job_id)


@pytest.mark.parametrize("kind", ["score", "rank"])
def test_cancelling_a_running_score_or_rank_kills_the_process_and_ends_cancelled(kind, videos, tmp_path):
    job = _cancel_via_real_client(kind, videos, tmp_path)
    assert (job["status"], job["error"]) == ("cancelled", "cancelled")
    assert job["results"] == []
    # both score/rank slots are free again: two more jobs can hold them at the same time
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


@pytest.mark.parametrize("kind", ["score", "rank"])
def test_a_score_or_rank_that_returns_none_ends_cancelled(kind, videos, tmp_path):
    job_id = jobs_module.jobs.create(f"suite_{kind}")
    client = FakeClient(**{kind: lambda *a, **k: None})
    if kind == "score":
        suite_routes.run_score_job(job_id, client, videos)
    else:
        suite_routes.run_rank_job(job_id, client, videos, str(tmp_path))
    assert jobs_module.jobs.get(job_id)["status"] == "cancelled"


@pytest.mark.parametrize("kind", ["score", "rank"])
def test_a_job_cancelled_while_queued_never_calls_the_client(kind, videos, tmp_path):
    job_id = jobs_module.jobs.create(f"suite_{kind}")
    assert jobs_module.jobs.request_cancel(job_id)
    client = FakeClient()
    if kind == "score":
        suite_routes.run_score_job(job_id, client, videos)
    else:
        suite_routes.run_rank_job(job_id, client, videos, str(tmp_path))
    assert client.calls == []
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error"]) == ("cancelled", "cancelled")
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


def test_a_compare_cancelled_while_queued_never_calls_the_client(videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_compare")
    assert jobs_module.jobs.request_cancel(job_id)
    client = FakeClient()
    suite_routes.run_compare_job(job_id, client, videos, str(tmp_path / "o"), "fast", 4)
    assert client.calls == []
    assert jobs_module.jobs.get(job_id)["status"] == "cancelled"


def test_score_and_rank_receive_a_cancel_callable_reading_the_store_flag(videos, tmp_path):
    for kind in ("score", "rank"):
        job_id = jobs_module.jobs.create(f"suite_{kind}")
        seen = []

        def go(*a, cancel=None, **k):
            seen.append(cancel())
            jobs_module.jobs.request_cancel(job_id)
            seen.append(cancel())
            return None

        client = FakeClient(**{kind: go})
        if kind == "score":
            suite_routes.run_score_job(job_id, client, videos)
        else:
            suite_routes.run_rank_job(job_id, client, videos, str(tmp_path))
        assert seen == [False, True]


def test_a_compare_that_returns_none_ends_cancelled_not_error_or_done(videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_compare")
    client = FakeClient(compare=lambda *a, **k: None)
    suite_routes.run_compare_job(job_id, client, videos, str(tmp_path / "o"), "fast", 4)
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "cancelled"
    assert job["results"] == []


def test_the_cancel_callable_reads_the_job_store_flag(videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_compare")
    seen = []

    def compare(paths, out_dir, mode, top, *, on_line, cancel, **kw):
        seen.append(cancel())
        jobs_module.jobs.request_cancel(job_id)
        seen.append(cancel())
        return None

    suite_routes.run_compare_job(job_id, FakeClient(compare=compare), videos, str(tmp_path / "o"), "fast", 4)
    assert seen == [False, True]


def test_progress_events_are_bounded_and_truncated(videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_compare")

    def compare(paths, out_dir, mode, top, *, on_line, cancel, **kw):
        on_line("x" * 5000)
        for i in range(suite_routes.MAX_PROGRESS_EVENTS + 50):
            on_line(f"line {i}")
        return fixture_doc("compare.json")

    suite_routes.run_compare_job(job_id, FakeClient(compare=compare), videos, str(tmp_path / "o"), "fast", 4)
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "done"
    assert len(job["events"]) <= suite_routes.MAX_PROGRESS_EVENTS + 5
    assert max(len(e["msg"]) for e in job["events"]) <= 600
    assert job["current"].startswith("line ")                    # the latest line is still visible


# Plan 3: owner settings are facts reported by the suite, never a direct file read.
def test_settings_route_returns_owner_facts(client, fake):
    response = client.get("/suite/settings")
    assert response.status_code == 200
    body = response.json()
    assert {k: v for k, v in body.items() if k != "devices"} == {
        "tool_version": "0.4.0", "supported": True, "path": "C:\\fixtures\\suite\\no_settings.json",
        "loaded": False, "errors": [], "applied_count": 0,
    }
    assert set(body["devices"]) == {"sony_bravia_8_ii", "galaxy_tab_s9_ultra", "vivobook_pro_16x"}
    for d in fixture_doc("devices.json")["devices"]:
        assert body["devices"][d["id"]] == {"label": d["label"], "usb_filesystem": None, "audio_output": None, "overrides": []}


def test_settings_loaded_overrides_and_broken_file(client):
    doc = fixture_doc("devices.json")
    doc["settings"] = {"path": "D:\\Owner\\settings.json", "loaded": True, "errors": ["unknown key x"], "applied_count": 1}
    sony = next(d for d in doc["devices"] if d["id"] == "sony_bravia_8_ii")
    sony["settings"] = {"usb_filesystem": "exfat", "audio_output": "tv_speakers"}
    sony["players"][1]["overrides"] = [{"rule": "audio_codecs.truehd", "source": "owner_test", "reason": "tested on my TV", "confidence": "owner"}]
    f = FakeClient(devices=doc)
    main.app.dependency_overrides[get_suite_client] = lambda: f
    body = client.get("/suite/settings").json()
    assert body["loaded"] is True and body["applied_count"] == 1 and body["errors"] == ["unknown key x"]
    assert body["devices"]["sony_bravia_8_ii"] == {"label": sony["label"], "usb_filesystem": "exfat", "audio_output": "tv_speakers", "overrides": [{"player": "kodi", "rule": "audio_codecs.truehd", "source": "owner_test", "reason": "tested on my TV", "confidence": "owner"}]}
    suite_routes.reset_state()
    doc["settings"].update(loaded=False, errors=["settings.json: devices must be an object"], applied_count=0)
    response = client.get("/suite/settings")
    assert response.status_code == 200 and response.json()["errors"] == ["settings.json: devices must be an object"]


def test_settings_old_suite_and_wrong_scalars(client):
    doc = fixture_doc("devices.json"); doc.pop("settings")
    for d in doc["devices"]:
        d.pop("settings")
    f = FakeClient(devices=doc)
    main.app.dependency_overrides[get_suite_client] = lambda: f
    body = client.get("/suite/settings").json()
    assert body["supported"] is False and body["path"] is None
    assert all(d["usb_filesystem"] is None for d in body["devices"].values())
    weird = {"tool_version": 4, "settings": {"path": 7, "loaded": "yes", "errors": ["a", 3], "applied_count": True}, "devices": [{"id": "x", "label": None, "settings": "no", "players": [{"id": "p", "overrides": ["bad"]}]}, {"label": "no id"}, "junk"]}
    suite_routes.reset_state(); f.results["devices"] = weird
    assert client.get("/suite/settings").json() == {"tool_version": None, "supported": True, "path": None, "loaded": False, "errors": ["a"], "applied_count": 0, "devices": {"x": {"label": "x", "usb_filesystem": None, "audio_output": None, "overrides": []}}}


@pytest.mark.parametrize("bad", [7, "bad", {}, None])
@pytest.mark.parametrize("field", ["errors", "players", "overrides", "devices"])
def test_settings_wrong_container_types_never_mutate_cache(client, bad, field):
    doc = fixture_doc("devices.json")
    if field == "devices":
        assert suite_routes.settings_view({"tool_version": "0.4.0", "devices": bad})["devices"] == {}
        return
    if field == "errors":
        doc["settings"]["errors"] = bad
    elif field == "players":
        doc["devices"][1]["players"] = bad
    else:
        doc["devices"][1]["players"][0]["overrides"] = bad
    before = copy.deepcopy(doc)
    main.app.dependency_overrides[get_suite_client] = lambda: FakeClient(devices=doc)
    response = client.get("/suite/settings")
    assert response.status_code == 200
    body = response.json()
    assert (body["errors"] if field == "errors" else body["devices"]["sony_bravia_8_ii"]["overrides"]) == []
    assert suite_routes._devices_cache[1] == before


def test_settings_shares_devices_cache(client, fake, monkeypatch):
    now = [1000.0]; monkeypatch.setattr(suite_routes, "_clock", lambda: now[0])
    assert client.get("/suite/devices").status_code == 200
    now[0] += 59
    assert client.get("/suite/settings").status_code == 200
    assert [c[0] for c in fake.calls] == ["devices"]
    now[0] += 2; assert client.get("/suite/settings").status_code == 200
    assert [c[0] for c in fake.calls] == ["devices", "devices"]


def test_simultaneous_devices_and_settings_share_one_fill(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import suite_version
    entered, release, second_started, second_miss = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    original_fresh = suite_routes._fresh_devices
    misses = []
    miss_lock = threading.Lock()
    def fresh():
        value = original_fresh()
        if value is None:
            with miss_lock:
                misses.append(True)
                if len(misses) >= 3:
                    second_miss.set()
        return value
    monkeypatch.setattr(suite_routes, "_fresh_devices", fresh)
    def devices():
        entered.set()
        assert release.wait(5)
        return fixture_doc("devices.json")
    f = VersionFakeClient(devices=devices)
    suite_version.ensure(f)
    def second():
        second_started.set()
        return suite_routes.settings_view(suite_routes._devices_document(f))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(suite_routes._devices_document, f)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
            assert second_miss.wait(5)  # both requests observed a cold cache before releasing the CLI read
        finally:
            release.set()
        assert first.result(timeout=5) == fixture_doc("devices.json")
        assert other.result(timeout=5)["tool_version"] == "0.4.0"
    assert [c[0] for c in f.calls].count("devices") == 1


def test_settings_error_is_not_cached_and_recovers(client):
    f = FakeClient(devices=SuiteError("timeout", "x"))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    assert client.get("/suite/settings").status_code == 504
    f.results["devices"] = fixture_doc("devices.json")
    assert client.get("/suite/settings").status_code == 200
    assert len(f.calls) == 2


@pytest.mark.parametrize("kind,status,detail", [("unavailable", 503, UNAVAILABLE), ("timeout", 504, "VideoSuite timed out."), ("failed", 502, "VideoSuite failed."), ("contract", 502, "VideoSuite returned an unusable response.")])
def test_settings_errors_do_not_expose_stderr(client, kind, status, detail):
    f = FakeClient(devices=SuiteError(kind, "secret", stderr_tail="C:\\secret\\x"))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    response = client.get("/suite/settings")
    assert response.status_code == status and response.json() == {"detail": detail}
    assert "secret" not in response.text


def test_settings_factory_unavailable_and_old_version(client):
    def unavailable():
        raise SuiteUnavailable("secret")
    main.app.dependency_overrides[get_suite_client] = unavailable
    response = client.get("/suite/settings")
    assert response.status_code == 503 and response.json() == {"detail": UNAVAILABLE}
    f = VersionFakeClient(version=dict(fixture_doc("version.json"), tool_version="0.2.9"))
    main.app.dependency_overrides[get_suite_client] = lambda: f
    response = client.get("/suite/settings")
    assert response.status_code == 502
    assert response.json() == {"detail": "The installed VideoSuite is 0.2.9; the Dashboard needs 0.3.0 or later. Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard."}


def test_settings_request_guards(client, fake):
    for headers, status, detail in [({"Origin": "http://evil.example"}, 403, "origin not allowed"), ({"Host": "evil.example"}, 400, "host not allowed"), ({"Sec-Fetch-Site": "cross-site"}, 403, "cross-site request refused")]:
        response = client.get("/suite/settings", headers=headers)
        assert response.status_code == status and response.json() == {"detail": detail}
    response = client.get("/suite/settings", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200 and response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert client.post("/suite/settings", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/suite/settings", json={}).status_code == 405
