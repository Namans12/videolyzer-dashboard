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
    doc = fixture_bytes("devices.json").replace(b'"tool_version": "0.2.0"', b'"tool_version": "0.2.0", "x": NaN')
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


def test_default_killer_runs_taskkill_tree_without_a_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(suite_client.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    suite_client.taskkill_tree(1234)
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
