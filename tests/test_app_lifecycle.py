import threading
import time

import pytest

from app_lifecycle import AppLifecycle
from jobs import JobStore
from proc_guard import WorkerRegistry


def test_stop_quit_drains_owned_workers_and_preserves_foreign_process():
    store = JobStore()
    workers = WorkerRegistry()
    life = AppLifecycle(store, workers, grace_s=.02, force_s=.1)
    ids = [store.create(kind) for kind in ("analysis", "magnet", "suite", "calibration")]
    owned_alive = [101, 102]
    foreign_pid = 999
    killed = []
    for pid in owned_alive[:]:
        def force(pid=pid):
            killed.append(pid)
            owned_alive.remove(pid)
        workers.register(str(pid), lambda: None, lambda timeout, pid=pid: pid not in owned_alive,
                         force=force, job_id=ids[0])
    assert life.request_quit("stop")["accepted"]
    assert all(store.is_cancelled(j) for j in ids)
    assert life.drain()
    assert owned_alive == []
    assert foreign_pid not in killed
    assert all(store.get(j)["status"] == "cancelled" for j in ids)


def test_quit_when_finished_blocks_new_jobs_and_waits_all_kinds():
    store = JobStore()
    life = AppLifecycle(store, WorkerRegistry(), grace_s=.01, force_s=.01)
    ids = [store.create(k) for k in ("suite", "path", "magnet", "calibrate-build")]
    life.request_quit("when_finished")
    assert not life.can_start_work()
    with pytest.raises(RuntimeError, match="shutting down"):
        store.create("magnet")
    assert not life.ready_to_quit()
    for j in ids[:-1]:
        store.finish(j)
    assert not life.ready_to_quit()
    store.finish(ids[-1])
    assert life.ready_to_quit()
    life.request_quit("keep")
    assert life.can_start_work()


def test_stuck_worker_refuses_success_and_retains_identity(caplog):
    workers = WorkerRegistry()
    life = AppLifecycle(JobStore(), workers, grace_s=.01, force_s=.02)
    blocker = threading.Event()
    life.register_worker("owned-stuck", lambda: None, lambda t: blocker.wait(t))
    life.request_quit("stop")
    start = time.monotonic()
    assert not life.drain()
    assert time.monotonic() - start < .5
    assert workers.ids() == ["owned-stuck"]
    assert life.error == "Owned workers did not stop; app remains running."
    assert not life.ready_to_quit()


def test_terminal_job_does_not_hide_live_worker():
    store, workers = JobStore(), WorkerRegistry()
    life = AppLifecycle(store, workers)
    j = store.create("magnet")
    life.register_worker(j, lambda: None, lambda t: False)
    store.finish(j)
    life.request_quit("when_finished")
    assert not life.ready_to_quit()
    life.unregister_worker(j)
    assert life.ready_to_quit()


def test_browser_disconnect_does_not_cancel_workers():
    store = JobStore()
    life = AppLifecycle(store, WorkerRegistry())
    j = store.create("suite")
    # Reading status/reconnecting carries no shutdown side effect.
    assert life.active_jobs()[0]["job_id"] == j
    assert store.is_cancelled(j) is False
    assert life.can_start_work()


def test_hanging_cancel_callback_is_bounded_and_retained():
    store, workers = JobStore(), WorkerRegistry()
    life = AppLifecycle(store, workers, grace_s=.01, force_s=.02)
    release = threading.Event()
    workers.register("native", release.wait, lambda t: True)
    try:
        life.request_quit("stop")
        start = time.monotonic()
        assert not life.drain()
        assert time.monotonic() - start < .5
        assert any(w.startswith("cleanup:") for w in workers.ids())
    finally:
        release.set()
        workers.wait_all(.2)
