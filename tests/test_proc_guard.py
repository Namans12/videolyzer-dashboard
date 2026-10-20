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


def test_process_creation_identity_is_not_pid_only():
    assert proc_guard.process_created(os.getpid()).isdigit()
    assert proc_guard.process_created(0) is None


def test_supervised_job_assignment_failure_is_explicit(monkeypatch, caplog):
    job = proc_guard.KillOnCloseJob()
    try:
        assert job.assign(object()) is False
    finally:
        job.close()
    assert "crash cleanup is best effort" in caplog.text


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


def test_media_tool_is_hidden_and_owned_even_when_job_assignment_fails(monkeypatch, caplog):
    import io
    seen = []
    class Proc:
        pid = 42424
        _handle = 123
        returncode = None
        stdout, stderr = io.StringIO(), io.StringIO()
        def poll(self): return self.returncode
        def communicate(self, timeout):
            assert any(entry[3] == "analysis-job" for entry in proc_guard.workers.snapshot().values())
            self.returncode = 0
            return "facts", ""
        def wait(self, timeout): return self.returncode
    class FailedJob:
        def assign(self, proc):
            proc_guard.logger.warning("Job Object assignment failed; crash cleanup is best effort")
            return False
        def close(self): pass
    monkeypatch.setattr(proc_guard, "KillOnCloseJob", FailedJob)
    monkeypatch.setattr(proc_guard.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)) or Proc())
    with proc_guard.owner("analysis-job"):
        assert proc_guard.run_tool(["ffprobe", "synthetic.mkv"], timeout=1).stdout == "facts"
    assert seen[0][1]["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert seen[0][1]["shell"] is False
    assert proc_guard.workers.ids() == []
    assert "crash cleanup is best effort" in caplog.text


def test_real_owned_process_waits_for_entire_job_tree(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    owned = proc_guard.OwnedProcess(child)
    try:
        assert owned.job is not None
        assert owned.job.active_count() == 1
        owned.force()
        assert owned.wait(5)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)
        if owned.job:
            owned.job.close()


def test_stop_keeps_unregistered_synthetic_process_alive():
    from app_lifecycle import AppLifecycle
    from jobs import JobStore
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                           creationflags=subprocess.CREATE_NO_WINDOW)
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    owned = proc_guard.OwnedProcess(own)
    workers = proc_guard.WorkerRegistry()
    life = AppLifecycle(JobStore(), workers, grace_s=.2, force_s=1)
    workers.register("owned", owned.cancel, owned.wait, force=owned.force)
    try:
        life.request_quit("stop")
        assert life.drain()
        assert own.poll() is not None
        assert foreign.poll() is None
    finally:
        for proc in (own, foreign):
            if proc.poll() is None:
                proc.kill()
            proc.wait(5)
        if owned.job:
            owned.job.close()
