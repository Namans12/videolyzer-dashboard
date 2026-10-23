"""Unit tests for jobs.JobStore (locked job store with honest terminal states)."""
from __future__ import annotations

import threading

import pytest

import jobs
from jobs import JobStore, TERMINAL


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_terminal_constant():
    assert TERMINAL == ("done", "error", "cancelled")


def test_create_returns_id_and_base_shape():
    s = JobStore()
    jid = s.create("batch", total=3, progress="0/3")
    job = s.get(jid)
    assert job["status"] == "running"
    assert job["kind"] == "batch"
    assert job["total"] == 3 and job["progress"] == "0/3"
    assert job["results"] == [] and job["events"] == []
    assert job["error"] is None and job["error_kind"] is None
    assert job["current"] == ""
    assert jid in s and len(s) == 1 and s.ids() == [jid]
    assert list(s) == [jid]


def test_create_extra_fields_and_unique_ids():
    s = JobStore()
    a = s.create("magnet", magnet_files=[], magnet_torrent=None)
    b = s.create("magnet")
    assert a != b
    assert s.get(a)["magnet_files"] == [] and s.get(a)["magnet_torrent"] is None
    assert "magnet_files" not in s.get(b)


def test_get_unknown_is_none():
    s = JobStore()
    assert s.get("nope") is None
    assert "nope" not in s
    assert s.is_cancelled("nope") is False
    assert s.events_since("nope", 0) is None


def test_get_returns_a_deep_copy():
    s = JobStore()
    jid = s.create("batch")
    s.update(jid, results=[{"file": "a.mkv", "nested": {"x": 1}}])
    s.append_event(jid, "hello")
    copy = s.get(jid)
    copy["status"] = "error"
    copy["results"][0]["nested"]["x"] = 99
    copy["events"].append({"msg": "forged", "ts": 0.0})
    again = s.get(jid)
    assert again["status"] == "running"
    assert again["results"][0]["nested"]["x"] == 1
    assert [e["msg"] for e in again["events"]] == ["hello"]


def test_update_and_unknown_id_is_a_noop():
    s = JobStore()
    jid = s.create("path")
    assert s.update(jid, current="a.mkv", progress="1/2") is True
    job = s.get(jid)
    assert job["current"] == "a.mkv" and job["progress"] == "1/2"
    assert s.update("nope", current="x") is False
    assert s.append_event("nope", "x") is False


def test_events_since_and_event_shape():
    s = JobStore()
    jid = s.create("path")
    for i in range(3):
        s.append_event(jid, f"m{i}")
    evs = s.events_since(jid, 0)
    assert [e["msg"] for e in evs] == ["m0", "m1", "m2"]
    assert all(set(e) == {"msg", "ts"} and isinstance(e["ts"], float) for e in evs)
    assert [e["msg"] for e in s.events_since(jid, 2)] == ["m2"]
    assert s.events_since(jid, 3) == []
    assert s.events_since(jid, 99) == []
    evs[0]["msg"] = "tampered"
    assert s.events_since(jid, 0)[0]["msg"] == "m0"       # copies


def test_events_and_status_reads_status_first_so_no_event_is_lost():
    s = JobStore()
    jid = s.create("path")
    s.append_event(jid, "a")
    s.finish(jid)
    events, status = s.events_and_status(jid, 0)
    assert status == "done" and [e["msg"] for e in events] == ["a"]
    assert s.events_and_status("nope", 0) is None


def test_finish_sets_done_and_results():
    s = JobStore()
    jid = s.create("batch")
    assert s.finish(jid, results=[{"file": "a"}]) == "done"
    job = s.get(jid)
    assert job["status"] == "done" and job["results"] == [{"file": "a"}]
    assert job["error"] is None


def test_finish_without_results_keeps_existing_results():
    s = JobStore()
    jid = s.create("batch")
    s.update(jid, results=[{"file": "kept"}])
    s.finish(jid)
    assert s.get(jid)["results"] == [{"file": "kept"}]


def test_fail_sets_error_and_kind():
    s = JobStore()
    jid = s.create("batch")
    assert s.fail(jid, "boom", error_kind="timeout") == "error"
    job = s.get(jid)
    assert job["status"] == "error" and job["error"] == "boom"
    assert job["error_kind"] == "timeout"


def test_fail_coerces_error_to_text():
    s = JobStore()
    jid = s.create("batch")
    s.fail(jid, ValueError("bad"))
    assert s.get(jid)["error"] == "bad"


def test_terminal_state_is_final():
    s = JobStore()
    jid = s.create("batch")
    s.fail(jid, "first")
    assert s.finish(jid, results=[{"x": 1}]) == "error"
    assert s.fail(jid, "second") == "error"
    job = s.get(jid)
    assert job["status"] == "error" and job["error"] == "first" and job["results"] == []


def test_finish_and_fail_on_unknown_id_return_none():
    s = JobStore()
    assert s.finish("nope") is None
    assert s.fail("nope", "x") is None


def test_cancel_flag():
    s = JobStore()
    jid = s.create("batch")
    assert s.is_cancelled(jid) is False
    assert s.request_cancel(jid) is True
    assert s.is_cancelled(jid) is True
    assert s.request_cancel("nope") is False


