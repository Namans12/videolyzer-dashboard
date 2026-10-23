"""Unified comparison contracts (Plan 4B U4): strict request/snapshot/result validation and capability gates.

Nested suite documents are the REAL emitted fixtures in ``tests/fixtures/suite4b`` (produced by
``scripts/make_suite_fixtures.py --plan4b`` from the actual VideoSuite CLI emitters), never hand-made near-matches.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import comparison_contracts as cc
import suite_client

ROOT = Path(__file__).resolve().parents[1]
FIX4B = ROOT / "tests" / "fixtures" / "suite4b"
FIXTURE_COMMANDS = {
    "version": "version", "compare_plan": "compare-plan", "compare_all": "compare", "rank_overall": "rank",
    "rank_device": "rank", "quick_overall": "quick", "quick_device": "quick", "quick_both": "quick",
    "quick_partial": "quick", "quick_just_player_alternatives": "quick",
}
SUITE_PYTHON = Path(r"C:\Users\naman\AppData\Local\Programs\Python\Python310\python.exe")
COMPARIZER = Path(r"D:\Video Comparizer")


def fixture(name: str) -> dict:
    return json.loads((FIX4B / (name + ".json")).read_text(encoding="utf-8"))


# ── requests ────────────────────────────────────────────────────────────────────

def request(**over) -> dict:
    base = {"input_kind": "files", "results": "both", "depth": "full", "paths": ["D:\\Movies\\A & B 100%.mkv"],
            "device": "sony_bravia_8_ii", "primary_player": "just_player", "player": "all",
            "include_alternatives": True, "fs": None, "lang": "eng", "audio_output": None}
    base.update(over)
    return base


def test_run_request_accepts_the_six_modes_and_keeps_primary_and_evaluation_separate():
    for results in ("overall", "device", "both"):
        for depth in ("quick", "full"):
            device_fields = ({"device": None, "primary_player": None, "player": None} if results == "overall" else {})
            out = cc.validate_run_request(request(results=results, depth=depth, **device_fields))
            assert out["results"] == results and out["depth"] == depth
    out = cc.validate_run_request(request())
    assert (out["primary_player"], out["player"], out["include_alternatives"]) == ("just_player", "all", True)
    single = cc.validate_run_request(request(include_alternatives=False, player="just_player"))
    assert (single["primary_player"], single["player"]) == ("just_player", "just_player")
    magnets = cc.validate_run_request(request(input_kind="magnets", paths=None, depth="quick",
                                              magnets=[{"uri": "magnet:?xt=urn:btih:" + h * 40, "file_index": None}
                                                       for h in 'ab']))
    assert magnets["magnets"][0]["file_index"] is None and "paths" not in magnets


@pytest.mark.parametrize('count', [0, 1, 9])
def test_magnet_comparison_requires_two_to_eight_links(count):
    with pytest.raises(cc.ContractError):
        cc.validate_run_request(request(input_kind='magnets', paths=None, depth='quick',
            magnets=[{'uri': 'magnet:?xt=urn:btih:' + str(i) * 40, 'file_index': None} for i in range(count)]))


@pytest.mark.parametrize("change", [
    {"extra": 1},                                                          # extra keys forbidden
    {"results": "best"}, {"depth": "fast"}, {"input_kind": "folder"},
    {"results": "overall"},                                                # Overall with device/primary/player
    {"results": "overall", "device": None, "primary_player": None, "player": "all"},
    {"device": "unknown_tv"}, {"device": None},                           # Device/Both needs a supported device
    {"primary_player": None}, {"primary_player": "all"},                   # explicit real primary
    {"player": "kodi"},                                                    # alternatives require player=all
    {"include_alternatives": False},                                       # single requires player=primary
    {"paths": []}, {"paths": None}, {"paths": ["ok", 3]},
    {"magnets": [{"uri": "magnet:?xt=urn:btih:" + "a" * 40, "file_index": 0}]},  # both inputs
    {"fs": "hfs"}, {"audio_output": "headphones"}, {"lang": "english!"},
    {"include_alternatives": "yes"}, {"device": 5},
])
def test_run_request_rejects_contradictions_without_echoing_values(change):
    raw = request(**change)
    with pytest.raises(cc.ContractError) as error:
        cc.validate_run_request(raw)
    assert "A & B 100%" not in str(error.value) and "unknown_tv" not in str(error.value)


def test_run_request_rejects_non_objects():
    for raw in (None, [], "x", 1):
        with pytest.raises(cc.ContractError):
            cc.validate_run_request(raw)


# ── snapshots ───────────────────────────────────────────────────────────────────

def internal_snapshot(tmp_path: Path, **over) -> dict:
    plan = fixture("compare_plan")
    snap = {
        "run_id": "run-0001", "plan_revision": "rev-0001", "request": cc.validate_run_request(request()),
        "stage": "awaiting_confirmation", "job_id": "job-0001",
        "candidates": [dict(candidate_id="c%d" % i, kind="file", path=c["path"], uri=None, file_index=None,
                            status=c["status"], record_id=c["id"], reason=c["reason"], size=c["size"],
                            mtime_ns=c["mtime_ns"]) for i, c in enumerate(plan["candidates"])],
        "conditions": {"device": "sony_bravia_8_ii", "primary_player": "just_player", "player": "all",
                       "evaluated_players": ["builtin_usb", "kodi", "just_player"], "fs": None, "lang": "eng",
                       "audio_output": None, "settings_digest": "0" * 64},
        "versions": {"tool_version": "0.7.0", "engine_tag": "562ab353", "facts_version": 2,
                     "rules_versions": {"sony_bravia_8_ii": 3}},
        "records_digest": plan["records_digest"], "reports_dir": str(tmp_path / "reports"),
        "estimate": plan["estimate"], "created_at": 1700000000.0,
    }
    snap.update(over)
    return snap


def test_internal_snapshot_requires_reports_dir_and_public_projection_omits_it(tmp_path):
    snap = cc.validate_internal_snapshot(internal_snapshot(tmp_path))
    assert snap["reports_dir"] == str(tmp_path / "reports")
    missing = internal_snapshot(tmp_path)
    del missing["reports_dir"]
    with pytest.raises(cc.ContractError):
        cc.validate_internal_snapshot(missing)
    with pytest.raises(cc.ContractError):
        cc.validate_internal_snapshot(internal_snapshot(tmp_path, reports_dir="relative\\reports"))
    public = cc.to_public_snapshot(snap)
    assert "reports_dir" not in public and str(tmp_path) not in json.dumps(public)
    assert set(public) == set(snap) - {"reports_dir"}
    assert cc.validate_public_snapshot(public) == public
    leaked = dict(public, reports_dir=str(tmp_path))
    with pytest.raises(cc.ContractError):
        cc.validate_public_snapshot(leaked)
    # The projection is explicit: an unexpected internal key never reaches the public shape.
    with pytest.raises(cc.ContractError):
        cc.to_public_snapshot(dict(snap, secret_root=str(tmp_path)))


@pytest.mark.parametrize("change", [{"stage": "running"}, {"created_at": "now"}, {"estimate": {"pairs": -1, "minutes": 0}},
                                    {"conditions": {"device": None}}, {"versions": {"tool_version": 7}},
                                    {"records_digest": "xyz"}])
def test_snapshot_nested_values_are_strict(tmp_path, change):
    with pytest.raises(cc.ContractError):
        cc.validate_internal_snapshot(internal_snapshot(tmp_path, **change))


# ── results ─────────────────────────────────────────────────────────────────────

def run_result(tmp_path: Path, *, depth="quick", results="both") -> dict:
    req = cc.validate_run_request(request(depth=depth, results=results))
    snap = cc.to_public_snapshot(cc.validate_internal_snapshot(internal_snapshot(tmp_path, request=req, stage="done")))
    if depth == "quick":
        quick = fixture("quick_both" if results == "both" else "quick_device")
        overall = quick["overall"] if results == "both" else None
        device = quick if results != "overall" else None
        facts = quick["records"]
        coverage = {"submitted": 3, "readable": 3, "expected_pairs": 0, "completed_pairs": 0, "missing_pairs": 0,
                    "complete": True}
    else:
        overall = fixture("rank_overall") if results != "device" else None
        device = fixture("rank_device") if results != "overall" else None
        facts = fixture("compare_plan")["records"]
        coverage = {"submitted": 3, "readable": 3, "expected_pairs": 3, "completed_pairs": 3, "missing_pairs": 0,
                    "complete": True}
    return {"schema": "videosuite-comparison-run/1", "run_id": snap["run_id"], "request": req, "snapshot": snap,
            "measurement": "metadata" if depth == "quick" else "pixels",
            "completed_stages": ["facts", "quick"] if depth == "quick" else ["facts", "pixels", "rank"],
            "coverage": coverage, "facts": facts, "overall": overall, "device": device, "failures": [],
            "reports": [{"report_id": "r1", "name": "A_vs_B.json", "kind": "json",
                         "url": "/comparison/runs/run-0001/reports/r1", "sha256": "a" * 64}],
            "estimate": snap["estimate"],
            "provenance": {"build_id": None, "tool_version": "0.7.0", "engine_tag": "562ab353", "facts_version": 2,
                           "rules_versions": {"sony_bravia_8_ii": 3}, "records_digest": snap["records_digest"],
                           "settings_digest": "0" * 64}}


@pytest.mark.parametrize("depth", ["quick", "full"])
def test_run_result_accepts_real_nested_documents(tmp_path, depth):
    result = run_result(tmp_path, depth=depth)
    result["device"]["future_suite_key"] = {"kept": True}
    out = cc.validate_run_result(result)
    assert out["device"]["future_suite_key"] == {"kept": True}


@pytest.mark.parametrize("mutation", [
    "reports_dir_in_snapshot", "metadata_with_rank", "pixels_with_quick", "overall_only_device_present",
    "complete_with_missing_pairs", "absolute_report_url", "bad_report_hash", "quick_quality_score",
    "quick_missing_groups", "rank_bad_verdict", "request_mismatch", "unknown_top_level", "bad_failure_stage",
])
def test_run_result_invalid_required_or_nested_values_fail(tmp_path, mutation):
    depth = "full" if mutation in ("metadata_with_rank", "rank_bad_verdict") else "quick"
    result = run_result(tmp_path, depth=depth)
    if mutation == "reports_dir_in_snapshot":
        result["snapshot"]["reports_dir"] = str(tmp_path)
    elif mutation == "metadata_with_rank":
        result["measurement"] = "metadata"
    elif mutation == "pixels_with_quick":
        result["measurement"] = "pixels"
    elif mutation == "overall_only_device_present":
        result["request"] = dict(result["request"], results="overall", device=None, primary_player=None, player=None)
        result["snapshot"]["request"] = result["request"]
    elif mutation == "complete_with_missing_pairs":
        result["coverage"].update(missing_pairs=1)
    elif mutation == "absolute_report_url":
        result["reports"][0]["url"] = "http://127.0.0.1:8000/comparison/runs/run-0001/reports/r1"
    elif mutation == "bad_report_hash":
        result["reports"][0]["sha256"] = "zz"
    elif mutation == "quick_quality_score":
        result["device"]["devices"]["sony_bravia_8_ii/just_player"]["groups"][0]["quality_score"] = 1.0
    elif mutation == "quick_missing_groups":
        del result["overall"]["groups"]
    elif mutation == "rank_bad_verdict":
        next(iter(result["device"]["devices"].values()))["ranking"][0]["verdict"] = "MAYBE"
    elif mutation == "request_mismatch":
        result["request"] = dict(result["request"], lang="deu")
    elif mutation == "unknown_top_level":
        result["winner"] = "A.mkv"
    elif mutation == "bad_failure_stage":
        result["failures"] = [{"candidate_id": "c0", "stage": "teleport", "kind": "failed", "message": "x"}]
    with pytest.raises(cc.ContractError):
        cc.validate_run_result(result)


# ── capability gate ─────────────────────────────────────────────────────────────

def test_require_capabilities_reports_a_compatible_update_message():
    info = {"status": "ok", "tool_version": "0.6.0", "capabilities": ["devices", "calibration", "offline_payload"]}
    cc.require_capabilities(info, ["calibration"])
    with pytest.raises(cc.CapabilityError) as error:
        cc.require_capabilities(info, ["compare_all", "quick_recommendations", "calibration"])
    assert error.value.missing == ["compare_all", "quick_recommendations"]
    assert "0.7.0" in str(error.value) and "Advanced" in str(error.value)
    for broken in ({"status": "unavailable", "capabilities": []}, {"status": "too_old", "capabilities": ["compare_all"]},
                   {}, None):
        with pytest.raises(cc.CapabilityError):
            cc.require_capabilities(broken, ["compare_all"])


# ── actual emitted 4B documents ─────────────────────────────────────────────────

def test_real_emitted_4b_documents_match_parsers_and_fixture_shapes():
    assert sorted(p.stem for p in FIX4B.glob("*.json")) == sorted(FIXTURE_COMMANDS)
    for name, command in FIXTURE_COMMANDS.items():
        raw = (FIX4B / (name + ".json")).read_bytes()
        assert b"\r\n" not in raw
        doc = suite_client.parse_document(command, raw)
        assert doc["tool_version"] == "0.7.0"
        doc = json.loads(raw)
        doc["future_suite_key"] = {"optional": [1, None]}
        assert suite_client.parse_document(command, json.dumps(doc).encode())["future_suite_key"] == {"optional": [1, None]}
    plan = fixture("compare_plan")
    assert cc.records_digest(plan["records"], plan["candidates"], plan["records"][0]["facts_version"]) == plan["records_digest"]
    for c in plan["candidates"]:
        assert c["id"] == cc.record_id(c["path"], c["size"], c["mtime_ns"])
    assert fixture("compare_all")["records_digest"] == fixture("rank_device")["records_digest"] == plan["records_digest"]


@pytest.mark.parametrize("name,path,value", [
    ("compare_plan", "candidates.0.status", "maybe"), ("compare_plan", "pairs.0.a", 1),
    ("compare_plan", "estimate.pairs", "3"), ("compare_plan", "records_digest", 5),
    ("compare_plan", "mode", "fast"), ("compare_plan", "selection", "shortlist"),
    ("compare_all", "coverage.missing_pairs", -1), ("compare_all", "candidates.0.size", "big"),
    ("rank_overall", "coverage.completed_pairs", None), ("rank_device", "selected.player", 1),
    ("rank_device", "evaluated_players", [1]),
    ("quick_overall", "measurement", "pixels"), ("quick_overall", "overall.groups.0.status", "winner"),
    ("quick_overall", "overall.groups.0.ids", "A"), ("quick_overall", "overall.winner_ids.0", 3),
    ("quick_overall", "overall.coverage_complete", "yes"),
    ("quick_device", "devices.*.groups.0.basis.*.playback.evidence", "rumour"),
    ("quick_device", "devices.*.groups.0.basis.*.playback.verdict", "MAYBE"),
    ("quick_just_player_alternatives", "alternatives.0.order.groups", {}),
    ("quick_just_player_alternatives", "alternatives.0.player", None),
    ("quick_both", "selected.player", 7), ("quick_partial", "records.0.source.path", 1),
])
def test_required_nested_4b_field_mutation_is_a_contract_error(name, path, value):
    doc = fixture(name)
    target = doc
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[next(iter(target)) if part == "*" else int(part) if isinstance(target, list) else part]
    last = parts[-1]
    target[next(iter(target)) if last == "*" else int(last) if isinstance(target, list) else last] = value
    with pytest.raises(suite_client.SuiteError) as error:
        suite_client.parse_document(FIXTURE_COMMANDS[name], json.dumps(doc).encode())
    assert error.value.kind == "contract"


def test_quick_document_never_carries_measured_scores():
    for name in ("quick_overall", "quick_device", "quick_both", "quick_partial"):
        doc = fixture(name)
        assert doc["measurement"] == "metadata"
        text = json.dumps({k: doc[k] for k in ("overall", "devices", "alternatives")})
        assert "quality_score" not in text and "device_score" not in text
        doc["devices"] = copy.deepcopy(doc["devices"])
        for order in doc["devices"].values():
            order["groups"][0]["basis"][order["groups"][0]["ids"][0]]["device_score"] = 50.0
            break
        else:
            continue
        with pytest.raises(suite_client.SuiteError):
            suite_client.parse_document("quick", json.dumps(doc).encode())


def test_just_player_alternatives_fixture_keeps_primary_and_alternative_partition():
    doc = suite_client.parse_document("quick", (FIX4B / "quick_just_player_alternatives.json").read_bytes())
    assert doc["selected"] == {"device": "sony_bravia_8_ii", "player": "just_player"}
    assert list(doc["devices"]) == ["sony_bravia_8_ii/builtin_usb", "sony_bravia_8_ii/kodi", "sony_bravia_8_ii/just_player"]
    assert [(a["device"], a["player"]) for a in doc["alternatives"]] == [("sony_bravia_8_ii", "builtin_usb"),
                                                                        ("sony_bravia_8_ii", "kodi")]
    for alternative in doc["alternatives"]:
        assert alternative["order"] == doc["devices"][alternative["device"] + "/" + alternative["player"]]
        for group in alternative["order"]["groups"]:
            for row in group["basis"].values():
                assert row["playback"]["evidence"] in cc.EVIDENCE_WORDS


def test_legacy_suite_fixtures_stay_040_backwards_compat():
    legacy = ROOT / "tests" / "fixtures" / "suite"
    for name in ("version", "compare", "rank", "score", "score_partial", "devices"):
        assert json.loads((legacy / (name + ".json")).read_text(encoding="utf-8"))["tool_version"] == "0.4.0"


@pytest.mark.skipif(not (SUITE_PYTHON.is_file() and (COMPARIZER / "videosuite").is_dir()),
                    reason="needs the Comparizer checkout and its test interpreter")
def test_fixture_check_two_independent_generations_match_and_semantic_mutation_fails(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update(VIDEOSUITE_ROOT=str(COMPARIZER), VIDEOSUITE_PYTHON=str(SUITE_PYTHON))
    script = ROOT / "scripts" / "make_suite_fixtures.py"
    ok = subprocess.run([sys.executable, str(script), "--plan4b", "--check", "--scratch", str(tmp_path / "check")],
                        env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "two independent generations match" in ok.stdout
    assert "semantic mutation detected" in ok.stdout
    # A checked-in fixture copy with one semantic change must be reported as drift (nonzero exit).
    drifted = tmp_path / "drifted"
    drifted.mkdir()
    for path in FIX4B.glob("*.json"):
        (drifted / path.name).write_bytes(path.read_bytes())
    plan = fixture("compare_plan")
    plan["records"][0]["hdr"]["formats"] = ["hdr10"]
    (drifted / "compare_plan.json").write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    bad = subprocess.run([sys.executable, str(script), "--plan4b", "--check", "--scratch", str(tmp_path / "bad"),
                          "--fixtures", str(drifted)], env=env, capture_output=True, text=True, encoding="utf-8",
                         timeout=600)
    assert bad.returncode != 0
    assert "compare_plan" in bad.stdout + bad.stderr


def test_run_request_path_count_and_length_are_bounded():
    assert cc.validate_run_request(request(paths=[r"C:\m\%d.mkv" % i for i in range(50)]))
    for paths in ([r"C:\m\%d.mkv" % i for i in range(51)], ["C:\\" + "x" * 4100 + ".mkv"]):
        with pytest.raises(cc.ContractError):
            cc.validate_run_request(request(paths=paths))
