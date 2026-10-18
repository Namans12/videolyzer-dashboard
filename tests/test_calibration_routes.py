"""Tests for the /suite/calibration routes (Plan 4A, task D2).

No real subprocess: routes get a fake client through ``app.dependency_overrides[get_suite_client]``; the cancel
tests drive the real ``SuiteClient`` with a fake process (injected ``popen``/``killer``). TestClient runs
BackgroundTasks before the response returns, so HTTP tests assert final job states only; "running" behaviour is
tested by calling the runners in a worker thread."""
from __future__ import annotations

import copy
import importlib
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import jobs as jobs_module
import main
import suite_routes
from suite_client import ERROR_KINDS, SuiteArgumentError, SuiteClient, SuiteError, SuiteUnavailable, get_suite_client

UNAVAILABLE = "VideoSuite is not installed (set VIDEOSUITE_PYTHON and VIDEOSUITE_ROOT)"
SECRET = "SECRET-VALUE-7f3a"
ROUTES = ["plan", "status", "session", "record", "build", "apply", "undo", "report"]
KIT = "0123456789ab-" + "f" * 64


def routes_module():
    return importlib.import_module("calibration_routes")


# ── documents the fake client returns ────────────────────────────────────────────

def _head(sub: str) -> dict:
    return {"schema": f"videosuite-calibrate-{sub}/1", "tool_version": "0.5.0"}


def plan_doc() -> dict:
    return {**_head("plan"), "catalogue_version": 1, "players": ["builtin_usb"], "tests": []}


def build_doc() -> dict:
    return {**_head("build"), "kit_id": KIT, "out": "D:\\kit", "descriptor_path": "D:\\kit\\kit.json",
            "total_bytes": 10, "descriptors": [], "unavailable": []}


SUMMARY = {"done": 0, "total": 0, "matches": 0, "differs": 0}


def status_doc() -> dict:
    return {**_head("status"), "recovery_required": False, "kit": None, "session": None, "answers": [],
            "summary": dict(SUMMARY)}


def session_doc() -> dict:
    return {**_head("session"), "kit_id": KIT, "session": {"tv_software": "1.0", "audio_output": None,
                                                           "usb_filesystem": None, "confirmed_at": "t"}}


def record_doc() -> dict:
    return {**_head("record"), "summary": dict(SUMMARY),
            "answer": {"kit_id": KIT, "test": "T01", "player": "builtin_usb", "outcome": "ok", "note": "",
                       "conditions": {"audio_output": None, "usb_filesystem": None, "tv_software": "1.0"},
                       "at": "t", "older_tv_software": False, "match": "matches"}}


def apply_doc() -> dict:
    return {**_head("apply"), "revision": "r1", "applied": True, "recovery_required": False,
            "changes": [], "conflicts": [], "skipped": []}


def report_doc() -> dict:
    return {**_head("report"), "note": "playback only, not a quality ranking", "kit_active": True,
            "recovery_required": False, "files": [], "best_player_changes": []}


DOCS = {"calibrate_plan": plan_doc, "calibrate_build": build_doc, "calibrate_status": status_doc,
        "calibrate_session": session_doc, "calibrate_record": record_doc, "calibrate_apply": apply_doc,
        "calibrate_undo": apply_doc, "calibrate_report": report_doc, "devices": None}


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
        if name in self.results:
            return copy.deepcopy(value)
        if name == "devices":
            return {"schema": "videosuite-devices/1", "tool_version": "0.5.0", "devices": []}
        return DOCS[name]()

    def devices(self):
        return self._go("devices", (), {})

    def version(self):
        return {"schema": "videosuite-version/1", "tool_version": "0.5.0", "capabilities": ["calibration"],
                "schemas": {}, "facts_version": 2}

    def calibrate_plan(self, *a, **k):
        return self._go("calibrate_plan", a, k)

    def calibrate_build(self, *a, **k):
        return self._go("calibrate_build", a, k)

    def calibrate_status(self, *a, **k):
        return self._go("calibrate_status", a, k)

    def calibrate_session(self, *a, **k):
        return self._go("calibrate_session", a, k)

    def calibrate_record(self, *a, **k):
        return self._go("calibrate_record", a, k)

    def calibrate_apply(self, *a, **k):
        return self._go("calibrate_apply", a, k)

    def calibrate_undo(self, *a, **k):
        return self._go("calibrate_undo", a, k)

    def calibrate_report(self, *a, **k):
        return self._go("calibrate_report", a, k)

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def fake(client):
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


def no_jobs() -> bool:
    return len(jobs_module.jobs) == 0


def only_job() -> dict:
    ids = jobs_module.jobs.ids()
    assert len(ids) == 1, ids
    return jobs_module.jobs.get(ids[0])


def job_of(client, response) -> dict:
    assert response.status_code == 200, response.text
    return client.get(f"/job/{response.json()['job_id']}").json()


