"""Shared pytest fixtures for the Dashboard backend tests.

Rules (plan 2, "Test-harness rules"):

* ``client`` uses ``base_url="http://localhost"`` (never the default host
  ``testserver``, which the request guard's Host rule rejects).
* ``isolated_uploads`` points ``main.UPLOAD_DIR`` at ``tmp_path / "uploads"`` and
  creates it BEFORE any ``with TestClient(...)`` block, because ``lifespan``
  deletes every file in ``UPLOAD_DIR`` on startup. ``client`` depends on it.
* ``clean_state`` clears the in-memory job store (later tasks extend it with the
  ``/suite/devices`` cache and the port allocator, and Plan 4B U5 with the unified run store).
* ``isolated_run_store`` points the unified run store's data root at ``tmp_path / "app"`` so no test can write
  run folders into a real app-data directory.
* TestClient runs BackgroundTasks to completion before the response returns, so
  HTTP tests assert final job states only.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture(autouse=True)
def guard_taskkill(monkeypatch):
    import suite_client
    original = suite_client.taskkill_tree
    def forbidden(*args, **kwargs):
        pytest.fail("real taskkill called in a test")
    monkeypatch.setattr(suite_client, "taskkill_tree", forbidden)
    return original


@pytest.fixture(autouse=True)
def disable_suite_startup_check(monkeypatch):
    monkeypatch.setenv("VIDEOLYZER_SUITE_STARTUP_CHECK", "0")


@pytest.fixture(autouse=True)
def isolated_uploads(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(main, "UPLOAD_DIR", str(uploads))
    # The disk guard compares real free space with a 20 GB floor; do not let the
    # developer's disk state decide test outcomes.
    monkeypatch.setattr(main, "MIN_FREE_BYTES", 0)
    return uploads


@pytest.fixture(autouse=True)
def isolated_run_store(tmp_path):
    import comparison_runs
    comparison_runs.store.configure(data_root=tmp_path / "app")
    return comparison_runs.store


@pytest.fixture(autouse=True)
def clean_state():
    # The job store has no timers (retention is purged inside ``create``), so
    # there is nothing to patch: just start and end every test empty.
    import comparison_runs
    import magnet
    import suite_client
    import suite_routes
    import suite_version
    import proc_guard

    def reset() -> None:
        with proc_guard.workers.lock:
            proc_guard.workers._workers.clear()
            proc_guard.workers.stopping = False
        main.lifecycle.mode = "keep"
        main.lifecycle.error = None
        main.jobs.accept_work(True)
        with suite_client._LIVE_LOCK:
            suite_client._LIVE.clear()
        main._jobs.clear()
        magnet.port_allocator.reset()
        magnet.set_partial_scorer(None)
        suite_routes.reset_state()          # /suite/devices cache and job slots
        suite_client.reset_suite_client()
        suite_version.reset()
        comparison_runs.store.reset()           # unified runs; keeps the per-test data root

    reset()
    yield reset
    reset()
    main.app.dependency_overrides.clear()


@pytest.fixture
def client(isolated_uploads):
    # isolated_uploads is requested explicitly so the temp dir exists before the
    # lifespan startup scans (and empties) UPLOAD_DIR.
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c


@pytest.fixture
def upload_dir(isolated_uploads):
    return str(isolated_uploads)


def listdir(path) -> list[str]:
    return sorted(os.listdir(path))