def test_cancel_after_finish_returns_true_but_does_not_change_the_job():
    s = JobStore()
    jid = s.create("batch")
    s.finish(jid, results=[{"a": 1}])
    assert s.request_cancel(jid) is True
    job = s.get(jid)
    assert job["status"] == "done" and job["error"] is None
    assert s.is_cancelled(jid) is False


def test_finish_with_cancel_flag_ends_cancelled_and_keeps_partial_results():
    s = JobStore()
    jid = s.create("batch")
    s.update(jid, per_magnet=[{"index": 0}])
    s.request_cancel(jid)
    assert s.finish(jid, results=[{"file": "partial"}]) == "cancelled"
    job = s.get(jid)
    assert job["status"] == "cancelled"
    assert job["error"] == "cancelled"
    assert job["results"] == [{"file": "partial"}]
    assert job["per_magnet"] == [{"index": 0}]


def test_fail_with_cancel_flag_ends_cancelled():
    s = JobStore()
    jid = s.create("magnet")
    s.request_cancel(jid)
    assert s.fail(jid, "Cancelled", error_kind="internal") == "cancelled"
    job = s.get(jid)
    assert job["status"] == "cancelled" and job["error"] == "cancelled"
    assert job["error_kind"] is None


def test_mutate_runs_under_the_lock_on_the_live_job():
    s = JobStore()
    jid = s.create("compare", per_magnet=[{"status": "pending", "events": []}])
    s.mutate(jid, lambda job: job["per_magnet"][0].update(status="running"))
    assert s.get(jid)["per_magnet"][0]["status"] == "running"
    assert s.mutate("nope", lambda job: None) is False


def test_clear():
    s = JobStore()
    s.create("a")
    s.request_cancel(s.create("b"))
    s.clear()
    assert len(s) == 0 and s.ids() == []


# ── retention ────────────────────────────────────────────────────────────────

def _finished(s: JobStore, n: int, clock: FakeClock | None = None) -> list[str]:
    # create everything first (create purges), then finish in order
    ids = [s.create("batch") for _ in range(n)]
    for i, jid in enumerate(ids):
        s.finish(jid) if i % 3 else s.fail(jid, "e")
        if clock:
            clock.now += 1
    return ids


def test_retention_keeps_the_last_20_finished_jobs_purged_inside_create():
    clock = FakeClock()
    s = JobStore(keep_finished=20, grace_s=0, clock=clock)
    ids = _finished(s, 25, clock)
    assert len(s) == 25                       # nothing is purged until the next create
    new = s.create("batch")
    assert len(s) == 21                       # 20 finished + the new running one
    assert s.ids() == ids[5:] + [new]
    assert s.get(ids[0]) is None and s.get(ids[5]) is not None


def test_retention_never_purges_running_jobs():
    s = JobStore(keep_finished=2, grace_s=0)
    running = [s.create("batch") for _ in range(5)]
    _finished(s, 6)
    s.create("batch")
    assert all(r in s for r in running)
    assert len(s) == 5 + 2 + 1


def test_retention_counts_cancelled_as_finished():
    s = JobStore(keep_finished=1, grace_s=0)
    a = s.create("x")
    s.request_cancel(a)
    s.finish(a)
    b = s.create("x")
    s.finish(b)
    s.create("x")
    assert a not in s and b in s


def test_just_finished_jobs_survive_the_grace_period_even_beyond_20():
    clock = FakeClock()
    s = JobStore(keep_finished=20, grace_s=30.0, clock=clock)
    ids = _finished(s, 25)                    # all finish at the same instant
    s.create("batch")
    assert all(i in s for i in ids)           # a poller can still read the final state
    clock.now += 31
    s.create("batch")
    assert all(i not in s for i in ids[:5]) and all(i in s for i in ids[5:])


def test_default_retention_values():
    s = JobStore()
    assert s.keep_finished == 20 and s.grace_s == 30.0


# ── concurrency ──────────────────────────────────────────────────────────────

def test_eight_threads_updating_concurrently_keep_counts_exact():
    s = JobStore()
    jid = s.create("batch", per_magnet=[{"n": 0} for _ in range(8)], counter=0)
    per_thread = 100
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def work(idx: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                s.append_event(jid, f"t{idx}-{i}")
                s.mutate(jid, lambda j: j.__setitem__("counter", j["counter"] + 1))
                s.mutate(jid, lambda j: j["per_magnet"][idx].__setitem__(
                    "n", j["per_magnet"][idx]["n"] + 1))
                if i % 10 == 0:
                    s.get(jid)                 # readers run concurrently with writers
        except BaseException as exc:           # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    job = s.get(jid)
    assert len(job["events"]) == 8 * per_thread
    assert job["counter"] == 8 * per_thread
    assert [m["n"] for m in job["per_magnet"]] == [per_thread] * 8


def test_concurrent_creates_get_distinct_ids():
    s = JobStore(keep_finished=1000)
    out: list[str] = []
    lock = threading.Lock()

    def work():
        mine = [s.create("batch") for _ in range(100)]
        with lock:
            out.extend(mine)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 800 == len(s)


def test_module_exports_a_shared_store():
    assert isinstance(jobs.jobs, JobStore)


def test_event_wall_clock_is_injectable_for_deterministic_envelopes():
    store = JobStore()
    store._wall = lambda: 1700000000.0
    job_id = store.create("comparison_run")
    store.append_event(job_id, "Planning")
    assert store.get(job_id)["events"] == [{"msg": "Planning", "ts": 1700000000.0}]