def start_in_thread(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def bodies(videos, tmp_path) -> dict:
    """One valid request per route: (method, url, body-or-None)."""
    out = tmp_path / "kit"
    out.mkdir(exist_ok=True)
    return {
        "plan": ("post", "/suite/calibration/plan", {"paths": videos, "players": ["builtin_usb"]}),
        "status": ("get", f"/suite/calibration/status?kit={KIT}", None),
        "session": ("post", "/suite/calibration/session", {"out": str(out), "tv_software": "1.0",
                                                           "audio_output": "soundbar", "usb_filesystem": "exfat"}),
        "record": ("post", "/suite/calibration/record", {"kit": KIT, "test": "T01", "player": "builtin_usb",
                                                         "outcome": "ok", "note": "fine"}),
        "build": ("post", "/suite/calibration/build", {"paths": videos, "out": str(tmp_path / "newkit"),
                                                       "players": ["builtin_usb"], "tests": ["T01"]}),
        "apply": ("post", "/suite/calibration/apply", {"kit": KIT, "preview": True}),
        "undo": ("post", "/suite/calibration/undo", {"kit": KIT}),
        "report": ("post", "/suite/calibration/report", {"kit": KIT, "paths": videos}),
    }


def call(client, spec, **overrides):
    method, url, body = spec
    if method == "get":
        return client.get(url)
    return client.post(url, json={**body, **overrides})


# ── registration ─────────────────────────────────────────────────────────────────

def test_the_eight_calibration_routes_exist_with_their_methods():
    paths = {}
    for r in main.app.routes:
        inner = getattr(r, "original_router", None)
        subs = [(r.include_context.prefix + s.path, s) for s in inner.routes] if inner is not None else [(getattr(r, "path", None), r)]
        for path, sub in subs:
            if path and path.startswith("/suite/calibration/"):
                paths[path] = set(sub.methods)
    assert paths == {f"/suite/calibration/{n}": {"GET"} if n == "status" else {"POST"} for n in ROUTES}


# ── plan: sync vs job ────────────────────────────────────────────────────────────

def test_plan_without_deep_dv_is_synchronous_and_creates_no_job(client, fake, videos):
    r = client.post("/suite/calibration/plan", json={"paths": videos, "players": ["builtin_usb", "kodi"]})
    assert r.status_code == 200
    assert r.json() == plan_doc()
    assert no_jobs()
    name, args, kwargs = fake.calls[0]
    assert name == "calibrate_plan"
    assert list(args[0]) == [str(Path(p).resolve()) for p in videos]
    assert list(args[1]) == ["builtin_usb", "kodi"]
    assert kwargs.get("deep_dv") is False
    assert "cancel" not in kwargs or kwargs["cancel"] is None
    # a synchronous plan holds no slot
    assert suite_routes._slots.try_acquire("calibration") is None


def test_plan_players_and_deep_dv_are_optional(client, fake, videos):
    assert client.post("/suite/calibration/plan", json={"paths": videos}).status_code == 200
    assert list(fake.calls[0][1][1]) == []
    assert client.post("/suite/calibration/plan", json={"paths": videos, "deep_dv": False}).status_code == 200
    assert no_jobs()
    assert [c[0] for c in fake.calls] == ["calibrate_plan", "calibrate_plan"]


def test_plan_with_deep_dv_is_a_job_that_ends_done_with_the_document(client, fake, videos):
    r = client.post("/suite/calibration/plan", json={"paths": videos, "players": ["builtin_usb"], "deep_dv": True})
    job = job_of(client, r)
    assert (job["status"], job["kind"]) == ("done", "suite_calibration_plan")
    assert job["results"] == plan_doc()
    assert job["error"] is None and job["error_kind"] is None
    name, args, kwargs = fake.calls[0]
    assert name == "calibrate_plan"
    assert list(args[0]) == [str(Path(p).resolve()) for p in videos]
    assert kwargs["deep_dv"] is True
    assert callable(kwargs["cancel"])
    assert suite_routes._slots.try_acquire("calibration") is None           # the slot was released


def test_plan_accepts_offline_sources_and_an_empty_list(client, fake, tmp_path):
    ghost = tmp_path / "offline_drive_copy.mkv"                               # does not exist; its folder does
    r = client.post("/suite/calibration/plan", json={"paths": [str(ghost)]})
    assert r.status_code == 200
    assert list(fake.calls[0][1][0]) == [str(ghost.resolve())]
    assert client.post("/suite/calibration/plan", json={"paths": []}).status_code == 200
    assert list(fake.calls[1][1][0]) == []


def test_plan_deep_dv_job_with_offline_source_is_accepted(client, fake, tmp_path):
    r = client.post("/suite/calibration/plan", json={"paths": [str(tmp_path / "gone.mkv")], "deep_dv": True})
    assert job_of(client, r)["status"] == "done"


# ── plan/build/report: path guards ───────────────────────────────────────────────

PATH_ROUTES = ["plan", "build", "report"]


@pytest.mark.parametrize("route", PATH_ROUTES)
@pytest.mark.parametrize("raw,status", [("\\\\server\\share\\a.mkv", 400), ("relative\\a.mkv", 400),
                                        ("C:a.mkv", 400), ("\\\\?\\UNC\\server\\share\\a.mkv", 400),
                                        ("\\\\.\\NUL", 400),
                                        ("C:\\a.mkv::$DATA", 400), ("NUL", 400), ("C:\\a\x00b.mkv", 400)])
def test_bad_source_paths_are_rejected_without_a_job_or_a_call(client, fake, videos, tmp_path, route, raw, status):
    spec = bodies(videos, tmp_path)[route]
    r = call(client, spec, paths=[raw])
    assert r.status_code == status, r.text
    assert "server" not in r.text
    assert no_jobs() and fake.calls == []


@pytest.mark.parametrize("route", PATH_ROUTES)
@pytest.mark.parametrize("name", ["a\nb.mkv", "a\rb.mkv", "a\tb.mkv", "a\x1fb.mkv", "a\u2028b.mkv"])
def test_control_characters_in_a_source_path_are_400(client, fake, videos, tmp_path, route, name):
    spec = bodies(videos, tmp_path)[route]
    r = call(client, spec, paths=[str(tmp_path / name)])
    assert r.status_code == 400, r.text
    assert no_jobs() and fake.calls == []


@pytest.mark.parametrize("route", PATH_ROUTES)
@pytest.mark.parametrize("suffix", ["\n", "\r", "\t", "\u2028"])
def test_raw_source_control_characters_are_rejected_before_normalization(client, fake, videos, tmp_path, route, suffix):
    r = call(client, bodies(videos, tmp_path)[route], paths=[videos[0] + suffix])
    assert r.status_code == 400
    assert fake.calls == [] and no_jobs()


@pytest.mark.parametrize("route", PATH_ROUTES)
def test_offline_sources_preserve_spaces_metacharacters_and_unicode(client, fake, videos, tmp_path, route):
    source = str(tmp_path / "фильм with spaces & 50%.mkv")
    r = call(client, bodies(videos, tmp_path)[route], paths=[source])
    assert r.status_code == 200
    args = fake.calls[0][1]
    assert list(args[1] if route == "report" else args[0]) == [source]


@pytest.mark.parametrize("route", PATH_ROUTES)
def test_unsupported_extension_and_missing_parent_and_roots(client, fake, videos, tmp_path, route, monkeypatch):
    spec = bodies(videos, tmp_path)[route]
    assert call(client, spec, paths=[str(tmp_path / "notes.txt")]).status_code == 400
    assert call(client, spec, paths=[str(tmp_path / "nope" / "a.mkv")]).status_code == 404
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(other))
    assert call(client, spec, paths=videos).status_code == 403
    assert no_jobs() and fake.calls == []


@pytest.mark.parametrize("route", PATH_ROUTES)
def test_one_bad_path_rejects_the_whole_request(client, fake, videos, tmp_path, route):
    spec = bodies(videos, tmp_path)[route]
    assert call(client, spec, paths=videos + ["\\\\h\\s\\c.mkv"]).status_code == 400
    assert no_jobs() and fake.calls == []


@pytest.mark.parametrize("route", PATH_ROUTES)
def test_51_paths_is_422(client, fake, videos, tmp_path, route):
    spec = bodies(videos, tmp_path)[route]
    assert call(client, spec, paths=[videos[0]] * 51).status_code == 422
    assert call(client, spec, paths=[videos[0]] * 50).status_code == 200
    jobs_module.jobs.clear()


def test_report_needs_at_least_one_path(client, fake, videos, tmp_path):
    spec = bodies(videos, tmp_path)["report"]
    assert call(client, spec, paths=[]).status_code == 422
    assert no_jobs()


def test_build_out_is_a_folder_that_need_not_exist_but_follows_the_folder_policy(client, fake, videos, tmp_path):
    spec = bodies(videos, tmp_path)["build"]
    for bad, status in [(str(tmp_path / "x&y"), 400), (str(tmp_path / "x%y"), 400),
                        (str(tmp_path / "missing" / "kit"), 404), ("\\\\h\\s\\kit", 400)]:
        assert call(client, spec, out=bad).status_code == status, bad
    f = tmp_path / "afile"
    f.write_text("x")
    assert call(client, spec, out=str(f)).status_code == 400
    assert no_jobs() and fake.calls == []
    r = call(client, spec, out=str(tmp_path / "newkit"))
    assert job_of(client, r)["status"] == "done"
    assert fake.calls[0][1][1] == str((tmp_path / "newkit").resolve())


# ── output folders behind a link or junction (checked on the RAW path, before canonicalization) ──

