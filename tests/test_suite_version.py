"""Version handshake branches, cache lifetime, and startup isolation."""
import threading

import pytest
from fastapi.testclient import TestClient

import main
import suite_routes
import suite_version
from suite_client import SuiteError, SuiteUnavailable


class VersionClient:
    def __init__(self, version="0.3.0", devices="0.3.0"):
        self.value = version
        self.device_value = devices
        self.calls = 0

    def version(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return {"tool_version": self.value, "capabilities": ["devices", "score_partial"],
                "engine_tag": "tag", "schemas": {"devices": "videosuite-devices/1"}}

    def devices(self):
        if isinstance(self.device_value, Exception):
            raise self.device_value
        return {"tool_version": self.device_value}


@pytest.mark.parametrize("value,expected", [
    ("0.3.0", (0, 3, 0)), ("0.10.0", (0, 10, 0)),
    ("0.4.0.dev1", (0, 4, 0)), (" 1.2.3", (1, 2, 3)), ("v1", None),
    (None, None), (123, None),
])
def test_parse_version(value, expected):
    assert suite_version.parse_version(value) == expected


@pytest.mark.parametrize("value,status,message", [
    ("0.3.0", "ok", "VideoSuite 0.3.0"),
    ("0.10.0", "ok", "VideoSuite 0.10.0"),
    ("0.4.0.dev1", "ok", "VideoSuite 0.4.0.dev1"),
    ("0.2.9", "too_old", "The installed VideoSuite is 0.2.9; the Dashboard needs 0.3.0 or later. "
     "Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard."),
    ("v1", "error", "VideoSuite reported an unreadable version 'v1'."),
    (None, "error", "VideoSuite reported an unreadable version None."),
])
def test_check_version(value, status, message):
    fake = VersionClient(value)
    result = suite_version.check(fake)
    assert result == {"status": status, "tool_version": value, "minimum": "0.3.0",
                      "capabilities": ["devices", "score_partial"], "engine_tag": "tag",
                      "schemas": {"devices": "videosuite-devices/1"}, "message": message,
                      "checked_at": result["checked_at"]}
    assert isinstance(result["checked_at"], (int, float))
    suite_version.check(fake)
    assert fake.calls == 2  # check itself never caches


def test_client_without_version():
    result = suite_version.check(object())
    assert result["status"] == "unchecked"
    assert result["message"] == "this client cannot report a version"


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("exc,status,message", [
    (SuiteUnavailable("secret"), "unavailable", suite_routes.UNAVAILABLE_DETAIL),
    (SuiteError("unavailable", "secret"), "unavailable", suite_routes.UNAVAILABLE_DETAIL),
    (SuiteError("contract", "secret"), "error", "VideoSuite did not answer the version check (contract)."),
    (SuiteError("timeout", "secret"), "error", "VideoSuite did not answer the version check (timeout)."),
    (RuntimeError("secret"), "error", "VideoSuite did not answer the version check (internal)."),
])
def test_check_errors_never_raise(fallback, exc, status, message):
    fake = VersionClient(SuiteError("usage", "old") if fallback else exc, exc)
    result = suite_version.check(fake)
    assert result["status"] == status
    assert result["message"] == message
    assert result["tool_version"] is None
    assert result["capabilities"] == []
    assert result["schemas"] == {}
    assert result["engine_tag"] is None


@pytest.mark.parametrize("value,status,message", [
    ("0.3.0", "ok", "VideoSuite 0.3.0 (no 'version' command; detected through 'devices')"),
    ("0.2.9", "too_old", "The installed VideoSuite is 0.2.9; the Dashboard needs 0.3.0 or later. "
     "Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard."),
    ("v1", "error", "VideoSuite reported an unreadable version 'v1'."),
    (SuiteError("usage", "old"), "too_old", "The installed VideoSuite is older than 0.3.0 "
     "(it has no 'devices' command). Reinstall the Comparizer (run the latest Video Comparator installer), "
     "then restart the Dashboard."),
])
def test_030_fallback(value, status, message):
    result = suite_version.check(VersionClient(SuiteError("usage", "old"), value))
    assert result["status"] == status
    assert result["message"] == message
    assert result["capabilities"] == ([] if isinstance(value, Exception) else ["devices"])
    assert result["schemas"] == {}
    assert result["engine_tag"] is None
    assert result["tool_version"] == (None if isinstance(value, Exception) else value)


@pytest.mark.parametrize("value,ttl", [
    ("0.3.0", 300), ("0.2.9", 300), (SuiteUnavailable("missing"), 10),
    ("v1", 10),
])
def test_cache_ttls_refresh_and_reset(monkeypatch, value, ttl):
    now = [1000.0]
    monkeypatch.setattr(suite_version, "_now", lambda: now[0])
    fake = VersionClient(value)
    assert suite_version.cached() is None
    first = suite_version.ensure(fake)
    now[0] += 5 if ttl == 10 else 299
    assert suite_version.ensure(fake) == first
    assert fake.calls == 1
    now[0] = 1000 + ttl  # expiration boundary is strict
    assert suite_version.cached() == first  # cached() returns even expired results
    suite_version.ensure(fake)
    assert fake.calls == 2
    now[0] += 1
    suite_version.ensure(fake, refresh=True)
    assert fake.calls == 3
    suite_version.reset()
    assert suite_version.cached() is None


def test_unchecked_cache_has_short_ttl(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(suite_version, "_now", lambda: now[0])
    first = suite_version.ensure(object())
    now[0] = 5
    assert suite_version.ensure(object()) == first
    now[0] = 11
    assert suite_version.ensure(object())["checked_at"] == 11


def test_concurrent_first_checks_run_once():
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    fake = VersionClient()
    original = fake.version
    def slow():
        started.set()
        assert release.wait(5)
        return original()
    fake.version = slow
    results = []
    def second():
        second_started.set()
        results.append(suite_version.ensure(fake))
    threads = [threading.Thread(target=lambda: results.append(suite_version.ensure(fake))),
               threading.Thread(target=second)]
    try:
        threads[0].start()
        assert started.wait(5)
        threads[1].start()
        assert second_started.wait(5)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert results[0] == results[1]
    assert fake.calls == 1


@pytest.mark.parametrize("value,warning", [("0.3.0", False), ("0.2.9", True),
                                         (SuiteUnavailable("missing"), True)])
def test_background_check_stores_result_and_warns(monkeypatch, value, warning):
    warnings = []
    monkeypatch.setattr(suite_version.logger, "warning", warnings.append)
    def factory():
        if isinstance(value, Exception):
            raise value
        return VersionClient(value)
    thread = suite_version.start_background_check(factory)
    assert thread.daemon
    assert thread.name == "suite-version-check"
    thread.join(5)
    assert not thread.is_alive()
    result = suite_version.cached()
    assert result["status"] == ("unavailable" if isinstance(value, Exception) else
                                "ok" if value == "0.3.0" else "too_old")
    assert warnings == ([result["message"]] if warning else [])


@pytest.mark.parametrize("exc,kind", [(RuntimeError("secret"), "internal"),
                                     (SuiteError("contract", "secret"), "contract"),
                                     (SuiteError("usage", "secret"), "usage"),
                                     (SuiteError("unavailable", "secret"), "unavailable")])
def test_background_discovery_failures_are_cached_and_logged(monkeypatch, exc, kind):
    warnings = []
    monkeypatch.setattr(suite_version.logger, "warning", warnings.append)
    def factory():
        raise exc
    thread = suite_version.start_background_check(factory)
    thread.join(5)
    assert not thread.is_alive()
    result = suite_version.cached()
    assert result is not None
    assert result["status"] == ("unavailable" if kind == "unavailable" else "error")
    assert result["message"] == (suite_routes.UNAVAILABLE_DETAIL if kind == "unavailable" else
                                  f"VideoSuite did not answer the version check ({kind}).")
    assert warnings == [result["message"]]


@pytest.mark.parametrize("doc", [None, [], object(), {"tool_version": object()},
                                 {"tool_version": "0.4.0", "capabilities": [object()]},
                                 {"tool_version": "0.4.0", "schemas": {object(): "v1"}},
                                 {"tool_version": "0.4.0", "engine_tag": float("nan")}])
def test_check_malformed_metadata_is_json_safe(doc):
    import json
    fake = VersionClient()
    fake.version = lambda: doc
    result = suite_version.check(fake)
    assert result["status"] == "error"
    assert result["message"] == "VideoSuite did not answer the version check (contract)."
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("enabled", ["0", "1", None])
def test_lifespan_startup_switch(monkeypatch, isolated_uploads, enabled):
    calls = []
    monkeypatch.setattr(suite_version, "start_background_check", lambda: calls.append(True))
    if enabled is None:
        monkeypatch.delenv(suite_version.ENV_STARTUP_CHECK)
    else:
        monkeypatch.setenv(suite_version.ENV_STARTUP_CHECK, enabled)
    with TestClient(main.app, base_url="http://localhost"):
        assert calls == ([] if enabled == "0" else [True])


def test_readme_documents_version_and_startup_switch():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "/suite/version" in text
    assert "VIDEOLYZER_SUITE_STARTUP_CHECK" in text
