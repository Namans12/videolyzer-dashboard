"""Installed Dashboard QA entrypoint, using an already-running QA-owned host.

Set PLAN4B_INSTALL_E2E=1, PLAN4B_QA_ROOT, PLAN4B_QA_INSTALL,
PLAN4B_QA_EVIDENCE and PLAN4B_QA_ORIGIN=http://127.0.0.1:<port>.
Evidence uses the videosuite-installed-qa/1 schema documented by Comparizer's
tests/e2e/test_one_app_install.py. The browser scenarios below require real
browser logs/screenshots; this entrypoint validates their disposition and does
live same-origin status/dist checks. It never launches a server or installer.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest


BROWSER = ("all_three_tabs", "theme_keyboard_name_role_state", "light_dark_contrast",
           "desktop_390px", "no_console_errors", "calibration_scratch_build")
installed = pytest.mark.skipif(os.environ.get("PLAN4B_INSTALL_E2E") != "1",
                               reason="installed QA requires PLAN4B_INSTALL_E2E=1")


def owned(root, value):
    if not value:
        raise ValueError("explicit QA child path required")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            info = ancestor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("QA reparse path refused")
    path = path.resolve()
    if root.resolve() not in path.parents:
        raise ValueError("QA path escaped root")
    return path


def qa_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("explicit IPv4 loopback QA origin required")
    return value


def require_browser(root, evidence):
    for name in BROWSER:
        row = evidence.get("scenarios", {}).get(name, {})
        assert row.get("status") == "PASS", f"required browser scenario not passed: {name}"
        log = owned(root, row.get("log", ""))
        assert log.is_file() and log.stat().st_size > 0, f"missing measured browser log: {name}"
    console = evidence["scenarios"]["no_console_errors"]
    assert console.get("errors") == [], "browser console errors are not waived"


@installed
def test_installed_dashboard_same_origin_identity_and_browser_contract():
    raw = os.environ.get("PLAN4B_QA_ROOT", "")
    assert raw and Path(raw).is_absolute(), "explicit absolute PLAN4B_QA_ROOT required"
    root = Path(raw)
    assert root.is_dir() and root.name.startswith("plan4b-qa-")
    install = owned(root, os.environ.get("PLAN4B_QA_INSTALL", ""))
    pointer = json.loads((install / "app/current.json").read_text(encoding="utf-8"))
    assert pointer["schema"] == "videosuite-app-pointer/1"
    assert pointer["release"] == "app/releases/" + pointer["build_id"]
    release = owned(install, pointer["release"])
    manifest_file = release / "release.json"
    assert hashlib.sha256(manifest_file.read_bytes()).hexdigest() == pointer["manifest_sha256"]
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["build_channel"] == "qa", "production installs are excluded"
    evidence = json.loads(owned(root, os.environ.get("PLAN4B_QA_EVIDENCE", "")).read_text(encoding="utf-8"))
    assert evidence["schema"] == "videosuite-installed-qa/1"
    assert (evidence["build_id"], evidence["install_id"]) == (pointer["build_id"], pointer["install_id"])
    require_browser(root, evidence)
    origin = qa_origin(os.environ.get("PLAN4B_QA_ORIGIN", ""))
    # Disable ambient proxies and redirects; QA traffic must stay on this origin.
    from urllib.request import HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise AssertionError("installed QA request redirected")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def read(path):
        with opener.open(Request(origin + path, headers={"Origin": origin}), timeout=10) as response:
            assert response.status == 200
            return response.read(8 * 1024 * 1024)
    status = json.loads(read("/app/status"))
    assert status["schema"] == "videosuite-app-status/1"
    assert status["build_id"] == manifest["build_id"] == pointer["build_id"]
    assert status["install_id"] == pointer["install_id"] and status["origin"] == origin
    assert status["ready"] is True and status["checks"]
    assert all(check["status"] == "ok" for check in status["checks"])
    index = release / "dashboard/dist/index.html"
    for path in ("/", "/analyze", "/suite", "/calibration"):
        assert read(path) == index.read_bytes(), path
    for row in manifest["files"]:
        if row["path"].startswith("dashboard/dist/assets/"):
            data = read("/assets/" + row["path"].removeprefix("dashboard/dist/assets/"))
            assert len(data) == row["size"] and hashlib.sha256(data).hexdigest() == row["sha256"]


@pytest.mark.parametrize("value", ["", "http://localhost:1234", "https://127.0.0.1:1234",
    "http://192.168.1.2:1234", "http://127.0.0.1:1234/foreign", "http://user@127.0.0.1:1234"])
def test_installed_qa_refuses_foreign_origin(value):
    with pytest.raises(ValueError):
        qa_origin(value)


def test_installed_qa_accepts_only_explicit_loopback_origin():
    assert qa_origin("http://127.0.0.1:54321") == "http://127.0.0.1:54321"


def test_installed_qa_requires_every_browser_scenario(tmp_path):
    with pytest.raises(AssertionError, match="all_three_tabs"):
        require_browser(tmp_path, {})


def test_installed_qa_refuses_unowned_evidence(tmp_path):
    for value in ("", "..", str(tmp_path.parent / "foreign.json"), str(tmp_path)):
        with pytest.raises(ValueError):
            owned(tmp_path, value)


def test_installed_qa_does_not_waive_abort_console_errors(tmp_path):
    (tmp_path / "browser.log").write_text("measured browser output\n", encoding="utf-8")
    evidence = {"scenarios": {name: {"status": "PASS", "log": "browser.log"} for name in BROWSER}}
    evidence["scenarios"]["no_console_errors"]["errors"] = ["AbortError"]
    with pytest.raises(AssertionError, match="console errors"):
        require_browser(tmp_path, evidence)
    evidence["scenarios"]["no_console_errors"]["errors"] = []
    require_browser(tmp_path, evidence)