def _make_link(link: Path, target: Path, kind: str) -> None:
    """A junction (Windows) or symlink at ``link`` -> ``target``; skips the test when it cannot be created."""
    try:
        if kind == "junction":
            if sys.platform != "win32":
                pytest.skip("junctions exist on Windows only")
            done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=30)
            if done.returncode != 0 or not link.exists():
                pytest.skip("cannot create a junction here (mklink /J failed)")
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, subprocess.SubprocessError):
        pytest.skip(f"cannot create a {kind} here (no privilege)")


LINK_KINDS = ["junction", "symlink"]
LINK_REFUSAL = {"build": "out cannot be, or lie inside, a link or junction"}


@pytest.fixture
def linked(tmp_path):
    """tmp_path/target (real folder with an existing 'sub'), made reachable through tmp_path/alias."""
    target = tmp_path / "target"
    (target / "sub").mkdir(parents=True)
    return tmp_path / "alias", target


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("tail", ["kit", "sub\\kit", ""])
def test_build_refuses_an_output_folder_that_is_or_sits_behind_a_link(client, fake, videos, tmp_path, linked, kind, tail):
    alias, target = linked
    _make_link(alias, target, kind)
    spec = bodies(videos, tmp_path)["build"]
    out = str(alias / tail) if tail else str(alias)
    for raw in (out, f'"{out}"', f"  {out}  ", out + "\\"):
        r = call(client, spec, out=raw)
        assert r.status_code == 400, (raw, r.text)
        assert r.json() == {"detail": "out cannot be, or lie inside, a link or junction"}
        assert str(tmp_path) not in r.text
    assert no_jobs() and fake.calls == []                                     # no job, and the CLI never sees it


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_build_refuses_a_link_deeper_in_the_ancestors_and_a_planted_leaf_link(client, fake, videos, tmp_path, linked, kind):
    alias, target = linked
    _make_link(alias, target, kind)
    spec = bodies(videos, tmp_path)["build"]
    (tmp_path / "plain").mkdir()
    _make_link(tmp_path / "plain" / "leaf", target, kind)                    # the leaf itself is the link
    assert call(client, spec, out=str(tmp_path / "plain" / "leaf")).status_code == 400
    assert call(client, spec, out=str(alias / "sub" / "deeper" / "kit")).status_code in (400, 404)
    assert no_jobs() and fake.calls == []


def test_build_accepts_a_normal_folder_next_to_a_link(client, fake, videos, tmp_path, linked):
    alias, target = linked
    _make_link(alias, target, "junction" if sys.platform == "win32" else "symlink")
    spec = bodies(videos, tmp_path)["build"]
    for name in ("kit", "kit2"):
        r = call(client, spec, out=str(tmp_path / name))
        assert job_of(client, r)["status"] == "done"
        jobs_module.jobs.clear()
    (tmp_path / "real").mkdir()
    assert job_of(client, call(client, spec, out=str(tmp_path / "real" / "kit")))["status"] == "done"
    assert fake.calls[0][1][1] == str((tmp_path / "kit").resolve())


@pytest.mark.parametrize("kind", LINK_KINDS)
@pytest.mark.parametrize("tail", ["", "sub"])
def test_session_refuses_a_kit_folder_that_is_or_sits_behind_a_link(client, fake, tmp_path, linked, kind, tail):
    alias, target = linked
    _make_link(alias, target, kind)
    out = str(alias / tail) if tail else str(alias)
    r = client.post("/suite/calibration/session", json={"out": out, "tv_software": "1"})
    assert r.status_code == 400
    assert r.json() == {"detail": "out cannot be, or lie inside, a link or junction"}
    assert str(tmp_path) not in r.text
    assert fake.calls == []
    plain = tmp_path / "kitfolder"
    plain.mkdir()
    assert client.post("/suite/calibration/session", json={"out": str(plain), "tv_software": "1"}).status_code == 200


def test_link_check_leaves_the_other_refusals_alone(client, fake, videos, tmp_path):
    spec = bodies(videos, tmp_path)["build"]
    for bad, status in [("\\\\h\\s\\kit", 400), ("relative", 400), (str(tmp_path / "missing" / "kit"), 404),
                        (str(tmp_path / "x&y"), 400), ("C:\\a\x00b", 400)]:
        assert call(client, spec, out=bad).status_code == status, bad
    assert no_jobs() and fake.calls == []


# ── build ────────────────────────────────────────────────────────────────────────

def test_build_is_a_job_with_progress_lines(client, fake, videos, tmp_path):
    def build(paths, out, players, tests, *, deep_dv=False, on_line=None, cancel=None):
        on_line("Estimated total: 10 bytes")
        on_line("T01 built")
        return build_doc()
    fake.results["calibrate_build"] = build
    r = client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "kit"),
                                                      "players": ["builtin_usb", "kodi"], "tests": ["T01", "T02"],
                                                      "deep_dv": True})
    job = job_of(client, r)
    assert (job["status"], job["kind"]) == ("done", "suite_calibration_build")
    assert job["results"] == build_doc()
    msgs = [e["msg"] for e in job["events"]]
    assert "Estimated total: 10 bytes" in msgs and "T01 built" in msgs
    _, args, kwargs = fake.calls[0]
    assert list(args[0]) == [str(Path(p).resolve()) for p in videos]
    assert args[1] == str((tmp_path / "kit").resolve())
    assert list(args[2]) == ["builtin_usb", "kodi"] and list(args[3]) == ["T01", "T02"]
    assert kwargs["deep_dv"] is True


def test_build_defaults_mean_every_test_and_no_deep_dv(client, fake, videos, tmp_path):
    r = client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "kit")})
    assert job_of(client, r)["status"] == "done"
    _, args, kwargs = fake.calls[0]
    assert list(args[2]) == [] and list(args[3]) == []
    assert kwargs["deep_dv"] is False


