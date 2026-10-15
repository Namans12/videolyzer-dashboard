import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import proc_guard


def test_null_job_and_singleton():
    assert proc_guard.NULL_JOB.assign(object()) is False
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda _: proc_guard.default_job(), range(32)))
    assert all(job is jobs[0] for job in jobs)


def test_creation_failure_cached_and_logged_once(monkeypatch, caplog):
    calls = []
    def fail():
        calls.append(True)
        raise OSError("job unavailable")
    monkeypatch.setattr(proc_guard, "_job", None)
    monkeypatch.setattr(proc_guard, "KillOnCloseJob", fail)
    monkeypatch.setattr(proc_guard.atexit, "register", lambda fn: None)
    assert proc_guard.default_job() is proc_guard.NULL_JOB
    assert proc_guard.default_job() is proc_guard.NULL_JOB
    assert calls == [True]
    assert caplog.text.count("VideoSuite Job Object unavailable") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_missing_handle_is_not_assigned():
    job = proc_guard.KillOnCloseJob()
    try:
        assert job.assign(object()) is False
    finally:
        job.close()
        job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_close_kills_child_and_grandchild(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    # Handshake ensures assignment precedes the grandchild spawn.
    ready = tmp_path / "assigned"
    code = ("import pathlib,subprocess,sys,time;"
            f"ready=pathlib.Path({str(ready)!r});"
            "exec('while not ready.exists(): time.sleep(0.01)');"
            "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(g.pid));time.sleep(120)")
    job = proc_guard.KillOnCloseJob()
    child = subprocess.Popen([sys.executable, "-c", code],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def alive(pid):
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, timeout=5, check=True)
        return any(str(pid) in line.split() for line in result.stdout.splitlines())
    try:
        assert job.assign(child) is True
        ready.touch()
        deadline = time.monotonic() + 5
        while (not pidfile.exists() or not pidfile.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        grandchild = int(pidfile.read_text())
        assert alive(child.pid) and alive(grandchild)
        job.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not alive(child.pid) and not alive(grandchild):
                break
            time.sleep(0.05)
        assert not alive(child.pid) and not alive(grandchild)
    finally:
        job.close()
        if child.poll() is None:
            child.kill()
        child.wait(5)
