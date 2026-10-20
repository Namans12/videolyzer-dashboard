"""Tests for suite_client (plan 2, task D10). No real subprocess and no real taskkill:
the client takes an injectable ``popen`` factory and ``killer``; fake process objects
replace the child."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import suite_client
from suite_client import (SuiteArgumentError, SuiteClient, SuiteError, SuiteUnavailable,
                          get_suite_client, resolve_suite)

FIX = Path(__file__).parent / "fixtures" / "suite"


def test_version_happy_path_and_argv():
    pop = FakePopen(ok("version.json"))
    doc = make_client(pop).version()
    assert doc["schema"] == "videosuite-version/1"
    assert pop.argv == ["PY", "-m", "videosuite", "version", "--json"]
    assert make_client(FakePopen())._timeout_devices() == suite_client.DEVICES_TIMEOUT_S


@pytest.mark.parametrize("name,command", [("devices", "devices"), ("version", "version"),
    ("score", "score"), ("score_partial", "score"), ("rank", "rank"), ("compare", "compare")])
def test_regenerated_fixtures_and_unknown_keys(name, command):
    doc = fixture_doc(name + ".json")
    assert doc["tool_version"] == "0.4.0"
    doc["future_field"] = {"unknown": [True, None]}
    parsed = suite_client.parse_document(command, json.dumps(doc).encode())
    assert parsed["future_field"] == doc["future_field"]


# '*' selects the first real mapping entry; indexes select a real fixture row.
_DEEP_CASES = [
    ("version", "schemas", []), ("version", "capabilities", [1]),
    ("version", "capabilities", {}), ("version", "facts_version", True),
    ("version", "facts_version", 1.5), ("version", "engine_tag", 1),
    ("score", "records.0", []), ("score", "records.0.id", 1),
    ("score", "records.0.source", []), ("score", "records.0.source.path", 1),
    ("score", "records.0.source.partial", 1), ("score", "records.0.playback", []),
    ("score", "records.0.playback.*", []),
    ("score", "records.0.playback.*.verdict", "UNKNOWN"),
    ("score", "records.0.playback.*.score", True),
    ("score", "records.0.playback.*.score_range", [1]),
    ("score", "records.0.playback.*.score_range", [1, True]),
    ("score", "records.0.playback.*.evidence", 1),
    ("score", "records.0.playback.*.effective_hdr", None),
    ("score", "records.0.playback.*.effective_audio", 1),
    ("score", "records.0.playback.*.issues", {}),
    ("score", "records.0.playback.*.issues", [1]),
    ("score", "records.0.playback.*.issues", [{"severity": 1, "message": "ok"}]),
    ("score", "records.0.playback.*.issues", [{"severity": "warn", "message": 1}]),
    ("score", "records.0.playback.*.hints", {}),
    ("score", "records.0.playback.*.hints", [1]),
    ("score", "records.0.best_player", []),
    ("score", "records.0.best_player", {"device": []}),
    ("score", "records.0.best_player", {"device": {"player": 1, "reason": "ok"}}),
    ("score", "records.0.best_player", {"device": {"player": "p", "reason": 1}}),
    ("rank", "quality.0", []), ("rank", "quality.0.id", 1),
    ("rank", "quality.0.label", 1), ("rank", "quality.0.rank", True),
    ("rank", "quality.0.games", True), ("rank", "quality.0.quality_score", True),
    ("rank", "devices.*", []), ("rank", "devices.*.ranking", {}),
    ("rank", "devices.*.will_not_play", {}), ("rank", "devices.*.not_compared", {}),
    ("rank", "devices.*.crossing_note", 1), ("rank", "devices.*.ranking.0", []),
    ("rank", "devices.*.ranking.0.label", 1), ("rank", "devices.*.ranking.0.device_rank", True),
    ("rank", "devices.*.ranking.0.verdict", "UNKNOWN"),
    ("rank", "devices.*.ranking.0.playback_score", True),
    ("rank", "devices.*.ranking.0.quality_score", True),
    ("rank", "devices.*.ranking.0.why", 1), ("rank", "devices.*.ranking.0.reasons", [1]),
    ("rank", "stale_reports", {}), ("rank", "legacy_reports", {}),
    ("rank", "head_to_head_conflicts", {}), ("devices", "devices.0.label", 1),
    ("devices", "devices.0.players.0.label", 1), ("devices", "devices.0.players.0.default", 1),
    ("devices", "devices.0.players.0.rule_counts", []),
    ("devices", "devices.0.players.0.rule_counts", {"rule": True}),
]
_DEEP_CASES += [("facts", path, value) for command, path, value in _DEEP_CASES
                if command == "score" and (path in ("records.0", "records.0.id") or ".source" in path)]


@pytest.mark.parametrize("command,path,value", _DEEP_CASES)
def test_deep_contract_rejects_invalid_path(command, path, value):
    doc = fixture_doc(("score" if command == "facts" else command) + ".json")
    doc["schema"] = f"videosuite-{command}/1"
    target = doc
    resolved = ""
    parts = path.split(".")
    for part in parts[:-1]:
        key = next(iter(target)) if part == "*" else int(part) if isinstance(target, list) else part
        target = target[key]
        resolved += f"[{key}]" if isinstance(key, int) else ("." if resolved else "") + str(key)
    last = parts[-1]
    key = next(iter(target)) if last == "*" else int(last) if isinstance(target, list) else last
    target[key] = value
    resolved += f"[{key}]" if isinstance(key, int) else ("." if resolved else "") + str(key)
    error = _contract(lambda: suite_client.parse_document(command, json.dumps(doc).encode()))
    assert resolved in str(error)


def test_optional_nulls_numbers_and_nested_unknown_keys_are_accepted():
    version = fixture_doc("version.json")
    version["engine_tag"] = None
    suite_client.parse_document("version", json.dumps(version).encode())
    del version["engine_tag"]
    suite_client.parse_document("version", json.dumps(version).encode())
    doc = fixture_doc("score.json")
    record = doc["records"][0]
    record["source"]["partial"] = False
    record["future"] = []
    for playback in record["playback"].values():
        playback.update(score=1, score_range=[1, 2.5], effective_audio=None, future={})
    record["best_player"] = {"device": {"player": "p", "reason": "r", "future": 1}}
    suite_client.parse_document("score", json.dumps(doc).encode())
    del record["best_player"]
    del record["source"]["partial"]
    suite_client.parse_document("score", json.dumps(doc).encode())
    doc = fixture_doc("rank.json")
    doc["quality"][0]["quality_score"] = None
    for device in doc["devices"].values():
        device["crossing_note"] = None
        device["ranking"][0].update(quality_score=None, why=None)
    suite_client.parse_document("rank", json.dumps(doc).encode())
    for key in ("stale_reports", "legacy_reports", "head_to_head_conflicts"):
        doc.pop(key, None)
    suite_client.parse_document("rank", json.dumps(doc).encode())


def fixture_bytes(name: str) -> bytes:
    return (FIX / name).read_bytes()


def fixture_doc(name: str) -> dict:
    return json.loads(fixture_bytes(name).decode("utf-8"))


# ── fakes ────────────────────────────────────────────────────────────────────────

class FakeProc:
    """A child that has already produced its output and exits with ``rc``.

    ``hang=True`` keeps ``wait`` raising TimeoutExpired until ``die()`` is called
    (what the injected killer does)."""

    def __init__(self, stdout=b"", stderr=b"", rc=0, hang=False, pid=4242):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.pid = pid
        self._rc = rc
        self.returncode = None if hang else rc
        self.hang = hang
        self.wait_calls = 0

    def die(self, rc=1):
        self.returncode = rc
        self.hang = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is not None:
            return self.returncode
        raise subprocess.TimeoutExpired("fake", timeout)

    def kill(self):  # pragma: no cover - only used if the killer is not injected
        self.die(-9)


class FakePopen:
    """Records every call; returns the queued FakeProc and snapshots the --list file."""

    def __init__(self, *procs):
        self.procs = list(procs)
        self.calls = []          # (argv, kwargs)
        self.list_text = None    # bytes of the --list file at call time
        self.list_dir = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if "--list" in argv or any(a.startswith("--list=") for a in argv):
            lf = argv[argv.index("--list") + 1] if "--list" in argv else None
            self.list_text = Path(lf).read_bytes()
            self.list_dir = Path(lf).parent
        return self.procs.pop(0)

    @property
    def argv(self):
        return self.calls[-1][0]


def make_client(popen, killer=None, clock=None, **kw):
    return SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=popen,
                       killer=killer or (lambda pid: None), clock=clock, **kw)


def ok(name, **kw):
    return FakeProc(stdout=fixture_bytes(name), **kw)


# ── resolution ───────────────────────────────────────────────────────────────────

def test_argv_prefix_wins_and_gets_utf8_env(tmp_path):
    c = SuiteClient(argv_prefix=["X:\\py.exe", "-m", "videosuite"], popen=FakePopen(),
                    environ={"PATH": "p"})
    assert c.argv_prefix == ["X:\\py.exe", "-m", "videosuite"]
    assert c.env_overrides["PYTHONUTF8"] == "1"
    assert c.env_overrides["PYTHONIOENCODING"] == "utf-8"


def test_resolution_order_constructor_then_env_vars_then_env_path(tmp_path):
    py = tmp_path / "envpy.exe"
    py.write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    envpy = tmp_path / "envvars_py.exe"
    envpy.write_text("x")
    root2 = tmp_path / "root2"
    root2.mkdir()
    setup = root / "setup"
    setup.mkdir()
    (setup / "env.path").write_text(f"python={py}\nkind=venv\nprefix={tmp_path}\n", encoding="utf-8")

    # 3) env.path only
    prefix, env = resolve_suite(environ={}, env_path=setup / "env.path")
    assert prefix == [str(py), "-m", "videosuite"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(root)
    # 2) environment variables beat env.path
    prefix, env = resolve_suite(environ={"VIDEOSUITE_PYTHON": str(envpy), "VIDEOSUITE_ROOT": str(root2)},
                                env_path=setup / "env.path")
    assert prefix == [str(envpy), "-m", "videosuite"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(root2)
    # 1) the constructor beats both
    c = SuiteClient(argv_prefix=["Z", "-m", "videosuite"], popen=FakePopen(),
                    environ={"VIDEOSUITE_PYTHON": str(envpy), "VIDEOSUITE_ROOT": str(root2)},
                    env_path=setup / "env.path")
    assert c.argv_prefix == ["Z", "-m", "videosuite"]


def test_env_path_parsing_and_root_is_grandparent(tmp_path):
    py = tmp_path / "python.exe"
    py.write_text("x")
    root = tmp_path / "App"
    (root / "setup").mkdir(parents=True)
    (root / "setup" / "env.path").write_text(f"kind=venv\r\nPYTHON = {py}\r\n", encoding="utf-8")
    prefix, env = resolve_suite(environ={"PYTHONPATH": "old"}, env_path=root / "setup" / "env.path")
    assert prefix[0] == str(py)
    assert env["PYTHONPATH"] == str(root)          # the inherited value is NOT appended (minor 3)
    assert env["PYTHONNOUSERSITE"] == "1"


def test_unresolvable_raises_suite_unavailable(tmp_path):
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={}, env_path=tmp_path / "missing" / "env.path")
    with pytest.raises(SuiteUnavailable):
        SuiteClient(environ={}, env_path=tmp_path / "missing" / "env.path")
    # only one of the two variables is not enough
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={"VIDEOSUITE_PYTHON": str(tmp_path / "py.exe")}, env_path=tmp_path / "nope")


@pytest.mark.parametrize("name", ["run_suite.bat", "python.cmd", "PY.BAT"])
def test_resolver_never_returns_a_bat(tmp_path, name):
    bat = tmp_path / name
    bat.write_text("@echo off")
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={"VIDEOSUITE_PYTHON": str(bat), "VIDEOSUITE_ROOT": str(root)}, env_path=tmp_path / "nope")
    (tmp_path / "setup").mkdir()
    (tmp_path / "setup" / "env.path").write_text(f"python={bat}\n", encoding="utf-8")
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={}, env_path=tmp_path / "setup" / "env.path")
    with pytest.raises(SuiteUnavailable):
        SuiteClient(argv_prefix=[str(bat)], popen=FakePopen())


def test_get_suite_client_is_a_singleton_and_resettable(monkeypatch, tmp_path):
    py = tmp_path / "p.exe"
    py.write_text("x")
    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setenv("VIDEOSUITE_PYTHON", str(py))
    monkeypatch.setenv("VIDEOSUITE_ROOT", str(root))
    suite_client.reset_suite_client()
    try:
        a = get_suite_client()
        assert get_suite_client() is a
        suite_client.reset_suite_client()
        assert get_suite_client() is not a
    finally:
        suite_client.reset_suite_client()


def test_get_suite_client_unavailable(monkeypatch, tmp_path):
    monkeypatch.delenv("VIDEOSUITE_PYTHON", raising=False)
    monkeypatch.delenv("VIDEOSUITE_ROOT", raising=False)
    monkeypatch.setattr(suite_client, "DEFAULT_ENV_PATH", tmp_path / "none" / "env.path")
    suite_client.reset_suite_client()
    try:
        with pytest.raises(SuiteUnavailable):
            get_suite_client()
    finally:
        suite_client.reset_suite_client()


# ── happy paths per command ──────────────────────────────────────────────────────

def test_devices_happy_path():
    pop = FakePopen(ok("devices.json"))
    doc = make_client(pop).devices()
    assert doc["schema"] == "videosuite-devices/1"
    assert [d["id"] for d in doc["devices"]] == ["galaxy_tab_s9_ultra", "sony_bravia_8_ii", "vivobook_pro_16x"]
    assert pop.argv == ["PY", "-m", "videosuite", "devices", "--json"]
    assert "--list" not in pop.argv


def test_score_happy_path_and_argv():
    pop = FakePopen(ok("score.json"))
    doc = make_client(pop).score(["C:\\a.mkv", "C:\\b.mkv"], device="sony_bravia_8_ii", player="kodi",
                                 fs="ntfs", lang="eng", deep_dv=True)
    assert len(doc["records"]) == 3
    a = pop.argv
    assert a[:4] == ["PY", "-m", "videosuite", "score"]
    assert "--json" in a and "--deep-dv" in a
    assert "--device=sony_bravia_8_ii" in a and "--player=kodi" in a
    assert "--fs=ntfs" in a and "--lang=eng" in a
    assert "--list" in a


def test_rank_happy_path_and_argv(tmp_path):
    pop = FakePopen(ok("rank.json"))
    doc = make_client(pop).rank(["C:\\a.mkv"], reports_dir=str(tmp_path), device="all", player="all",
                                fs=None, lang="eng", wq=0.7, wp=0.3)
    assert doc["schema"] == "videosuite-rank/1" and "quality" in doc
    a = pop.argv
    assert f"--reports={tmp_path}" in a
    assert "--wq=0.7" in a and "--wp=0.3" in a
    assert not any(x.startswith("--fs") for x in a)
    assert "--deep-dv" not in a


def test_facts_happy_path():
    doc = fixture_doc("score.json")
    doc["schema"] = "videosuite-facts/1"
    pop = FakePopen(FakeProc(stdout=json.dumps(doc).encode("utf-8")))
    assert make_client(pop).facts(["C:\\a.mkv"])["schema"] == "videosuite-facts/1"
    assert pop.argv[3] == "facts"


def test_compare_happy_path(tmp_path):
    pop = FakePopen(ok("compare.json", stderr=b"2 file(s) shortlisted\n  a vs b: done\n"))
    lines = []
    doc = make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], out_dir=str(tmp_path), mode="fast", top=3,
                                   on_line=lines.append)
    assert doc["schema"] == "videosuite-compare/1" and doc["failed"] == 0
    assert doc.exit_code == 0
    assert lines == ["2 file(s) shortlisted", "  a vs b: done"]
    assert doc.stderr_tail.splitlines() == lines
    assert f"--out={tmp_path}" in pop.argv and "--mode=fast" in pop.argv and "--top=3" in pop.argv
    assert "--json" in pop.argv


def test_popen_kwargs_no_shell_no_stdin():
    pop = FakePopen(ok("devices.json"))
    make_client(pop).devices()
    kw = pop.calls[0][1]
    assert kw.get("shell", False) is False
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.PIPE
    assert isinstance(pop.calls[0][0], list)
    assert kw["env"]["PYTHONUTF8"] == "1"


# ── contract violations ──────────────────────────────────────────────────────────

def _contract(client_call):
    with pytest.raises(SuiteError) as e:
        client_call()
    assert e.value.kind == "contract"
    return e.value


def test_wrong_schema_is_contract_error():
    pop = FakePopen(FakeProc(stdout=fixture_bytes("bad_schema.json")))
    _contract(lambda: make_client(pop).score(["C:\\a.mkv"]))


def test_schema_of_another_command_is_contract_error():
    pop = FakePopen(ok("rank.json"))
    _contract(lambda: make_client(pop).score(["C:\\a.mkv"]))


def test_empty_stdout_is_contract_error():
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=b"  \r\n"))).devices())


def test_non_utf8_stdout_is_contract_error():
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=b'{"schema": "\xff\xfe"}'))).devices())


def test_non_json_stdout_is_contract_error():
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=b"ERROR: Python environment not found"))).devices())


def test_json_that_is_not_an_object_is_contract_error():
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=b"[1, 2]"))).devices())


def test_nan_is_contract_error():
    doc = fixture_bytes("devices.json").replace(b'"schema":', b'"x": NaN, "schema":', 1)
    assert b"NaN" in doc
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=doc))).devices())


def test_oversize_stdout_is_contract_error():
    big = b'{"schema": "videosuite-devices/1", "pad": "' + b"a" * (20 * 1024 * 1024 + 10) + b'"}'
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=big))).devices())


@pytest.mark.parametrize("cmd,drop", [
    ("devices", "devices"), ("score", "records"), ("rank", "quality"), ("rank", "missing_pairs"),
    ("rank", "weights"), ("compare", "pairs"), ("compare", "failed"), ("compare", "estimate"),
    ("devices", "tool_version"), ("devices", "schema"),
])
def test_missing_required_key_is_contract_error(cmd, drop, tmp_path):
    fname = {"devices": "devices.json", "score": "score.json", "rank": "rank.json", "compare": "compare.json"}[cmd]
    doc = fixture_doc(fname)
    del doc[drop]
    pop = FakePopen(FakeProc(stdout=json.dumps(doc).encode("utf-8")))
    c = make_client(pop)
    call = {"devices": lambda: c.devices(), "score": lambda: c.score(["C:\\a.mkv"]),
            "rank": lambda: c.rank(["C:\\a.mkv"], str(tmp_path)),
            "compare": lambda: c.compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path))}[cmd]
    _contract(call)


def test_wrong_type_of_required_key_is_contract_error():
    doc = fixture_doc("devices.json")
    doc["devices"] = {"not": "a list"}
    _contract(lambda: make_client(FakePopen(FakeProc(stdout=json.dumps(doc).encode()))).devices())


def test_optional_rank_keys_are_accepted(tmp_path):
    doc = fixture_doc("rank.json")
    doc["stale_reports"] = []
    doc["legacy_reports"] = []
    pop = FakePopen(FakeProc(stdout=json.dumps(doc).encode()))
    assert "stale_reports" in make_client(pop).rank(["C:\\a.mkv"], str(tmp_path))


# ── exit codes ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rc,kind", [(2, "usage"), (3, "no_files"), (4, "ffprobe_missing"), (1, "failed"), (9, "failed"), (-1, "failed")])
def test_exit_code_maps_to_error_kind(rc, kind):
    pop = FakePopen(FakeProc(stdout=b"", stderr=b"videosuite: something\nsecond line\n", rc=rc))
    with pytest.raises(SuiteError) as e:
        make_client(pop).score(["C:\\a.mkv"])
    assert e.value.kind == kind
    assert e.value.exit_code == rc
    assert "second line" in e.value.stderr_tail


def test_nonzero_exit_does_not_parse_stdout():
    # a valid document with exit 3 is still an error for score
    pop = FakePopen(FakeProc(stdout=fixture_bytes("score.json"), rc=3))
    with pytest.raises(SuiteError) as e:
        make_client(pop).score(["C:\\a.mkv"])
    assert e.value.kind == "no_files"


def test_compare_exit_1_keeps_the_document(tmp_path):
    doc = fixture_doc("compare.json")
    doc["failed"] = 1
    doc["pairs"][0]["report"] = None
    pop = FakePopen(FakeProc(stdout=json.dumps(doc).encode(), stderr=b"  a vs b: FAILED\n", rc=1))
    out = make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path))
    assert out["failed"] == 1 and out.exit_code == 1
    assert out["pairs"][0]["report"] is None
    assert "FAILED" in out.stderr_tail


def test_compare_exit_1_without_a_document_is_failed_error(tmp_path):
    pop = FakePopen(FakeProc(stdout=b"", stderr=b"Traceback ...\n", rc=1))
    with pytest.raises(SuiteError) as e:
        make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path))
    assert e.value.kind == "failed"
    assert "Traceback" in e.value.stderr_tail


@pytest.mark.parametrize("rc,kind", [(2, "usage"), (3, "no_files"), (4, "ffprobe_missing"), (7, "failed")])
def test_compare_other_exit_codes(rc, kind, tmp_path):
    pop = FakePopen(FakeProc(stdout=fixture_bytes("compare.json"), rc=rc))
    with pytest.raises(SuiteError) as e:
        make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path))
    assert e.value.kind == kind


def test_stderr_tail_keeps_only_the_last_200_lines():
    err = "".join(f"line {i}\n" for i in range(500)).encode()
    pop = FakePopen(FakeProc(stdout=b"", stderr=err, rc=2))
    with pytest.raises(SuiteError) as e:
        make_client(pop).devices()
    lines = e.value.stderr_tail.splitlines()
    assert len(lines) == 200 and lines[0] == "line 300" and lines[-1] == "line 499"


def test_stderr_non_utf8_is_decoded_with_replacement():
    pop = FakePopen(FakeProc(stdout=b"", stderr=b"bad \xff byte\n", rc=2))
    with pytest.raises(SuiteError) as e:
        make_client(pop).devices()
    assert "bad" in e.value.stderr_tail and "\ufffd" in e.value.stderr_tail


# ── list files, argv hygiene ─────────────────────────────────────────────────────

def test_argv_has_list_and_no_user_path(tmp_path):
    pop = FakePopen(ok("score.json"))
    make_client(pop).score(["Z:\\Library\\Tom&Jerry (2021).mkv", "Y:\\x\\Film.mkv"])
    a = pop.argv
    assert "--list" in a
    joined = "\0".join(a)
    assert "Tom&Jerry" not in joined and "Film.mkv" not in joined and "Z:\\Library" not in joined


def test_ampersand_path_round_trips_through_list_file_byte_for_byte():
    paths = ["C:\\Videos\\Tom&Jerry (2021).mkv", "D:\\R&D\\100% ^ \"q\" Fïlm 映画.mkv"]
    pop = FakePopen(ok("score.json"))
    make_client(pop).score(paths)
    assert pop.list_text == ("\n".join(paths) + "\n").encode("utf-8")
    assert not pop.list_text.startswith(b"\xef\xbb\xbf")


def test_list_dir_is_removed_after_success_failure_and_timeout(tmp_path):
    pop = FakePopen(ok("score.json"))
    make_client(pop).score(["C:\\a.mkv"])
    assert not pop.list_dir.exists()

    pop = FakePopen(FakeProc(rc=3))
    with pytest.raises(SuiteError):
        make_client(pop).score(["C:\\a.mkv"])
    assert not pop.list_dir.exists()

    pop = FakePopen(FakeProc(stdout=b"junk"))
    with pytest.raises(SuiteError):
        make_client(pop).rank(["C:\\a.mkv"], str(tmp_path))
    assert not pop.list_dir.exists()


def test_list_dir_is_removed_when_popen_raises():
    seen = {}

    def boom(argv, **kw):
        lf = argv[argv.index("--list") + 1]
        seen["dir"] = Path(lf).parent
        raise OSError("cannot start")

    with pytest.raises(SuiteError) as e:
        make_client(boom).score(["C:\\a.mkv"])
    assert e.value.kind == "unavailable"
    assert not seen["dir"].exists()


def test_list_dir_is_removed_when_cancel_callback_raises(tmp_path):
    proc = FakeProc(hang=True)
    pop = FakePopen(proc)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die()

    def cancel():
        raise RuntimeError("callback bug")

    with pytest.raises(RuntimeError):
        make_client(pop, killer=killer).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), cancel=cancel)
    assert not pop.list_dir.exists()
    assert killed == [4242]          # the process tree never outlives an exception


# ── argument validation ──────────────────────────────────────────────────────────

def _no_call(*_a, **_k):  # pragma: no cover
    raise AssertionError("popen must not be called for invalid arguments")


@pytest.mark.parametrize("kw", [
    {"lang": "eng&calc"}, {"lang": "eng\n"}, {"lang": "e"}, {"lang": ""}, {"lang": "en-"}, {"lang": "eng-abcdefghij"},
    {"device": "a b"}, {"device": "x&y"}, {"device": ""}, {"device": "d" * 65}, {"device": "ä"},
    {"player": "p%PATH%"}, {"player": "a\nb"},
    {"fs": "ext4"}, {"fs": "NTFS"}, {"fs": "ntfs&"},
])
def test_score_rejects_bad_values(kw):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).score(["C:\\a.mkv"], **kw)


@pytest.mark.parametrize("lang", ["eng", "en", "pt-BR", "zh-Hant", "deu"])
def test_lang_accepts_valid_values(lang):
    pop = FakePopen(ok("score.json"))
    make_client(pop).score(["C:\\a.mkv"], lang=lang)
    assert f"--lang={lang}" in pop.argv


@pytest.mark.parametrize("paths", [[], "C:\\a.mkv", [1], ["a\nb.mkv"], ["a\r.mkv"], ["a\0b.mkv"], [""], ["  "]])
def test_bad_path_lists_are_rejected(paths):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).score(paths)


@pytest.mark.parametrize("wq,wp", [(-1, 1), (1, -0.1), (0, 0), (float("nan"), 1), (1, float("inf")), (1001, 1), (True, 1), ("0.5", 1), (None, 1)])
def test_rank_rejects_bad_weights(wq, wp, tmp_path):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).rank(["C:\\a.mkv"], str(tmp_path), wq=wq, wp=wp)


def test_rank_accepts_edge_weights(tmp_path):
    pop = FakePopen(ok("rank.json"), ok("rank.json"))
    make_client(pop).rank(["C:\\a.mkv"], str(tmp_path), wq=0, wp=1000)
    make_client(pop).rank(["C:\\a.mkv"], str(tmp_path), wq=1000, wp=0)


@pytest.mark.parametrize("bad", ["C:\\x&y", "C:\\x^y", "C:\\x%PATH%", "C:\\x!y", 'C:\\x"y', "C:\\x'y", "C:\\x<y", "C:\\x>y", "C:\\x|y",
                                 "", "C:\\x\ny", "C:\\x\0y", 7])
def test_dirs_reject_shell_metacharacters(bad):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).rank(["C:\\a.mkv"], bad)
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).compare(["C:\\a.mkv", "C:\\b.mkv"], bad)


def test_dirs_accept_spaces_and_unicode():
    pop = FakePopen(ok("rank.json"))
    make_client(pop).rank(["C:\\a.mkv"], "C:\\My Reports\\Fïlm 映画 (2021)")
    assert "--reports=C:\\My Reports\\Fïlm 映画 (2021)" in pop.argv


@pytest.mark.parametrize("kw", [{"top": 0}, {"top": 7}, {"top": True}, {"top": 2.5}, {"top": "3"}, {"mode": "slow"}, {"mode": "FAST"}, {"mode": None}])
def test_compare_rejects_bad_top_and_mode(kw, tmp_path):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), **kw)


def test_compare_accepts_top_bounds_and_accurate(tmp_path):
    pop = FakePopen(ok("compare.json"), ok("compare.json"))
    make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), mode="accurate", top=1)
    make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), mode="fast", top=6)
    assert "--mode=accurate" in pop.calls[0][0] and "--top=1" in pop.calls[0][0]


def test_argument_error_is_a_value_error():
    assert issubclass(SuiteArgumentError, ValueError)


# ── timeouts, cancellation, progress ─────────────────────────────────────────────

class StepClock:
    """Advances by ``step`` every time it is read."""

    def __init__(self, step):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_timeouts_scale_with_files_and_deep_dv():
    c = make_client(FakePopen())
    assert c._timeout_devices() == 30
    assert c._timeout_score_rank(1, False) == 60 + 45
    assert c._timeout_score_rank(4, False) == 60 + 4 * 45
    assert c._timeout_score_rank(2, True) == 60 + 2 * 45 + 2 * 900
    assert c._timeout_compare() == 6 * 3600


def test_compare_timeout_env_override():
    c = make_client(FakePopen(), environ={"VIDEOSUITE_COMPARE_TIMEOUT_S": "90"})
    assert c._timeout_compare() == 90
    c = make_client(FakePopen(), environ={"VIDEOSUITE_COMPARE_TIMEOUT_S": "banana"})
    assert c._timeout_compare() == 6 * 3600
    c = make_client(FakePopen(), environ={"VIDEOSUITE_COMPARE_TIMEOUT_S": "-5"})
    assert c._timeout_compare() == 6 * 3600


def test_timeout_kills_the_tree_and_raises_timeout_error():
    proc = FakeProc(stderr=b"working...\n", hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    pop = FakePopen(proc)
    with pytest.raises(SuiteError) as e:
        make_client(pop, killer=killer, clock=StepClock(20)).devices()      # 30 s ceiling passes after 2 reads
    assert e.value.kind == "timeout"
    assert killed == [4242]
    assert "working..." in e.value.stderr_tail


def test_compare_cancel_calls_the_killer_and_returns(tmp_path):
    proc = FakeProc(stderr=b"2 file(s) shortlisted\n", hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    pop = FakePopen(proc)
    lines = []
    out = make_client(pop, killer=killer).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path),
                                                  on_line=lines.append, cancel=lambda: True)
    assert out is None
    assert killed == [4242]
    assert not pop.list_dir.exists()


def test_score_cancel_calls_the_killer_and_returns_none():
    proc = FakeProc(hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    pop = FakePopen(proc)
    out = make_client(pop, killer=killer).score(["C:\\a.mkv"], cancel=lambda: True)
    assert out is None
    assert killed == [4242]
    assert not pop.list_dir.exists()


def test_rank_cancel_calls_the_killer_and_returns_none(tmp_path):
    proc = FakeProc(hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    pop = FakePopen(proc)
    out = make_client(pop, killer=killer).rank(["C:\\a.mkv"], str(tmp_path), cancel=lambda: True)
    assert out is None
    assert killed == [4242]
    assert not pop.list_dir.exists()


def test_compare_without_cancel_flag_does_not_kill(tmp_path):
    proc = FakeProc(stdout=fixture_bytes("compare.json"))
    killed = []
    out = make_client(FakePopen(proc), killer=killed.append).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path),
                                                                      cancel=lambda: False)
    assert out["failed"] == 0 and killed == []


def test_default_killer_runs_taskkill_tree_without_a_shell(monkeypatch, request):
    calls = []
    monkeypatch.setattr(suite_client.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    request.getfixturevalue("guard_taskkill")(1234)
    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", "1234"]
    assert calls[0][1].get("shell", False) is False


def test_progress_lines_reach_on_line_while_the_process_runs(tmp_path):
    """A stderr that delivers lines gradually: each line is forwarded as it arrives."""
    r_fd, w_fd = os.pipe()
    reader = os.fdopen(r_fd, "rb")
    writer = os.fdopen(w_fd, "wb")
    got = []
    seen_first = threading.Event()

    def on_line(line):
        got.append(line)
        if line.startswith("first"):
            seen_first.set()

    class PipeProc(FakeProc):
        def __init__(self):
            super().__init__(stdout=fixture_bytes("compare.json"), hang=True)
            self.stderr = reader
            self.step = 0

        def wait(self, timeout=None):
            # first poll: write one line and wait until the client consumed it; then finish
            if self.step == 0:
                self.step = 1
                writer.write("first: 1/2 started\n".encode())
                writer.flush()
                assert seen_first.wait(5), "on_line was not called while the process was still running"
                writer.write("second: done ž\n".encode("utf-8"))
                writer.flush()
                writer.close()
                self.die(0)
            return super().wait(timeout)

    pop = FakePopen(PipeProc())
    out = make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), on_line=on_line)
    assert got == ["first: 1/2 started", "second: done ž"]
    assert out.stderr_tail.splitlines() == got
    reader.close()


def test_on_line_exception_does_not_break_the_run(tmp_path):
    def bad(_line):
        raise RuntimeError("ui bug")

    pop = FakePopen(ok("compare.json", stderr=b"a\nb\n"))
    out = make_client(pop).compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path), on_line=bad)
    assert out["failed"] == 0 and out.stderr_tail.splitlines() == ["a", "b"]


def test_popen_oserror_is_unavailable_error():
    def boom(argv, **kw):
        raise FileNotFoundError("no python")

    with pytest.raises(SuiteError) as e:
        make_client(boom).devices()
    assert e.value.kind == "unavailable"


def test_error_kinds_are_the_documented_set():
    assert set(suite_client.ERROR_KINDS) == {"usage", "no_files", "ffprobe_missing", "failed_pairs", "contract",
                                             "timeout", "failed", "unavailable"}
    with pytest.raises(ValueError):
        SuiteError("nonsense", "x")


def test_real_taskkill_is_never_used_by_the_default_fixtures(monkeypatch):
    # guard for this test module itself: the injected killer is what runs
    monkeypatch.setattr(suite_client, "taskkill_tree", lambda pid: pytest.fail("real taskkill called"))
    proc = FakeProc(hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die()

    out = make_client(FakePopen(proc), killer=killer).compare(["C:\\a.mkv", "C:\\b.mkv"], "C:\\out", cancel=lambda: True)
    assert out is None and killed == [4242]


# ── fix round 1 ──────────────────────────────────────────────────────────────────

# Every character that the CLI's splitlines()/strip() would turn into a second path or change.
_SEPARATORS = ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\n", "\r", "\r\n", "\0", "\x01", "\x1f", "\t"]


@pytest.mark.parametrize("sep", _SEPARATORS, ids=[f"U+{ord(s[0]):04X}" for s in _SEPARATORS])
def test_line_separators_in_a_path_are_rejected(sep):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).score([f"C:\\one{sep}two.mkv"])
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).facts([f"C:\\one{sep}two.mkv"])


@pytest.mark.parametrize("sep", _SEPARATORS, ids=[f"U+{ord(s[0]):04X}" for s in _SEPARATORS])
def test_line_separators_in_a_dir_are_rejected(sep):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).rank(["C:\\a.mkv"], f"C:\\one{sep}two")
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).compare(["C:\\a.mkv", "C:\\b.mkv"], f"C:\\one{sep}two")


@pytest.mark.parametrize("edge", [" C:\\a.mkv", "C:\\a.mkv ", "\u00a0C:\\a.mkv", "C:\\a.mkv\u00a0", "\u3000C:\\a.mkv", "C:\\a.mkv\u2003"])
def test_leading_and_trailing_space_in_a_path_is_rejected(edge):
    with pytest.raises(SuiteArgumentError) as e:
        make_client(_no_call).score(["C:\\ok.mkv", edge])
    assert "space" in str(e.value) or "white" in str(e.value)


@pytest.mark.parametrize("edge", [" C:\\r", "C:\\r ", "\u00a0C:\\r", "C:\\r\u00a0"])
def test_leading_and_trailing_space_in_a_dir_is_rejected(edge):
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).rank(["C:\\a.mkv"], edge)
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).compare(["C:\\a.mkv", "C:\\b.mkv"], edge)


def test_inner_spaces_and_nbsp_are_fine_and_round_trip():
    paths = ["C:\\Fi lm\u00a0(2021).mkv", "C:\\Тест 映画.mkv"]
    pop = FakePopen(ok("score.json"))
    make_client(pop).score(paths)
    assert pop.list_text == ("\n".join(paths) + "\n").encode("utf-8")
    assert [ln for ln in pop.list_text.decode("utf-8").splitlines()] == paths       # the CLI reads it back unchanged


def test_argument_error_does_not_echo_the_path():
    with pytest.raises(SuiteArgumentError) as e:
        make_client(_no_call).score(["C:\\secret\u2028name.mkv"])
    assert "secret" not in str(e.value)


# launcher hardening (minor 1)

@pytest.mark.parametrize("suffix", ["", " ", "  ", ".", ". ", "::$DATA", ":stream"])
def test_resolver_rejects_batch_launcher_disguises(tmp_path, suffix):
    bat = tmp_path / "fakepy.bat"
    bat.write_text("@echo off")
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={"VIDEOSUITE_PYTHON": str(bat) + suffix, "VIDEOSUITE_ROOT": str(root)}, env_path=tmp_path / "nope")
    (tmp_path / "setup").mkdir()
    (tmp_path / "setup" / "env.path").write_text(f"python={bat}{suffix}\n", encoding="utf-8")
    with pytest.raises(SuiteUnavailable):
        resolve_suite(environ={}, env_path=tmp_path / "setup" / "env.path")
    with pytest.raises(SuiteUnavailable):
        SuiteClient(argv_prefix=[str(bat) + suffix, "-m", "videosuite"], popen=FakePopen())


@pytest.mark.parametrize("prefix", [
    ["cmd.exe", "/c", "x.bat"], ["C:\\Windows\\System32\\CMD.EXE", "/c", "x"], ["powershell", "-File", "x.ps1"],
    ["pwsh.exe", "-c", "x"], ["wscript.exe", "x.vbs"], ["CScript", "x.vbs"], ["C:\\Windows\\System32\\cmd", "/c"],
])
def test_shell_interpreters_are_refused_as_launchers(prefix):
    with pytest.raises(SuiteUnavailable):
        SuiteClient(argv_prefix=prefix, popen=FakePopen())


def test_a_plain_python_exe_prefix_is_still_accepted():
    SuiteClient(argv_prefix=["C:\\Apps\\py\\python.exe", "-m", "videosuite"], popen=FakePopen())


# working folder and environment of the child (minor 2 and 3)

def _client_from_env(tmp_path, popen, environ):
    py = tmp_path / "venv_python.exe"
    py.write_text("x")
    root = tmp_path / "suite_root"
    root.mkdir(exist_ok=True)
    env = {"VIDEOSUITE_PYTHON": str(py), "VIDEOSUITE_ROOT": str(root)}
    env.update(environ)
    return SuiteClient(popen=popen, killer=lambda pid: None, environ=env), root


def test_child_runs_in_the_suite_root_with_safe_path(tmp_path):
    pop = FakePopen(ok("devices.json"))
    c, root = _client_from_env(tmp_path, pop, {"PATH": "p"})
    c.devices()
    kw = pop.calls[0][1]
    assert kw["cwd"] == str(root)
    assert kw["env"]["PYTHONSAFEPATH"] == "1"


def test_child_environment_drops_inherited_python_variables(tmp_path):
    pop = FakePopen(ok("devices.json"))
    inherited = {"PATH": "C:\\Windows", "SystemRoot": "C:\\Windows", "TEMP": "C:\\T", "PYTHONPATH": "C:\\evil",
                 "PYTHONHOME": "C:\\other", "PYTHONSTARTUP": "C:\\s.py", "PYTHONINSPECT": "1", "PYTHONWARNINGS": "error",
                 "pythondontwritebytecode": "1"}
    c, root = _client_from_env(tmp_path, pop, inherited)
    c.devices()
    env = pop.calls[0][1]["env"]
    assert env["PYTHONPATH"] == str(root)
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONSAFEPATH"] == "1"
    for gone in ("PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONWARNINGS", "pythondontwritebytecode"):
        assert gone not in env
    assert env["SystemRoot"] == "C:\\Windows" and env["TEMP"] == "C:\\T"
    assert env["PATH"].split(os.pathsep)[-1] == "C:\\Windows"
    assert "VIDEOSUITE_PYTHON" in env                        # unrelated variables are kept


def test_child_environment_comes_from_the_injected_mapping_not_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("ONLY_IN_OS_ENVIRON", "1")
    pop = FakePopen(ok("devices.json"))
    c, _root = _client_from_env(tmp_path, pop, {"PATH": "p"})
    c.devices()
    assert "ONLY_IN_OS_ENVIRON" not in pop.calls[0][1]["env"]


def test_argv_prefix_client_has_safe_path_and_no_inherited_python_vars():
    pop = FakePopen(ok("devices.json"))
    c = SuiteClient(argv_prefix=["X:\\py.exe", "-m", "videosuite"], popen=pop,
                    environ={"PATH": "p", "PYTHONHOME": "h", "PYTHONPATH": "x"})
    c.devices()
    env = pop.calls[0][1]["env"]
    assert env["PYTHONSAFEPATH"] == "1" and "PYTHONHOME" not in env and "PYTHONPATH" not in env
    assert pop.calls[0][1].get("cwd") is None


# list dir after a timeout (minor 6)

def test_list_dir_is_removed_after_a_timeout_and_the_tree_is_killed(tmp_path):
    proc = FakeProc(hang=True)
    killed = []

    def killer(pid):
        killed.append(pid)
        proc.die(1)

    pop = FakePopen(proc)
    c = make_client(pop, killer=killer, clock=StepClock(1000), environ={"VIDEOSUITE_COMPARE_TIMEOUT_S": "5"})
    with pytest.raises(SuiteError) as e:
        c.compare(["C:\\a.mkv", "C:\\b.mkv"], str(tmp_path))
    assert e.value.kind == "timeout"
    assert killed == [4242]
    assert pop.list_dir is not None and not pop.list_dir.exists()


def test_list_dir_is_removed_after_a_score_timeout(tmp_path):
    proc = FakeProc(hang=True)
    killed = []
    pop = FakePopen(proc)
    c = make_client(pop, killer=lambda pid: (killed.append(pid), proc.die(1)), clock=StepClock(1000))
    with pytest.raises(SuiteError) as e:
        c.score(["C:\\a.mkv"])
    assert e.value.kind == "timeout" and killed == [4242]
    assert not pop.list_dir.exists()


def test_docstring_states_the_caller_contract():
    doc = suite_client.__doc__
    assert "is None" in doc and "failed_pairs" in doc and "SuiteUnavailable" in doc and 'kind="unavailable"' in doc


@pytest.mark.parametrize("name", ["Tom & Jerry (2021) 100%.mkv", "-Тест ^%.mkv"])
def test_score_partial_argv_fixture_and_timeout(name, monkeypatch):
    pop = FakePopen(ok("score_partial.json"))
    client = make_client(pop)
    original = client._with_list
    timeouts = []
    def capture(command, paths, extra, timeout, **kwargs):
        timeouts.append(timeout)
        return original(command, paths, extra, timeout, **kwargs)
    monkeypatch.setattr(client, "_with_list", capture)
    doc = client.score_partial("C:\\partial.mkv", real_size=30000000000, real_name=name)
    assert doc["records"][0]["source"]["partial"] is True
    assert timeouts == [105]
    assert "--list" in pop.argv and "C:\\partial.mkv" not in pop.argv
    assert "--real-size=30000000000" in pop.argv
    assert f"--real-name={name}" in pop.argv and "--coverage=head" in pop.argv
    assert pop.calls[0][1].get("shell", False) is False


@pytest.mark.parametrize("key,value", [("real_size", x) for x in [0, -1, True, "5", 2**51]] +
                         [("real_name", x) for x in ["", "a/b.mkv", "a\\b.mkv", "x\n", "x\u2028y", "x"*256]] +
                         [("coverage", "tail"), ("path", "x\n")])
def test_score_partial_invalid_before_process(key, value):
    kwargs = dict(path="C:\\partial.mkv", real_size=100, real_name="movie.mkv")
    kwargs[key] = value
    with pytest.raises(SuiteArgumentError):
        make_client(_no_call).score_partial(**kwargs)


@pytest.mark.parametrize("killer_kind", ["injected", "default_fake"])
def test_partial_stage_timeout_and_all_cleanup_share_five_seconds(monkeypatch, killer_kind):
    import magnet_suite
    now = [0.0]
    def clock(): return now[0]
    class SlowReader:
        oversize = False
        data = fixture_bytes("score_partial.json")
        text = ""
        def __init__(self, *args): pass
        def start(self): pass
        def join(self, timeout): now[0] += timeout
    monkeypatch.setattr(suite_client, "_StdoutReader", SlowReader)
    monkeypatch.setattr(suite_client, "_StderrReader", SlowReader)
    class Process(FakeProc):
        def wait(self, timeout=None):
            if self.hang:
                now[0] += timeout
                raise subprocess.TimeoutExpired("fake", timeout)
            return self.returncode
        def kill(self): pass
    procs = [Process(stdout=fixture_bytes("score_partial.json")) for _ in range(3)] + [Process(hang=True)]
    pop = FakePopen(*procs)
    killed = []
    def killer(pid):
        killed.append(pid)
        now[0] += 2.0
    def fake_taskkill(pid, *, timeout):
        assert 0 < timeout <= 2.0
        killed.append(pid)
        now[0] += timeout
    monkeypatch.setattr(suite_client, "taskkill_tree", fake_taskkill)
    client = (make_client(pop, clock=clock, killer=killer) if killer_kind == "injected" else
              SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=pop, clock=clock))
    original = client.score_partial
    count = [0]
    def partial(*args, **kwargs):
        count[0] += 1
        if count[0] < 4:
            # Three successful files approach the stage budget, including their cleanup.
            result = original(*args, **kwargs)
            now[0] = count[0] * (299.9 / 3)
            return result
        return original(*args, **kwargs)
    client.score_partial = partial
    monkeypatch.setattr(magnet_suite.suite_version, "ensure", lambda c: {"capabilities": ["score_partial"]})
    entries = [dict(key=str(i), path="C:\\probe.mkv", real_size=100, real_name="movie.mkv", coverage="head") for i in range(4)]
    out = magnet_suite.score_partials(entries, lambda m: None, lambda: False, client_factory=lambda: client, clock=clock)
    assert out["3"]["error_kind"] == "timeout"
    assert killed == [4242]
    assert now[0] <= 409.9
    assert not pop.list_dir.exists()


@pytest.mark.parametrize("args,expected", [((8, "accurate", 4), 252600),
    ((8, "fast", 4), 51000), ((2, "fast", 4), 21600), ((3, "accurate", 4), 27600),
    ((20, "fast", 4), 51000), ((), 21600), ((8, "unknown", 4), 252600)])
def test_d4_compare_timeout_scales_with_shortlisted_pairs(args, expected):
    assert make_client(FakePopen(), environ={})._timeout_compare(*args) == expected


@pytest.mark.parametrize("raw", ["90", "0.5", "banana", "0", "-5", "nan", "inf"])
def test_d4_compare_timeout_environment_override(raw):
    client = make_client(FakePopen(), environ={"VIDEOSUITE_COMPARE_TIMEOUT_S": raw})
    expected = float(raw) if raw in ("90", "0.5") else 252600
    assert client._timeout_compare(8, "accurate", 4) == expected


@pytest.mark.parametrize("mode,top", [("accurate", 3), ("fast", 4)])
def test_d4_compare_passes_file_count_mode_top_to_timeout(monkeypatch, tmp_path, mode, top):
    client = make_client(FakePopen(ok("compare.json")))
    calls = []
    def timeout(*args):
        calls.append(args)
        return 90
    monkeypatch.setattr(client, "_timeout_compare", timeout)
    paths = [str(tmp_path / f"{i}.mkv") for i in range(8)]
    client.compare(paths, str(tmp_path), mode, top)
    assert calls == [(8, mode, top)]


def test_partial_cleanup_does_not_block_on_hung_killer_or_pipe_close(monkeypatch):
    import time
    release = threading.Event()
    entered = threading.Event()
    class Pipe(io.BytesIO):
        def close(self): release.wait(2)
    class Reader:
        oversize = False
        data = b""
        text = ""
        def __init__(self, *args): pass
        def start(self): pass
        def join(self, timeout): time.sleep(timeout)
    monkeypatch.setattr(suite_client, "_StdoutReader", Reader)
    monkeypatch.setattr(suite_client, "_StderrReader", Reader)
    proc = FakeProc(hang=True)
    proc.stdout, proc.stderr = Pipe(), Pipe()
    def killer(pid):
        entered.set()
        release.wait(2)
    client = make_client(FakePopen(proc), killer=killer)
    original = client._with_list
    def shortened(command, paths, extra, timeout, **kwargs):
        assert timeout == 105 and kwargs["cleanup_budget"] == 5.0
        return original(command, paths, extra, 0.01, **dict(kwargs, cleanup_budget=0.35))
    monkeypatch.setattr(client, "_with_list", shortened)
    start = time.monotonic()
    try:
        with pytest.raises(SuiteError, match="timed out"):
            client.score_partial("C:\\probe.mkv", real_size=100, real_name="movie.mkv")
        assert entered.is_set()
        assert time.monotonic() - start < 0.6
    finally:
        release.set()


def test_d5_fake_factory_uses_null_job():
    import proc_guard
    assert make_client(FakePopen())._job is proc_guard.NULL_JOB


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("assignment_raises", [False, True])
def test_d5_assignment_and_live_registry(raises, assignment_raises):
    assigned = []
    class Job:
        def assign(self, proc):
            assigned.append(proc)
            if assignment_raises:
                raise RuntimeError("job unavailable")
            return True
    class Proc(FakeProc):
        def wait(self, timeout=None):
            assert self in suite_client.live_processes()
            if raises:
                raise RuntimeError("wait failed")
            return super().wait(timeout)
    proc = Proc(stdout=fixture_bytes("devices.json"))
    client = make_client(FakePopen(proc), job=Job())
    if raises:
        with pytest.raises(RuntimeError, match="wait failed"):
            client.devices()
    else:
        client.devices()
    assert assigned == [proc]
    assert proc not in suite_client.live_processes()


@pytest.mark.parametrize("default", [False, True])
def test_d5_shutdown_killer_resolved_at_call_time(monkeypatch, default):
    proc = FakeProc()
    killed, waited = [], []
    proc.wait = waited.append
    with suite_client._LIVE_LOCK:
        suite_client._LIVE.add(proc)
    monkeypatch.setattr(suite_client, "taskkill_tree", killed.append)
    assert suite_client.kill_live_processes(**({} if default else {"killer": killed.append})) == 1
    assert killed == [proc.pid] and waited == [5]


def test_d5_lifespan_stops_live_processes(monkeypatch, caplog):
    import main
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(suite_client, "kill_live_processes", lambda: calls.append(True) or 1)
    with caplog.at_level("INFO"), TestClient(main.app, base_url="http://localhost"):
        pass
    assert calls == [True]
    assert "stopped 1 VideoSuite process(es) on shutdown" in caplog.text


def test_d5_reset_clears_registry_and_default_kill_is_guarded(clean_state):
    proc = FakeProc()
    with suite_client._LIVE_LOCK:
        suite_client._LIVE.add(proc)
    with pytest.raises(pytest.fail.Exception, match="real taskkill called in a test"):
        suite_client.kill_live_processes()
    clean_state()
    assert suite_client.live_processes() == []


def test_d5_shutdown_swallows_process_exceptions():
    proc = FakeProc()
    waited = []
    def wait(timeout):
        waited.append(timeout)
        raise RuntimeError("wait failed")
    def killer(pid):
        raise RuntimeError("kill failed")
    proc.wait = wait
    with suite_client._LIVE_LOCK:
        suite_client._LIVE.add(proc)
    assert suite_client.kill_live_processes(killer) == 1
    assert waited == [5]


def test_d5_real_factory_selects_default_job(monkeypatch):
    import proc_guard
    marker = object()
    monkeypatch.setattr(proc_guard, "default_job", lambda: marker)
    client = SuiteClient(argv_prefix=["PY", "-m", "videosuite"])
    assert client._job is marker


@pytest.mark.parametrize("failed_reader", ["stdout", "stderr"])
@pytest.mark.parametrize("job_kind", ["null", "failed_assignment"])
@pytest.mark.parametrize("partial", [False, True])
def test_d5_reader_start_failure_cleans_before_unregister(monkeypatch, failed_reader, job_kind, partial):
    import proc_guard
    now = [0.0]
    calls = []
    error = RuntimeError("cannot start new thread")
    class Proc(FakeProc):
        def wait(self, timeout=None):
            assert self in suite_client.live_processes()
            calls.append(("wait", timeout))
            now[0] += 0.5
            return super().wait(timeout)
    proc = Proc(hang=True)
    class Pipe(io.BytesIO):
        def close(self):
            assert proc in suite_client.live_processes()
            calls.append(("close", None))
            super().close()
    proc.stdout, proc.stderr = Pipe(), Pipe()
    class Reader:
        def __init__(self, stream, *args):
            self.name = "stdout" if stream is proc.stdout else "stderr"
            self.started = False
        def start(self):
            if self.name == failed_reader:
                raise error
            self.started = True
        def join(self, timeout):
            if not self.started:
                raise RuntimeError("cannot join thread before it is started")
            calls.append(("join", timeout))
            now[0] += min(timeout, 3.0)
    monkeypatch.setattr(suite_client, "_StdoutReader", Reader)
    monkeypatch.setattr(suite_client, "_StderrReader", Reader)
    class FailedJob:
        def assign(self, child):
            assert child is proc
            return False
    def killer(pid):
        assert proc in suite_client.live_processes()
        calls.append(("kill", pid))
        now[0] += 2.0
        proc.die(1)
    job = proc_guard.NULL_JOB if job_kind == "null" else FailedJob()
    pop = FakePopen(proc)
    client = make_client(pop, killer=killer, job=job, clock=lambda: now[0])
    with pytest.raises(RuntimeError) as caught:
        if partial:
            client.score_partial("C:\\probe.mkv", real_size=100, real_name="movie.mkv")
        else:
            client.devices()
    assert caught.value is error
    assert ("kill", proc.pid) in calls
    assert any(action == "wait" for action, _ in calls)
    assert proc.stdout.closed and proc.stderr.closed
    assert proc not in suite_client.live_processes()
    if partial:
        assert now[0] <= 5.0
        assert not pop.list_dir.exists()


@pytest.mark.parametrize("failed_reader", ["stdout", "stderr"])
@pytest.mark.parametrize("default_killer", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("assignment_fails", [False, True])
def test_d5_global_thread_exhaustion_uses_thread_free_tree_cleanup(
        monkeypatch, guard_taskkill, failed_reader, default_killer, partial, assignment_fails):
    now = [0.0]
    original = RuntimeError("cannot start new thread")
    killed, waited, closed = [], [], []
    class Proc(FakeProc):
        def wait(self, timeout=None):
            assert self in suite_client.live_processes()
            waited.append(timeout)
            now[0] += min(timeout, 0.5)
            return super().wait(timeout)
    proc = Proc(hang=True)
    class Pipe(io.BytesIO):
        def close(self):
            assert proc in suite_client.live_processes()
            closed.append(True)
            super().close()
    proc.stdout, proc.stderr = Pipe(), Pipe()
    def fail_start(thread):
        # Let the stdout reader finish synchronously for the stderr-start case;
        # every other start, including all cleanup workers, fails globally.
        if failed_reader == "stderr" and thread.name == "suite-stdout":
            thread.run()
            return
        raise original
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    def tree_killer(pid, *, timeout=None):
        assert proc in suite_client.live_processes()
        if partial and default_killer:
            assert 0 < timeout <= 2.0
        killed.append(pid)
        now[0] += 1.0
        proc.die(1)
    if default_killer:
        def fake_run(argv, **kwargs):
            assert argv == ["taskkill", "/F", "/T", "/PID", str(proc.pid)]
            assert kwargs["shell"] is False
            tree_killer(proc.pid, timeout=kwargs["timeout"])
        # Exercise the actual default command/timeout without launching taskkill.
        monkeypatch.setattr(suite_client.subprocess, "run", fake_run)
        monkeypatch.setattr(suite_client, "taskkill_tree", guard_taskkill)
    # Injected path retains the autouse fail guard on real/default taskkill.
    class FailedJob:
        def assign(self, child):
            return False
    pop = FakePopen(proc)
    kwargs = {} if default_killer else {"killer": tree_killer}
    client = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=pop,
                         clock=lambda: now[0], job=FailedJob() if assignment_fails else None, **kwargs)
    with pytest.raises(RuntimeError) as caught:
        if partial:
            client.score_partial("C:\\probe.mkv", real_size=100, real_name="movie.mkv")
        else:
            client.devices()
    assert caught.value is original
    assert killed == [proc.pid] and waited
    assert proc.stdout.closed and proc.stderr.closed and len(closed) == 2
    assert proc not in suite_client.live_processes()
    if partial:
        assert now[0] <= 5.0 and not pop.list_dir.exists()


def test_d5_exhausted_cleanup_does_not_close_active_pipe_inline(monkeypatch, guard_taskkill):
    import time
    read_started = threading.Event()
    release_writer = threading.Event()
    r_fd, w_fd = os.pipe()
    class Pipe(io.BufferedReader):
        def read(self, size=-1):
            read_started.set()
            return super().read(size)
    stdout = Pipe(os.fdopen(r_fd, "rb", buffering=0))
    writer = os.fdopen(w_fd, "wb", buffering=0)
    def safety_release():
        release_writer.wait(0.8)
        writer.close()
    # Prestarted helper guarantees teardown even when the regression blocks.
    releaser = threading.Thread(target=safety_release, daemon=True)
    releaser.start()
    original_start = threading.Thread.start
    original_error = RuntimeError("cannot start new thread")
    readers = []
    def exhausted(thread):
        if thread.name == "suite-stdout":
            readers.append(thread)
            original_start(thread)
            assert read_started.wait(1)
            time.sleep(0.02)  # reader enters the real blocking buffered read
            return
        raise original_error
    monkeypatch.setattr(threading.Thread, "start", exhausted)
    calls = []
    def taskkill_timeout(argv, **kwargs):
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(suite_client.subprocess, "run", taskkill_timeout)
    monkeypatch.setattr(suite_client, "taskkill_tree", guard_taskkill)
    proc = FakeProc(hang=True)
    proc.stdout = stdout
    pop = FakePopen(proc)
    client = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], popen=pop)
    original_with_list = client._with_list
    def shortened(command, paths, extra, timeout, **kwargs):
        return original_with_list(command, paths, extra, timeout, **dict(kwargs, cleanup_budget=0.35))
    monkeypatch.setattr(client, "_with_list", shortened)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            client.score_partial("C:\\probe.mkv", real_size=100, real_name="movie.mkv")
        elapsed = time.monotonic() - started
        assert caught.value is original_error
        assert calls and 0 < calls[0] <= 0.101
        assert elapsed < 0.5
        assert proc.stderr.closed
        assert not stdout.closed and readers[0].is_alive()
        assert proc not in suite_client.live_processes()
        assert not pop.list_dir.exists()
    finally:
        release_writer.set()
        releaser.join(2)
        for reader in readers:
            reader.join(2)
        stdout.close()


# Plan 4A D1: frozen CLI documents, deliberately independent of engine imports.
_CAL_KIT = "a" * 12 + "-" + "b" * 64
_CAL_VETO = "T01:builtin_usb:hdr_map.dv_p8_1@mp4"
_CAL_PATHS = ["C:\\Тест & 100%\\movie.mkv"]


def calibration_doc(sub):
    prediction = dict(outcome=None, text="unknown", evidence="inferred")
    descriptor = dict(test="T01", player="builtin_usb", title="Banner", **{"class": "decisive"},
                      rule="hdr_map.dv_p8_1@mp4", instruction="Play", question="Banner?",
                      outcomes=[dict(id="yes", text="Yes", value={"out": "DV", "risk": None, "qoe": "KEEP"})],
                      conditions=["audio_output"], manual=False, file="clip.mp4",
                      source=dict(kind="invented", file=None, verification=None), prediction=prediction)
    session = dict(tv_software="V1", audio_output=None, usb_filesystem=None, confirmed_at="today")
    summary = dict(done=1, total=1, matches=0, differs=0)
    answer = dict(kit_id=_CAL_KIT, test="T01", player="builtin_usb", outcome="unsure", note="",
                  conditions=dict(audio_output=None, usb_filesystem=None, tv_software="V1"),
                  at="today", older_tv_software=False, match="unsure")
    side = dict(verdict="PLAYS_WELL", score=90, evidence="owner", effective_audio=None, effective_hdr="DV")
    bodies = {
        "plan": dict(catalogue_version=1, players=["builtin_usb"], tests=[dict(test="T01", title="Banner",
            **{"class": "decisive"}, players=["builtin_usb"], manual=False, instruction="Play", question="Banner?",
            outcomes=[dict(id="yes", text="Yes")], prediction={"builtin_usb": prediction},
            source=dict(kind="invented", status="ready", file=None, est_bytes=None, reason=None))]),
        "build": dict(kit_id=_CAL_KIT, out="C:\\kit", descriptor_path="C:\\kit\\kit.json", total_bytes=123,
                      descriptors=[descriptor], unavailable=[dict(test="T02", reason="no source")]),
        "session": dict(kit_id=_CAL_KIT, session=session),
        "record": dict(answer=answer, summary=summary),
        "status": dict(recovery_required=False, kit=dict(id=_CAL_KIT, catalogue_version=1, built_at="today",
                      out="C:\\kit", descriptors=[descriptor]), session=session, answers=[answer], summary=summary),
        "apply": dict(revision="c" * 64, applied=False, recovery_required=False,
            changes=[dict(id=_CAL_VETO, test="T01", player="builtin_usb", rule="hdr_map.dv_p8_1@mp4",
                          action="veto", reason="silent", previous=None, value="silent", veto=True)],
            conflicts=[dict(rule="audio_map.aac", player="builtin_usb", tests=["T01", "T02"])],
            skipped=[dict(test="T02", player="builtin_usb", reason="unsure")]),
        "report": dict(note="playback only, not a quality ranking", kit_active=True, recovery_required=False,
            files=[dict(path=_CAL_PATHS[0], player="tv/builtin_usb", before=side, after=side, changed=False)],
            best_player_changes=[dict(path=_CAL_PATHS[0], device="tv", before=dict(player="a", verdict="RISKY"),
                                      after=dict(player="b", verdict="PLAYS_WELL"))]),
    }
    return dict(schema=f"videosuite-calibrate-{sub}/1", tool_version="0.5.0", **bodies[sub])


def calibration_call(client, sub, **kwargs):
    calls = {
        "plan": lambda: client.calibrate_plan(_CAL_PATHS, ["builtin_usb", "kodi"], **kwargs),
        "build": lambda: client.calibrate_build(_CAL_PATHS, "C:\\kit", ["builtin_usb", "kodi"], ["T01", "T02"], **kwargs),
        "session": lambda: client.calibrate_session("C:\\kit", "V & 100% Тест", "tv_speakers", "exfat"),
        "record": lambda: client.calibrate_record(_CAL_KIT, "T01", "builtin_usb", "unsure", "Тест & 100%"),
        "status": lambda: client.calibrate_status(_CAL_KIT),
        "apply": lambda: client.calibrate_apply(_CAL_KIT, preview=False, revision="c" * 64, confirm_veto=[_CAL_VETO]),
        "undo": lambda: client.calibrate_undo(_CAL_KIT),
        "report": lambda: client.calibrate_report(_CAL_KIT, _CAL_PATHS, **kwargs),
    }
    return calls[sub]()


@pytest.mark.parametrize("sub", ["plan", "build", "session", "record", "status", "apply", "undo", "report"])
def test_calibration_argv_documents_and_timeouts(sub, monkeypatch):
    command = "apply" if sub == "undo" else sub
    pop = FakePopen(FakeProc(stdout=json.dumps(calibration_doc(command)).encode(), stderr=b"progress\n"))
    client = make_client(pop)
    runs = []
    original = client._run
    def capture(command, args, timeout, **kwargs):
        runs.append((command, timeout))
        return original(command, args, timeout, **kwargs)
    monkeypatch.setattr(client, "_run", capture)
    lines = []
    kwargs = {"deep_dv": True} if sub in ("plan", "build") else {}
    if sub in ("build", "report"):
        kwargs["on_line"] = lines.append
    result = calibration_call(client, sub, **kwargs)
    assert result == calibration_doc(command)
    assert pop.argv[:5] == ["PY", "-m", "videosuite", "calibrate", command]
    assert "--json" in pop.argv and pop.calls[0][1]["shell"] is False
    expected_timeout = {"plan": 1005, "build": 1740, "report": 105}.get(sub, 60)
    assert runs == [(f"calibrate-{command}", expected_timeout)]
    if sub in ("plan", "build", "report"):
        assert pop.list_text == ("\n".join(_CAL_PATHS) + "\n").encode()
        assert _CAL_PATHS[0] not in pop.argv and not pop.list_dir.exists()
    if sub in ("plan", "build"):
        assert "--deep-dv" in pop.argv
        assert "--player=builtin_usb" in pop.argv and "--player=kodi" in pop.argv
    if sub == "build":
        assert "--tests=T01,T02" in pop.argv and "--out=C:\\kit" in pop.argv and "--yes" in pop.argv
        assert result["total_bytes"] == 123
    if sub == "session":
        assert "--kit-file=C:\\kit\\kit.json" in pop.argv
        assert "--tv-software=V & 100% Тест" in pop.argv
        assert "--audio-output=tv_speakers" in pop.argv and "--usb-fs=exfat" in pop.argv
    if sub == "record":
        assert "--note=Тест & 100%" in pop.argv and "--outcome=unsure" in pop.argv and "--test=T01" in pop.argv
    if sub == "apply":
        assert f"--confirm-veto={_CAL_VETO}" in pop.argv and "--revision=" + "c" * 64 in pop.argv
    if sub == "undo":
        assert f"--undo={_CAL_KIT}" in pop.argv and not any(a.startswith("--kit=") for a in pop.argv)
    if sub in ("build", "report"):
        assert lines == ["progress"]


def _calibration_required_paths(value, prefix=()):
    # Values/previous/verification are opaque JSON; all other frozen keys are required.
    if isinstance(value, dict):
        for key, child in value.items():
            yield prefix + (key,)
            if key not in ("value", "previous", "verification"):
                yield from _calibration_required_paths(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _calibration_required_paths(child, prefix + (index,))


_CAL_SUBS = ["plan", "build", "session", "record", "status", "apply", "report"]
_CAL_REQUIRED_CASES = [(sub, path, invalid) for sub in _CAL_SUBS
                       for path in _calibration_required_paths(calibration_doc(sub))
                       for invalid in (False, True)
                       if not (invalid and path[-1] in ("value", "previous", "verification"))]


@pytest.mark.parametrize("sub,path,invalid", _CAL_REQUIRED_CASES)
def test_calibration_contract_required_nested_fields(sub, path, invalid):
    doc = calibration_doc(sub)
    row = doc
    for part in path[:-1]:
        row = row[part]
    if invalid:
        row[path[-1]] = [] if not isinstance(row[path[-1]], list) else {}
    else:
        del row[path[-1]]
    with pytest.raises(SuiteError) as error:
        suite_client.parse_document(f"calibrate-{sub}", json.dumps(doc).encode())
    assert error.value.kind == "contract"


@pytest.mark.parametrize("sub", _CAL_SUBS)
def test_calibration_contract_additions_and_oversize(sub):
    doc = calibration_doc(sub)
    doc["future"] = {"anything": True}
    if sub == "status":
        doc.update(kit=None, session=None, answers=[])
    assert suite_client.parse_document(f"calibrate-{sub}", json.dumps(doc).encode()) == doc
    with pytest.raises(SuiteError) as error:
        suite_client.parse_document(f"calibrate-{sub}", b"", oversize=True)
    assert error.value.kind == "contract"


@pytest.mark.parametrize('check_id,capability,status', [
    ('models','full','missing'),('models','full','error'),('cuda','full','error'),
    ('ffmpeg','full','missing'),('ffprobe','files','missing'),('dovi_tool','dolby_vision','missing')])
@pytest.mark.parametrize('mode',['fast','accurate'])
def test_packaged_failed_full_preflight_refuses_before_worker_and_keeps_metadata(tmp_path,monkeypatch,check_id,capability,status,mode):
    import app_preflight
    root=tmp_path/'release/suite'
    root.mkdir(parents=True)
    (root.parent/'release.json').write_text('{}')
    checks=[dict(id=check_id,capability=capability,status=status,message='Required engine support is unavailable')]
    probes=[]
    def preflight(*args):
        probes.append(args)
        return checks
    monkeypatch.setattr(app_preflight,'preflight',preflight)
    pop=FakePopen(ok('score.json'))
    c=make_client(pop,root=str(root),environ={'VIDEOSUITE_OFFLINE':'1','VIDEOLYZER_DATA_ROOT':str(tmp_path/'data')})
    with pytest.raises(SuiteError,match='Full comparison is unavailable') as error:
        c.compare(['C:\\a.mkv','C:\\b.mkv'],str(tmp_path/'out'),mode=mode)
    assert error.value.kind=='unavailable'
    assert pop.calls==[] and pop.list_dir is None and not (tmp_path/'out').exists()
    assert len(probes)==1
    assert c.score(['C:\\a.mkv'])['schema']=='videosuite-score/1'
    assert len(pop.calls)==1 and len(probes)==1


def test_packaged_optional_magnet_failure_does_not_block_pixels(tmp_path,monkeypatch):
    import app_preflight
    root=tmp_path/'release/suite'
    root.mkdir(parents=True)
    (root.parent/'release.json').write_text('{}')
    monkeypatch.setattr(app_preflight,'preflight',lambda *a:[dict(id='magnet_runtime',capability='magnets',status='missing',message=app_preflight.MAGNET_UNAVAILABLE)])
    pop=FakePopen(ok('compare.json'))
    c=make_client(pop,root=str(root),environ={'VIDEOSUITE_OFFLINE':'1','VIDEOLYZER_DATA_ROOT':str(tmp_path/'data')})
    assert c.compare(['C:\\a.mkv','C:\\b.mkv'],str(tmp_path/'out')) is not None
    assert len(pop.calls)==1


@pytest.mark.parametrize("sub", ["plan", "build", "report"])
@pytest.mark.parametrize("reason", ["timeout", "cancel", "oversize"])
def test_calibration_process_cleanup(sub, reason, monkeypatch):
    monkeypatch.setattr(suite_client, "MAX_STDOUT_BYTES", 32)
    proc = FakeProc(stdout=b"x" * 33 if reason == "oversize" else b"", hang=True)
    pop, killed = FakePopen(proc), []
    client = make_client(pop, killer=lambda pid: (killed.append(pid), proc.die(1)),
                         clock=StepClock(100000) if reason == "timeout" else None)
    kwargs = {"cancel": lambda: True} if reason == "cancel" else {}
    if reason == "cancel":
        assert calibration_call(client, sub, **kwargs) is None
    else:
        with pytest.raises(SuiteError) as error:
            calibration_call(client, sub)
        assert error.value.kind == ("contract" if reason == "oversize" else "timeout")
    assert killed == [4242] and not pop.list_dir.exists()


@pytest.mark.parametrize("sub", _CAL_SUBS)
def test_calibration_failed_refusal_is_suite_error(sub):
    pop = FakePopen(FakeProc(rc=1, stderr=b"videosuite: damaged store; recovery refused\n"))
    with pytest.raises(SuiteError) as error:
        calibration_call(make_client(pop), sub)
    assert error.value.kind == "failed" and error.value.exit_code == 1
    assert "damaged store" in error.value.stderr_tail


def test_calibration_empty_paths_preview_latest_and_unknown_conditions():
    for sub in ("plan", "build"):
        pop = FakePopen(FakeProc(stdout=json.dumps(calibration_doc(sub)).encode()))
        client = make_client(pop)
        if sub == "plan":
            client.calibrate_plan([], [])
        else:
            client.calibrate_build([], "C:\\kit", [], [])
        # The CLI rejects an empty --list file; no sources means omit --list.
        assert "--list" not in pop.argv and pop.list_text is None
    pop = FakePopen(*(FakeProc(stdout=json.dumps(calibration_doc(s)).encode()) for s in ("apply", "status", "session")))
    client = make_client(pop)
    client.calibrate_apply(_CAL_KIT, preview=True)
    assert "--preview" in pop.argv
    client.calibrate_status()
    assert not any(a.startswith("--kit") for a in pop.argv)
    client.calibrate_session("C:\\kit", "V1", None, None)
    assert not any(a.startswith(("--audio-output", "--usb-fs")) for a in pop.argv)


@pytest.mark.parametrize("call", [
    lambda c: c.calibrate_plan("file", []),
    lambda c: c.calibrate_plan([], "builtin_usb"),
    lambda c: c.calibrate_plan([], ["bad\nplayer"]),
    lambda c: c.calibrate_build([], "C:\\bad&dir", [], []),
    lambda c: c.calibrate_build([], "C:\\kit", [], ["T01,T02"]),
    lambda c: c.calibrate_session("C:\\bad%dir", "V1", None, None),
    lambda c: c.calibrate_session("C:\\kit", "", None, None),
    lambda c: c.calibrate_session("C:\\kit", "V1", "bad", None),
    lambda c: c.calibrate_session("C:\\kit", "V1", None, "bad"),
    lambda c: c.calibrate_record("bad\nkit", "T01", "p", "yes"),
    lambda c: c.calibrate_record(_CAL_KIT, "T01", "p", "yes", "bad\x00note"),
    lambda c: c.calibrate_status("bad\nkit"),
    lambda c: c.calibrate_apply(_CAL_KIT, preview=False),
    lambda c: c.calibrate_apply(_CAL_KIT, preview=True, revision="rev"),
    lambda c: c.calibrate_apply(_CAL_KIT, preview=False, revision="rev", confirm_veto=["bad,id"]),
    lambda c: c.calibrate_report(_CAL_KIT, []),
])
def test_calibration_bad_arguments_never_spawn(call):
    with pytest.raises(SuiteArgumentError):
        call(make_client(_no_call))


@pytest.mark.parametrize("sub,path,value", [
    ("plan", "catalogue_version", True), ("plan", "players", [1]),
    ("plan", "tests.0.class", "other"), ("plan", "tests.0.manual", 1),
    ("plan", "tests.0.source.kind", "other"), ("plan", "tests.0.source.status", "other"),
    ("plan", "tests.0.source.est_bytes", True), ("plan", "tests.0.source.est_bytes", -1),
    ("build", "total_bytes", True), ("build", "total_bytes", -1),
    ("build", "descriptors.0.conditions", [1]), ("build", "descriptors.0.source.verification", "yes"),
    ("record", "summary.done", True), ("record", "summary.total", -1),
    ("record", "answer.match", "other"), ("record", "answer.older_tv_software", 1),
    ("status", "recovery_required", 1), ("status", "kit.catalogue_version", True),
    ("apply", "applied", 1), ("apply", "recovery_required", 1), ("apply", "changes.0.veto", 1),
    ("apply", "changes.0.action", "other"), ("apply", "conflicts.0.tests", [1]),
    ("report", "note", "quality ranking"), ("report", "kit_active", 1),
    ("report", "recovery_required", 1), ("report", "files.0.before.score", True),
    ("report", "files.0.after.verdict", "other"), ("report", "files.0.changed", 1),
])
def test_calibration_contract_rejects_invalid_values(sub, path, value):
    doc = calibration_doc(sub)
    row = doc
    parts = path.split(".")
    for part in parts[:-1]:
        row = row[int(part) if isinstance(row, list) else part]
    row[parts[-1]] = value
    with pytest.raises(SuiteError) as error:
        suite_client.parse_document(f"calibrate-{sub}", json.dumps(doc).encode())
    assert error.value.kind == "contract"


@pytest.mark.parametrize("sub", ["plan", "build", "report"])
def test_calibration_callback_exception_kills_and_cleans(sub):
    proc = FakeProc(hang=True)
    pop, killed = FakePopen(proc), []
    def cancel():
        raise RuntimeError("cancel callback failed")
    client = make_client(pop, killer=lambda pid: (killed.append(pid), proc.die(1)))
    with pytest.raises(RuntimeError, match="cancel callback failed"):
        calibration_call(client, sub, cancel=cancel)
    assert killed == [4242] and not pop.list_dir.exists()


@pytest.mark.parametrize("sub", _CAL_SUBS)
def test_calibration_accepts_nested_additions(sub):
    def extend(value):
        if isinstance(value, dict):
            for child in list(value.values()):
                extend(child)
            value["future"] = {"extra": None}
        elif isinstance(value, list):
            for child in value:
                extend(child)
    doc = calibration_doc(sub)
    # Prediction is a player-keyed mapping rather than a structured row.
    if sub == "plan":
        row = doc["tests"][0]
        for value in list(row.values()):
            if value is not row["prediction"]:
                extend(value)
        extend(row["prediction"]["builtin_usb"])
    else:
        extend(doc)
    assert suite_client.parse_document(f"calibrate-{sub}", json.dumps(doc).encode()) == doc