def test_build_progress_events_are_bounded_and_truncated(videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_calibration_build")

    def build(paths, out, players, tests, *, on_line, **kw):
        on_line("x" * 5000)
        for i in range(suite_routes.MAX_PROGRESS_EVENTS + 50):
            on_line(f"line {i}")
        return build_doc()

    routes_module().run_calibration_build_job(job_id, FakeClient(calibrate_build=build), videos,
                                              str(tmp_path / "k"), [], [])
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "done"
    assert len(job["events"]) <= suite_routes.MAX_PROGRESS_EVENTS + 5
    assert max(len(e["msg"]) for e in job["events"]) <= 600
    assert job["current"].startswith("line ")


# ── report ───────────────────────────────────────────────────────────────────────

def test_report_is_a_job_that_takes_no_heavy_slot(client, fake, videos, tmp_path):
    r = client.post("/suite/calibration/report", json={"kit": KIT, "paths": videos})
    job = job_of(client, r)
    assert (job["status"], job["kind"]) == ("done", "suite_calibration_report")
    assert job["results"] == report_doc()
    name, args, kwargs = fake.calls[0]
    assert name == "calibrate_report"
    assert args[0] == KIT and list(args[1]) == [str(Path(p).resolve()) for p in videos]
    assert callable(kwargs["cancel"])
    # the heavy slot is free the whole time; the score/rank cap is released afterwards
    assert suite_routes._slots.try_acquire("calibration") is None
    suite_routes._slots.release("calibration")
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


# ── status / session / record / apply / undo (synchronous) ───────────────────────

def test_status_without_and_with_a_kit(client, fake):
    r = client.get("/suite/calibration/status")
    assert r.status_code == 200 and r.json() == status_doc()
    assert fake.calls[0] == ("calibrate_status", (None,), {})
    r = client.get(f"/suite/calibration/status?kit={KIT}")
    assert r.status_code == 200
    assert fake.calls[1] == ("calibrate_status", (KIT,), {})
    assert no_jobs()


@pytest.mark.parametrize("kit", ["a b", "../x", "x" * 129, "a&b", ""])
def test_status_rejects_a_bad_kit_id(client, fake, kit):
    r = client.get("/suite/calibration/status", params={"kit": kit})
    assert r.status_code == 422
    assert fake.calls == []
    assert "input" not in r.json()["detail"][0]


def test_session_passes_the_resolved_out_folder_and_the_confirmed_conditions(client, fake, tmp_path):
    out = tmp_path / "kit"
    out.mkdir()
    r = client.post("/suite/calibration/session", json={"out": str(out), "tv_software": "V1.2 beta",
                                                        "audio_output": "avr", "usb_filesystem": "ntfs"})
    assert r.status_code == 200 and r.json() == session_doc()
    name, args, kwargs = fake.calls[0]
    assert name == "calibrate_session"
    assert args == (str(out.resolve()), "V1.2 beta", "avr", "ntfs")
    assert no_jobs()


def test_session_unknown_conditions_are_null(client, fake, tmp_path):
    out = tmp_path / "kit"
    out.mkdir()
    assert client.post("/suite/calibration/session", json={"out": str(out), "tv_software": "1"}).status_code == 200
    assert fake.calls[0][1] == (str(out.resolve()), "1", None, None)


@pytest.mark.parametrize("body", [{"tv_software": ""}, {"tv_software": "a\nb"}, {"tv_software": " x"},
                                  {"tv_software": "x" * 201}, {"tv_software": 5},
                                  {"audio_output": "headphones"}, {"audio_output": 3},
                                  {"usb_filesystem": "ext4"}, {"usb_filesystem": "NTFS"}, {"bogus": 1}])
def test_session_value_validation_is_422(client, fake, tmp_path, body):
    out = tmp_path / "kit"
    out.mkdir()
    r = client.post("/suite/calibration/session", json={"out": str(out), "tv_software": "1", **body})
    assert r.status_code == 422, r.text
    assert fake.calls == []


def test_session_out_must_be_an_existing_plain_folder(client, fake, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    (tmp_path / "a&b").mkdir()
    for bad, status in [(str(tmp_path / "missing"), 404), (str(f), 400), (str(tmp_path / "a&b"), 400),
                        ("\\\\h\\s\\kit", 400), ("relative", 400)]:
        r = client.post("/suite/calibration/session", json={"out": bad, "tv_software": "1"})
        assert r.status_code == status, bad
    assert fake.calls == []


def test_record_passes_the_answer(client, fake):
    r = client.post("/suite/calibration/record", json={"kit": KIT, "test": "T01", "player": "builtin_usb",
                                                       "outcome": "plays", "note": "looked fine"})
    assert r.status_code == 200 and r.json() == record_doc()
    assert fake.calls[0][0] == "calibrate_record"
    assert fake.calls[0][1] == (KIT, "T01", "builtin_usb", "plays", "looked fine")
    assert no_jobs()


def test_record_note_is_optional(client, fake):
    r = client.post("/suite/calibration/record", json={"kit": KIT, "test": "T01", "player": "p", "outcome": "ok"})
    assert r.status_code == 200
    assert fake.calls[0][1][4] == ""


@pytest.mark.parametrize("body", [{"kit": "a b"}, {"kit": ""}, {"test": "T 1"}, {"test": ""}, {"player": "../p"},
                                  {"outcome": "a&b"}, {"outcome": ""}, {"note": "a\nb"}, {"note": "x" * 2001},
                                  {"note": 4}, {"bogus": 1}])
def test_record_value_validation_is_422(client, fake, body):
    base = {"kit": KIT, "test": "T01", "player": "builtin_usb", "outcome": "ok"}
    assert client.post("/suite/calibration/record", json={**base, **body}).status_code == 422
    assert fake.calls == []
    assert client.post("/suite/calibration/record", json={"kit": KIT}).status_code == 422


def test_apply_preview_and_apply_and_undo(client, fake):
    r = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": True})
    assert r.status_code == 200 and r.json() == apply_doc()
    assert fake.calls[0][0] == "calibrate_apply"
    assert fake.calls[0][1] == (KIT,) and fake.calls[0][2] == {"preview": True, "revision": None, "confirm_veto": []}
    r = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "abc123",
                                                      "confirm_veto": ["T05:builtin_usb:audio_codecs.truehd"]})
    assert r.status_code == 200
    assert fake.calls[1][2] == {"preview": False, "revision": "abc123",
                                "confirm_veto": ["T05:builtin_usb:audio_codecs.truehd"]}
    r = client.post("/suite/calibration/undo", json={"kit": KIT})
    assert r.status_code == 200 and r.json() == apply_doc()
    assert fake.calls[2][0] == "calibrate_undo" and fake.calls[2][1] == (KIT,)
    assert no_jobs()


@pytest.mark.parametrize("body", [{"preview": "yes"}, {"preview": 1}, {"preview": None}, {},
                                  {"preview": True, "revision": "r1"},
                                  {"preview": True, "confirm_veto": ["a:b:c"]},
                                  {"preview": False}, {"preview": False, "revision": "a b"},
                                  {"preview": False, "revision": "r", "confirm_veto": ["a b"]},
                                  {"preview": False, "revision": "r", "confirm_veto": [""]},
                                  {"preview": False, "revision": "r", "confirm_veto": "a:b:c"},
                                  {"preview": False, "revision": "r", "confirm_veto": ["x" * 513]},
                                  {"preview": False, "revision": "r", "confirm_veto": ["a:b:c"] * 201},
                                  {"preview": False, "revision": "r", "bogus": 1}])
def test_apply_value_validation_is_422(client, fake, body):
    r = client.post("/suite/calibration/apply", json={"kit": KIT, **body})
    assert r.status_code == 422, r.text
    assert fake.calls == []


def test_apply_change_ids_may_hold_a_container_key(client, fake):
    ids = ["T09:builtin_usb:hdr_map.dv_p8_1@mp4", "T09:kodi:hdr_map.dv_p8_1"]
    r = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "r",
                                                      "confirm_veto": ids})
    assert r.status_code == 200
    assert fake.calls[0][2]["confirm_veto"] == ids


def test_undo_takes_only_a_kit(client, fake):
    assert client.post("/suite/calibration/undo", json={"kit": KIT, "revision": "r"}).status_code == 422
    assert client.post("/suite/calibration/undo", json={"kit": "a b"}).status_code == 422
    assert client.post("/suite/calibration/undo", json={}).status_code == 422
    assert fake.calls == []


# ── cache invalidation after apply / undo ────────────────────────────────────────

def _devices_calls(fake) -> int:
    return fake.names().count("devices")


