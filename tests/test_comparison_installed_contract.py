"""Installed comparison QA entrypoint (plan V-U4), using an already-running QA-owned host.

Set PLAN4B_INSTALL_E2E=1, PLAN4B_QA_ROOT, PLAN4B_QA_INSTALL, PLAN4B_QA_EVIDENCE
and PLAN4B_QA_ORIGIN=http://127.0.0.1:<port>, as for tests/test_app_installed_contract.py.
Evidence uses the videosuite-installed-qa/1 schema documented by Comparizer's
tests/e2e/test_one_app_install.py and test_one_app_comparison.py. Its
``six_modes.runs`` maps quick_overall, quick_device, quick_both, full_overall,
full_device and full_both to JSON children captured from the installed app's
``GET /comparison/runs/{id}`` ({"run", "result"}). This entrypoint validates every
captured result with this repository's strict run contract and binds them to
the running installed build through the live same-origin /app/status. It never
launches a server or installer and never starts work. Without the opt-in the
installed test skips; a skip is not QA evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import pytest

import comparison_contracts

MODES = {f"{depth}_{results}": (depth, results)
         for depth in ("quick", "full") for results in ("overall", "device", "both")}
FIXTURES = Path(__file__).resolve().parent / "fixtures/comparison"
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


def checked_mode(detail, depth, results, suite=None):
    """Strict contract first, then the requested mode's stages and answers."""
    assert isinstance(detail, dict) and isinstance(detail.get("result"), dict), "run detail without a result"
    result = comparison_contracts.validate_run_result(detail["result"])
    assert detail.get("run", {}).get("run_id") == result["run_id"]
    assert (result["request"]["depth"], result["request"]["results"]) == (depth, results), "wrong mode"
    stages = set(result["completed_stages"])
    if depth == "quick":
        assert "quick" in stages and not stages & {"pixels", "rank"}, "Quick ran pixel stages"
    else:
        assert {"pixels", "rank"} <= stages, "Full skipped pixel/rank stages"
    assert (result["overall"] is not None) == (results in ("overall", "both"))
    assert (result["device"] is not None) == (results in ("device", "both"))
    if suite is not None:
        assert result["provenance"]["engine_tag"] == suite["engine_tag"]
        assert result["provenance"]["tool_version"] == suite["version"]
    return result


@installed
def test_installed_comparison_results_satisfy_the_run_contract():
    raw = os.environ.get("PLAN4B_QA_ROOT", "")
    assert raw and Path(raw).is_absolute(), "explicit absolute PLAN4B_QA_ROOT required"
    root = Path(raw)
    assert root.is_dir() and root.name.startswith("plan4b-qa-")
    install = owned(root, os.environ.get("PLAN4B_QA_INSTALL", ""))
    pointer = json.loads((install / "app/current.json").read_text(encoding="utf-8"))
    assert pointer["schema"] == "videosuite-app-pointer/1"
    release = owned(install, pointer["release"])
    manifest_file = release / "release.json"
    assert hashlib.sha256(manifest_file.read_bytes()).hexdigest() == pointer["manifest_sha256"]
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["build_channel"] == "qa", "production installs are excluded"
    evidence = json.loads(owned(root, os.environ.get("PLAN4B_QA_EVIDENCE", "")).read_text(encoding="utf-8"))
    assert evidence["schema"] == "videosuite-installed-qa/1"
    assert (evidence["build_id"], evidence["install_id"]) == (pointer["build_id"], pointer["install_id"])
    row = evidence.get("scenarios", {}).get("six_modes", {})
    assert row.get("status") == "PASS", "required installed scenario not passed: six_modes"
    log = owned(root, row.get("log", ""))
    assert log.is_file() and log.stat().st_size > 0, "missing measured log: six_modes"
    runs = row.get("runs", {})
    assert set(runs) == set(MODES), "all six Quick/Full x Overall/Device/Both runs are required"
    ids = set()
    for key, (depth, results) in MODES.items():
        detail = json.loads(owned(root, runs[key]).read_text(encoding="utf-8"))
        ids.add(checked_mode(detail, depth, results, manifest["suite"])["run_id"])
    assert len(ids) == 6, "each mode needs its own run"
    # Bind the captured evidence to the live installed build on its own origin.
    origin = qa_origin(os.environ.get("PLAN4B_QA_ORIGIN", ""))
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise AssertionError("installed QA request redirected")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(origin + "/app/status", headers={"Origin": origin}), timeout=10) as response:
        assert response.status == 200
        status = json.loads(response.read(1024 * 1024))
    assert status["build_id"] == manifest["build_id"] == pointer["build_id"]
    assert status["install_id"] == pointer["install_id"] and status["ready"] is True


# --- Always-run self-tests of the acceptance rules against real emitted fixtures ---

@pytest.mark.parametrize("name,depth,results", [("local_full_both.json", "full", "both"),
                                                ("local_quick_both.json", "quick", "both"),
                                                ("magnet_quick_overall.json", "quick", "overall")])
def test_comparison_gate_accepts_real_emitted_details(name, depth, results):
    detail = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    checked_mode(detail, depth, results)


def test_comparison_gate_refuses_a_mode_filed_under_another_name():
    detail = json.loads((FIXTURES / "local_quick_both.json").read_text(encoding="utf-8"))
    with pytest.raises(AssertionError, match="wrong mode"):
        checked_mode(detail, "full", "both")


def test_comparison_gate_refuses_quick_with_pixel_stages_or_contract_breaks():
    detail = json.loads((FIXTURES / "local_quick_both.json").read_text(encoding="utf-8"))
    detail["result"]["completed_stages"] = detail["result"]["completed_stages"] + ["pixels"]
    with pytest.raises(AssertionError, match="Quick ran pixel stages"):
        checked_mode(detail, "quick", "both")
    broken = json.loads((FIXTURES / "local_full_both.json").read_text(encoding="utf-8"))
    broken["result"]["measurement"] = "metadata"
    with pytest.raises(comparison_contracts.ContractError):
        checked_mode(broken, "full", "both")


def test_comparison_gate_refuses_wrong_engine_provenance():
    detail = json.loads((FIXTURES / "local_full_both.json").read_text(encoding="utf-8"))
    suite = dict(engine_tag="not-the-engine", version=detail["result"]["provenance"]["tool_version"])
    with pytest.raises(AssertionError):
        checked_mode(detail, "full", "both", suite)


def test_comparison_gate_refuses_unowned_evidence(tmp_path):
    for value in ("", "..", str(tmp_path.parent / "foreign.json"), str(tmp_path)):
        with pytest.raises(ValueError):
            owned(tmp_path, value)