def test_apply_and_undo_invalidate_the_devices_and_settings_cache_but_preview_does_not(client, fake):
    assert client.get("/suite/settings").status_code == 200
    assert client.get("/suite/devices").status_code == 200
    assert _devices_calls(fake) == 1
    assert client.post("/suite/calibration/apply", json={"kit": KIT, "preview": True}).status_code == 200
    client.get("/suite/devices")
    assert _devices_calls(fake) == 1                                          # a preview writes nothing
    assert client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "r"}).status_code == 200
    client.get("/suite/settings")
    assert _devices_calls(fake) == 2
    client.get("/suite/devices")
    assert _devices_calls(fake) == 2
    assert client.post("/suite/calibration/undo", json={"kit": KIT}).status_code == 200
    client.get("/suite/devices")
    assert _devices_calls(fake) == 3


def test_a_failed_apply_also_invalidates_because_the_settings_may_have_changed(client, fake):
    client.get("/suite/devices")
    fake.results["calibrate_apply"] = SuiteError("timeout", "x")
    assert client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "r"}).status_code == 504
    client.get("/suite/devices")
    assert _devices_calls(fake) == 2


def test_invalidation_waits_for_a_fill_in_progress(fake):
    suite_routes._fill_lock.acquire()
    done = threading.Event()
    t = start_in_thread(lambda: (suite_routes.invalidate_devices_cache(), done.set()))
    try:
        assert not done.wait(0.3)
    finally:
        suite_routes._fill_lock.release()
    assert done.wait(5)
    t.join(5)


def test_invalidation_is_not_reset_state_and_keeps_the_running_slots(client, fake):
    assert suite_routes._slots.acquire("compare")
    client.get("/suite/devices")
    assert client.post("/suite/calibration/undo", json={"kit": KIT}).status_code == 200
    assert suite_routes._devices_cache is None
    assert suite_routes._slots.try_acquire("compare") is not None            # the compare slot is still held


# ── errors of the synchronous routes ─────────────────────────────────────────────

SYNC_ROUTES = ["status", "session", "record", "apply", "undo"]
SYNC_METHOD = {"status": "calibrate_status", "session": "calibrate_session", "record": "calibrate_record",
               "apply": "calibrate_apply", "undo": "calibrate_undo"}


@pytest.mark.parametrize("route", SYNC_ROUTES)
@pytest.mark.parametrize("kind,status", [("unavailable", 503), ("timeout", 504), ("contract", 502), ("failed", 502),
                                         ("no_files", 502), ("ffprobe_missing", 502), ("usage", 400)])
def test_sync_suite_errors_map_to_clean_statuses_without_stderr(client, fake, videos, tmp_path, route, kind, status):
    fake.results[SYNC_METHOD[route]] = SuiteError(kind, "short", exit_code=9,
                                                  stderr_tail=f"{SECRET} C:\\Users\\x\\calibration.json")
    r = call(client, bodies(videos, tmp_path)[route] if route != "apply" else
             ("post", "/suite/calibration/apply", {"kit": KIT, "preview": True}))
    assert r.status_code == status
    assert SECRET not in r.text and "calibration.json" not in r.text and "C:\\" not in r.text
    assert isinstance(r.json()["detail"], str)
    assert no_jobs()


@pytest.mark.parametrize("tail,code,status", [
    ("videosuite: calibration store D:\\x\\calibration.json is damaged and was left untouched (bad json); x", "store_damaged", 409),
    ("videosuite: calibration store D:\\x is damaged (bad); it was not changed", "store_damaged", 409),
    ("videosuite: the record of kit secret-kit in the calibration store is malformed; it was not changed", "store_damaged", 409),
    ("videosuite: transaction 'secret-tx' of kit secret-kit in the calibration store is malformed; nothing was changed", "store_damaged", 409),
    ("videosuite: settings file D:\\s.json matches neither the state before nor after the unfinished calibration "
     "transaction 'tx1'; it was left pending.", "recovery_required", 409),
    ("videosuite: another VideoSuite calibration command is using D:\\x\\calibration.lock; waited 30 s.", "calibration_busy", 409),
    ("videosuite: the calibration store, the catalogue, the settings file or the conditions changed since the preview; "
     "run apply --preview again", "stale_revision", 409),
    ("videosuite: settings.json cannot be used as a settings document (x); it was not changed.", "settings_refused", 409),
    ("videosuite: cannot read the settings file D:\\s.json: boom; it was not changed", "settings_refused", 409),
])
def test_the_clis_refusals_become_conflict_responses_with_a_code(client, fake, tail, code, status):
    fake.results["calibrate_apply"] = SuiteError("failed", "videosuite calibrate-apply exited with code 1.",
                                                 exit_code=1, stderr_tail=tail)
    r = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "r"})
    assert r.status_code == status
    body = r.json()
    assert body["code"] == code and isinstance(body["detail"], str) and body["detail"]
    assert "D:\\" not in r.text and "tx1" not in r.text and "secret-kit" not in r.text and "secret-tx" not in r.text


@pytest.mark.parametrize("tail", ["settings changed while applying", "videosuite: settings changed while applying SECRET-VALUE-7f3a"])
def test_settings_changed_during_apply_is_a_safe_conflict(client, fake, tail):
    fake.results["calibrate_apply"] = SuiteError("failed", "failed", exit_code=1, stderr_tail=tail)
    response = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": False, "revision": "r"})
    assert response.status_code == 409
    assert response.json() == {"code": "settings_changed", "detail":
                               "The settings file changed while applying. Preview and apply again."}


def test_damaged_store_is_a_conflict_for_status_session_and_record_too(client, fake, tmp_path):
    damaged = SuiteError("failed", "x", exit_code=1,
                         stderr_tail="videosuite: calibration store D:\\x is damaged (bad); it was not changed")
    out = tmp_path / "kit"
    out.mkdir()
    for method, request in [("calibrate_status", lambda: client.get("/suite/calibration/status")),
                            ("calibrate_session", lambda: client.post("/suite/calibration/session", json={"out": str(out), "tv_software": "1"})),
                            ("calibrate_record", lambda: client.post("/suite/calibration/record", json={"kit": KIT, "test": "T01", "player": "p", "outcome": "ok"}))]:
        fake.results[method] = damaged
        r = request()
        assert (r.status_code, r.json()["code"]) == (409, "store_damaged"), method


def test_an_unrecognised_exit_1_failure_stays_a_generic_502(client, fake):
    fake.results["calibrate_status"] = SuiteError("failed", "x", exit_code=1, stderr_tail="videosuite: something odd")
    r = client.get("/suite/calibration/status")
    assert (r.status_code, r.json()) == (502, {"detail": "VideoSuite failed."})


def test_a_suite_without_the_calibrate_command_says_to_update(client, fake):
    fake.results["calibrate_status"] = SuiteError(
        "usage", "x", exit_code=2, stderr_tail="usage: videosuite ...\nvideosuite: error: argument command: invalid choice: 'calibrate'")
    r = client.get("/suite/calibration/status")
    assert r.status_code == 502
    assert "0.5.0" in r.json()["detail"] and r.json()["code"] == "suite_too_old"


def test_cli_recovery_required_flag_passes_through_as_data(client, fake):
    doc = status_doc()
    doc["recovery_required"] = True
    fake.results["calibrate_status"] = doc
    r = client.get("/suite/calibration/status")
    assert r.status_code == 200 and r.json()["recovery_required"] is True
    doc = apply_doc()
    doc.update(recovery_required=True, applied=False)
    fake.results["calibrate_apply"] = doc
    r = client.post("/suite/calibration/apply", json={"kit": KIT, "preview": True})
    assert r.status_code == 200 and r.json()["recovery_required"] is True


def test_argument_error_from_the_client_is_a_400_without_text(client, fake):
    fake.results["calibrate_record"] = SuiteArgumentError(f"note {SECRET}")
    r = client.post("/suite/calibration/record", json={"kit": KIT, "test": "T01", "player": "p", "outcome": "ok"})
    assert r.status_code == 400 and SECRET not in r.text


def test_unexpected_exception_in_a_sync_route_is_a_clean_500_without_text(client, fake):
    fake.results["calibrate_status"] = RuntimeError(SECRET)
    r = client.get("/suite/calibration/status")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error", "code": "internal"}
    assert SECRET not in r.text


@pytest.mark.parametrize("route", ROUTES)
def test_suite_unavailable_is_503_and_creates_no_job(client, videos, tmp_path, route):
    def boom():
        raise SuiteUnavailable("secret C:\\path")
    main.app.dependency_overrides[get_suite_client] = boom
    spec = bodies(videos, tmp_path)[route]
    r = call(client, spec, **({"deep_dv": True} if route == "plan" else {}))
    assert r.status_code == 503
    assert r.json() == {"detail": UNAVAILABLE}
    assert no_jobs()


# ── job outcomes ─────────────────────────────────────────────────────────────────

JOB_ROUTES = [("plan", "calibrate_plan", {"deep_dv": True}), ("build", "calibrate_build", {}),
              ("report", "calibrate_report", {})]


@pytest.mark.parametrize("kind", ERROR_KINDS)
@pytest.mark.parametrize("route,method,extra", JOB_ROUTES)
def test_every_error_kind_ends_the_job_with_that_kind_and_frees_the_slot(client, fake, videos, tmp_path, route, method, extra, kind):
    tail = "line\n" * 200 + "SECRET-TAIL-END"
    fake.results[method] = SuiteError(kind, "videosuite failed.", exit_code=3, stderr_tail=tail)
    job = job_of(client, call(client, bodies(videos, tmp_path)[route], **extra))
    assert job["status"] == "error" and job["error_kind"] == kind and job["error"] == "videosuite failed."
    assert job["error_detail"].endswith("SECRET-TAIL-END") and len(job["error_detail"]) <= 500
    assert job["exit_code"] == 3
    assert suite_routes._slots.try_acquire("calibration") is None
    suite_routes._slots.release("calibration")
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


@pytest.mark.parametrize("route,method,extra", JOB_ROUTES)
def test_timeout_and_unexpected_exceptions_end_the_job_honestly(client, fake, videos, tmp_path, route, method, extra):
    fake.results[method] = SuiteError("timeout", "videosuite calibrate timed out.")
    job = job_of(client, call(client, bodies(videos, tmp_path)[route], **extra))
    assert (job["status"], job["error_kind"]) == ("error", "timeout")
    jobs_module.jobs.clear()
    fake.results[method] = RuntimeError(f"boom {SECRET}")
    job = job_of(client, call(client, bodies(videos, tmp_path)[route], **extra))
    assert (job["status"], job["error_kind"]) == ("error", "internal")
    assert SECRET not in json.dumps(job)
    assert suite_routes._slots.try_acquire("calibration") is None


@pytest.mark.parametrize("route,method,extra", JOB_ROUTES)
def test_a_client_argument_error_inside_a_job_is_a_usage_error(client, fake, videos, tmp_path, route, method, extra):
    fake.results[method] = SuiteArgumentError("paths must be a non-empty list")
    job = job_of(client, call(client, bodies(videos, tmp_path)[route], **extra))
    assert (job["status"], job["error_kind"]) == ("error", "usage")


# ── 422 never echoes the input ───────────────────────────────────────────────────

BAD_BODIES = {
    "plan": {"paths": [SECRET], "players": [SECRET], "deep_dv": SECRET, "x" + SECRET: SECRET},
    "session": {"out": 5, "tv_software": SECRET, "audio_output": SECRET, "usb_filesystem": SECRET},
    "record": {"kit": SECRET + " x", "test": SECRET + " x", "player": 5, "outcome": SECRET + "&", "note": SECRET + "\n"},
    "build": {"paths": SECRET, "out": SECRET, "players": SECRET, "tests": [SECRET + " x"], "deep_dv": SECRET},
    "apply": {"kit": SECRET + " x", "preview": SECRET, "revision": SECRET + " x", "confirm_veto": [SECRET + " x"]},
    "undo": {"kit": SECRET + " x", "x" + SECRET: 1},
    "report": {"kit": SECRET + " x", "paths": [SECRET] * 60},
}


@pytest.mark.parametrize("route", sorted(BAD_BODIES))
def test_a_422_body_never_contains_the_input_or_the_offending_value(client, fake, route):
    r = client.post(f"/suite/calibration/{route}", json=BAD_BODIES[route])
    assert r.status_code == 422
    raw = r.text                                                              # tested on the RAW body
    assert '"input"' not in raw and '"ctx"' not in raw
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    for err in detail:
        assert set(err) <= {"type", "loc", "msg"}
        assert SECRET not in err["msg"]
    assert fake.calls == [] and no_jobs()


@pytest.mark.parametrize("route", sorted(BAD_BODIES))
def test_no_offending_value_appears_in_a_422_except_as_a_field_name(client, fake, route):
    raw = client.post(f"/suite/calibration/{route}", json=BAD_BODIES[route]).text
    stripped = raw
    for key in BAD_BODIES[route]:
        stripped = stripped.replace(f'"{key}"', "")
    assert SECRET not in stripped


@pytest.mark.parametrize("route", ["plan", "session", "record", "build", "apply", "undo", "report"])
def test_malformed_json_is_422_without_an_echo(client, fake, route):
    r = client.post(f"/suite/calibration/{route}", content=f'{{"kit": "{SECRET}", ', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert SECRET not in r.text and '"input"' not in r.text


def test_status_query_422_has_no_input_either(client, fake):
    r = client.get("/suite/calibration/status", params={"kit": SECRET + " x"})
    assert r.status_code == 422
    assert SECRET not in r.text and '"input"' not in r.text


@pytest.mark.parametrize("route", ["plan", "session", "record", "build", "apply", "undo", "report"])
def test_every_post_route_refuses_a_missing_body_and_extra_fields(client, fake, videos, tmp_path, route):
    assert client.post(f"/suite/calibration/{route}", json={}).status_code == 422
    spec = bodies(videos, tmp_path)[route]
    assert call(client, spec, bogus=1).status_code == 422
    assert fake.calls == [] and no_jobs()


# ── request guards ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route", ["plan", "session", "record", "build", "apply", "undo", "report"])
def test_text_plain_post_is_415_and_cross_site_is_403(client, fake, videos, tmp_path, route):
    method, url, body = bodies(videos, tmp_path)[route]
    r = client.post(url, content=json.dumps(body), headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert client.post(url, json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(url, json=body, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert fake.calls == [] and no_jobs()


def test_status_is_guarded_and_post_to_it_is_405(client, fake):
    assert client.get("/suite/calibration/status", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/suite/calibration/status", headers={"Host": "evil.example"}).status_code == 400
    assert client.post("/suite/calibration/status", json={}).status_code == 405
    assert client.get("/suite/calibration/plan").status_code == 405
    ok = client.get("/suite/calibration/status", headers={"Origin": "http://localhost:5173"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173"


# ── concurrency: the calibration slot ────────────────────────────────────────────

HEAVY_SUFFIX = "only one heavy job (a picture comparison, a deep Dolby Vision scan or a calibration kit job) runs at a time"


def test_slots_calibration_and_compare_exclude_each_other_both_ways():
    s = suite_routes._Slots()
    assert s.acquire("calibration")
    assert s.try_acquire("compare") == f"a calibration kit job is already running; {HEAVY_SUFFIX}"
    assert s.try_acquire("score_rank+deep_dv") == f"a calibration kit job is already running; {HEAVY_SUFFIX}"
    assert s.try_acquire("calibration") == "a calibration kit job is already running"
    assert s.acquire("score_rank")                                            # an ordinary score is not blocked
    s.release("calibration")
    assert s.acquire("compare")
    assert s.try_acquire("calibration") == f"a comparison is already running; {HEAVY_SUFFIX}"
    s.release("compare")
    assert s.acquire("score_rank+deep_dv")
    assert s.try_acquire("calibration") == f"a deep Dolby Vision scan is already running; {HEAVY_SUFFIX}"
    s.release("score_rank+deep_dv")
    assert s.acquire("calibration")


def test_slots_old_heavy_messages_are_unchanged():
    s = suite_routes._Slots()
    assert s.acquire("compare")
    assert s.try_acquire("score_rank+deep_dv") == (
        "a comparison is already running; only one heavy job (a picture comparison or a deep Dolby Vision scan) runs at a time")
    assert s.try_acquire("compare") == "a comparison is already running"


def test_slots_calibration_ignores_the_score_rank_cap_and_does_not_use_it():
    s = suite_routes._Slots()
    assert s.acquire("score_rank") and s.acquire("score_rank")
    assert s.try_acquire("calibration") is None
    s.release("calibration")
    assert s.try_acquire("score_rank") == "too many suite jobs are running"
    assert s.acquire("calibration")
    s.release("score_rank")
    s.release("score_rank")
    assert s.acquire("score_rank") and s.acquire("score_rank")                # calibration never consumed a score slot


def test_slots_a_refused_calibration_takes_nothing_and_release_is_symmetric():
    s = suite_routes._Slots()
    assert s.acquire("compare")
    assert s.try_acquire("calibration") is not None
    s.release("compare")
    assert s.acquire("calibration")                                           # nothing was left behind
    s.release("calibration")
    s.release("calibration")                                                  # a double release is harmless
    assert s.acquire("compare")
    s.release("calibration")                                                  # ...and never frees someone else's slot
    assert s.try_acquire("compare") is not None


def test_slots_calibration_check_and_take_is_atomic():
    s = suite_routes._Slots()
    barrier = threading.Barrier(5)
    results = []

    def take(slot):
        barrier.wait(timeout=5)
        results.append((slot, s.try_acquire(slot)))

    threads = [start_in_thread(take, slot) for slot in ("compare", "calibration", "score_rank+deep_dv", "calibration")]
    barrier.wait(timeout=5)
    for t in threads:
        t.join(5)
        assert not t.is_alive()
    assert len([1 for _, detail in results if detail is None]) == 1


class BlockingClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def _block(self, doc):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10)
        return doc

    def calibrate_build(self, *a, **k):
        return self._block(build_doc())

    def calibrate_plan(self, *a, **k):
        return self._block(plan_doc())

    def calibrate_report(self, *a, **k):
        return self._block(report_doc())

    def compare(self, *a, **k):
        return self._block(json.loads(json.dumps({"schema": "videosuite-compare/1", "tool_version": "0.5.0", "mode": "fast",
                                                  "estimate": {}, "shortlist": [], "dropped": [], "pairs": [], "failed": 0})))

    def score(self, *a, **k):
        return self._block({"schema": "videosuite-score/1", "tool_version": "0.5.0", "records": []})


CAL_HOLDERS = ["build", "plan_deep"]


def _hold_calibration(kind, videos, tmp_path):
    blocker = BlockingClient()
    job_id = jobs_module.jobs.create("suite_calibration_build" if kind == "build" else "suite_calibration_plan")
    mod = routes_module()
    if kind == "build":
        t = start_in_thread(mod.run_calibration_build_job, job_id, blocker, videos, str(tmp_path / "k"), [], [])
    else:
        t = start_in_thread(mod.run_calibration_plan_job, job_id, blocker, videos, [], deep_dv=True)
    assert blocker.started.wait(10)
    return blocker, job_id, t


@pytest.mark.parametrize("holder", CAL_HOLDERS)
def test_a_running_calibration_job_blocks_compare_deep_score_and_another_calibration_job(client, fake, videos, tmp_path, holder):
    blocker, job_id, t = _hold_calibration(holder, videos, tmp_path)
    try:
        before = len(jobs_module.jobs)
        compare = client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"})
        assert compare.status_code == 409
        assert compare.json() == {"detail": f"a calibration kit job is already running; {HEAVY_SUFFIX}"}
        deep = client.post("/suite/score", json={"paths": videos, "deep_dv": True})
        assert deep.status_code == 409 and deep.json() == compare.json()
        build = client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "k2")})
        assert (build.status_code, build.json()) == (409, {"detail": "a calibration kit job is already running"})
        plan = client.post("/suite/calibration/plan", json={"paths": videos, "deep_dv": True})
        assert (plan.status_code, plan.json()) == (409, {"detail": "a calibration kit job is already running"})
        assert len(jobs_module.jobs) == before                                # refusals create no job
        # not blocked: ordinary score, synchronous plan, the sync routes, the report job
        assert client.post("/suite/score", json={"paths": videos}).status_code == 200
        assert client.post("/suite/calibration/plan", json={"paths": videos}).status_code == 200
        assert client.get("/suite/calibration/status").status_code == 200
        assert client.post("/suite/calibration/report", json={"kit": KIT, "paths": videos}).status_code == 200
    finally:
        blocker.release.set()
        t.join(10)
    assert not t.is_alive()
    assert jobs_module.jobs.get(job_id)["status"] == "done"
    assert client.post("/suite/compare", json={"paths": videos, "out_dir": str(tmp_path / "o"), "mode": "fast"}).status_code == 200


@pytest.mark.parametrize("holder", ["compare", "deep_dv"])
def test_a_running_compare_or_deep_score_blocks_calibration_build_and_deep_plan(client, fake, videos, tmp_path, holder):
    blocker = BlockingClient()
    job_id = jobs_module.jobs.create("suite_compare" if holder == "compare" else "suite_score")
    if holder == "compare":
        t = start_in_thread(suite_routes.run_compare_job, job_id, blocker, videos, str(tmp_path / "o"))
    else:
        t = start_in_thread(suite_routes.run_score_job, job_id, blocker, videos, deep_dv=True)
    assert blocker.started.wait(10)
    try:
        text = ("a comparison is already running" if holder == "compare" else "a deep Dolby Vision scan is already running")
        detail = f"{text}; {HEAVY_SUFFIX}"
        before = len(jobs_module.jobs)
        r = client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "k")})
        assert (r.status_code, r.json()) == (409, {"detail": detail})
        r = client.post("/suite/calibration/plan", json={"paths": videos, "deep_dv": True})
        assert (r.status_code, r.json()) == (409, {"detail": detail})
        assert len(jobs_module.jobs) == before
        # a runner called directly ends busy and never calls the client
        other = FakeClient()
        refused = jobs_module.jobs.create("suite_calibration_build")
        routes_module().run_calibration_build_job(refused, other, videos, str(tmp_path / "k"), [], [])
        job = jobs_module.jobs.get(refused)
        assert (job["status"], job["error_kind"], job["error"]) == ("error", "busy", detail)
        assert other.calls == []
        assert client.post("/suite/calibration/plan", json={"paths": videos}).status_code == 200   # sync plan is free
    finally:
        blocker.release.set()
        t.join(10)
    assert client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "k")}).status_code == 200


def test_the_calibration_slot_is_released_after_every_ending(client, fake, videos, tmp_path):
    for result in (None, SuiteError("failed", "x"), RuntimeError("x"), SuiteError("timeout", "x")):
        fake.results["calibrate_build"] = result
        r = client.post("/suite/calibration/build", json={"paths": videos, "out": str(tmp_path / "k")})
        assert r.status_code == 200
        job = jobs_module.jobs.get(r.json()["job_id"])
        assert job["status"] == ("cancelled" if result is None else "error")
        assert suite_routes._slots.try_acquire("calibration") is None
        suite_routes._slots.release("calibration")


# ── cancel ───────────────────────────────────────────────────────────────────────

class HangingProc:
    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"working\n")
        self.pid = 4343
        self.returncode = None

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


@pytest.mark.parametrize("kind", ["plan", "build", "report"])
def test_cancel_route_kills_a_running_calibration_cli_and_ends_the_job_cancelled(client, videos, tmp_path, kind):
    proc = HangingProc()
    started = threading.Event()
    killed: list[int] = []
    argvs: list[list[str]] = []

    def popen(argv, **kwargs):
        argvs.append(list(argv))
        started.set()
        return proc

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    real = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=popen, killer=killer)
    mod = routes_module()
    job_id = jobs_module.jobs.create("suite_calibration_" + kind)
    if kind == "plan":
        t = start_in_thread(mod.run_calibration_plan_job, job_id, real, videos, [], deep_dv=True)
    elif kind == "build":
        t = start_in_thread(mod.run_calibration_build_job, job_id, real, videos, str(tmp_path / "k"), [], [])
    else:
        t = start_in_thread(mod.run_calibration_report_job, job_id, real, KIT, videos)
    assert started.wait(10)
    assert argvs[0][:4] == ["PY", "-m", "videosuite", "calibrate"] and argvs[0][4] == kind
    assert client.post(f"/job/{job_id}/cancel").status_code == 200           # the real cancel route
    t.join(15)
    assert not t.is_alive()
    assert killed == [4343]
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error"]) == ("cancelled", "cancelled")
    assert job["results"] == []
    assert suite_routes._slots.try_acquire("calibration") is None            # slots are free again
    suite_routes._slots.release("calibration")
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


@pytest.mark.parametrize("kind", ["plan", "build", "report"])
def test_cancel_http_route_reaches_a_queued_job_and_releases_its_reserved_slot(client, fake, videos, tmp_path, monkeypatch, kind):
    from fastapi import BackgroundTasks

    queued = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, fn, *args, **kw: queued.append((fn, args, kw)))
    extra = {"deep_dv": True} if kind == "plan" else {}
    r = call(client, bodies(videos, tmp_path)[kind], **extra)
    assert r.status_code == 200 and len(queued) == 1
    job_id = r.json()["job_id"]
    assert fake.calls == []
    slot = "score_rank" if kind == "report" else "calibration"
    assert suite_routes._slots._score_rank == (1 if kind == "report" else 0)
    assert suite_routes._slots._heavy == (None if kind == "report" else "calibration")
    assert client.post(f"/job/{job_id}/cancel").status_code == 200
    fn, args, kwargs = queued[0]
    assert kwargs["_slot_held"] is True
    fn(*args, **kwargs)
    assert fake.calls == []
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error"]) == ("cancelled", "cancelled")
    assert suite_routes._slots.try_acquire(slot) is None
    suite_routes._slots.release(slot)
    assert suite_routes._slots._heavy is None and suite_routes._slots._score_rank == 0


@pytest.mark.parametrize("kind", ["plan", "build", "report"])
def test_a_job_cancelled_while_queued_never_calls_the_client_and_frees_the_slot(videos, tmp_path, kind):
    job_id = jobs_module.jobs.create("suite_calibration_" + kind)
    assert jobs_module.jobs.request_cancel(job_id)
    fake = FakeClient()
    mod = routes_module()
    if kind == "plan":
        mod.run_calibration_plan_job(job_id, fake, videos, [], deep_dv=True)
    elif kind == "build":
        mod.run_calibration_build_job(job_id, fake, videos, str(tmp_path / "k"), [], [])
    else:
        mod.run_calibration_report_job(job_id, fake, KIT, videos)
    assert fake.calls == []
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error"]) == ("cancelled", "cancelled")
    assert suite_routes._slots.try_acquire("calibration") is None
    suite_routes._slots.release("calibration")
    assert suite_routes._slots.acquire("score_rank") and suite_routes._slots.acquire("score_rank")


@pytest.mark.parametrize("kind", ["plan", "build", "report"])
def test_the_cancel_callable_reads_the_job_flag_and_none_ends_cancelled(videos, tmp_path, kind):
    job_id = jobs_module.jobs.create("suite_calibration_" + kind)
    seen = []

    def go(*a, cancel=None, **k):
        seen.append(cancel())
        jobs_module.jobs.request_cancel(job_id)
        seen.append(cancel())
        return None

    method = {"plan": "calibrate_plan", "build": "calibrate_build", "report": "calibrate_report"}[kind]
    fake = FakeClient(**{method: go})
    mod = routes_module()
    if kind == "plan":
        mod.run_calibration_plan_job(job_id, fake, videos, [], deep_dv=True)
    elif kind == "build":
        mod.run_calibration_build_job(job_id, fake, videos, str(tmp_path / "k"), [], [])
    else:
        mod.run_calibration_report_job(job_id, fake, KIT, videos)
    assert seen == [False, True]
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "cancelled" and job["results"] == []


def test_a_running_job_cancelled_by_the_route_ends_cancelled_even_if_the_client_returns_a_document(client, videos, tmp_path):
    job_id = jobs_module.jobs.create("suite_calibration_build")

    def go(*a, cancel=None, **k):
        assert client.post(f"/job/{job_id}/cancel").status_code == 200
        return build_doc()

    routes_module().run_calibration_build_job(job_id, FakeClient(calibrate_build=go), videos, str(tmp_path / "k"), [], [])
    assert jobs_module.jobs.get(job_id)["status"] == "cancelled"
