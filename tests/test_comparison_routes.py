"""Unified comparison HTTP routes (Plan 4B U5): final states through TestClient, real public serialization.

BackgroundTasks run before TestClient returns, so each POST is followed by assertions on final states. The fake
suite answers with the real U4 emitted documents; the request paths are the shared harness's virtual inputs, admitted
by a test-only path-guard seam (they are never created or read). Serialized HTTP envelopes are pinned under
``tests/fixtures/comparison`` (set ``PLAN4B_REGEN_FIXTURES=1`` to rewrite them from these same flows).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

import comparison_contracts as cc
import comparison_routes
import comparison_runs as cr
import jobs as jobs_module
import main
import suite_routes
from suite_client import get_suite_client
from test_comparison_runs import PATHS, FakeSuite, fixture4b, virtual_stat

FIXDIR = Path(__file__).parent / "fixtures" / "comparison"
REGEN = os.environ.get("PLAN4B_REGEN_FIXTURES") == "1"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Shared deterministic clock/identity harness: run ids, revisions, job ids, timestamps and data root."""
    monkeypatch.setattr(cr, "_stat", virtual_stat)
    monkeypatch.setattr(comparison_routes, "_guard_paths", lambda paths: list(paths))
    ids = iter("run-%04d" % i for i in range(1, 1000))
    cr.store.configure(data_root=tmp_path / "app", clock=lambda: 1700000000.0, new_id=lambda: next(ids))
    counter = iter(range(1, 1000))
    monkeypatch.setattr(jobs_module.uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))
    monkeypatch.setattr(jobs_module.jobs, "_wall", lambda: 1700000000.0)
    suite = FakeSuite()
    main.app.dependency_overrides[get_suite_client] = lambda: suite
    yield suite
    main.app.dependency_overrides.pop(get_suite_client, None)


def body(results="both", depth="full", **over):
    raw = {"input_kind": "files", "results": results, "depth": depth, "paths": list(PATHS), "lang": "eng",
           "device": None, "primary_player": None, "player": None, "include_alternatives": True, "fs": None,
           "audio_output": None}
    if results != "overall":
        raw.update(device="sony_bravia_8_ii", primary_player="just_player", player="all")
    raw.update(over)
    return raw


def plan_and_confirm(client, raw):
    r = client.post("/comparison/plans", json=raw)
    assert r.status_code == 200, r.text
    started = r.json()
    assert set(started) == {"job_id", "run_id"}
    detail = client.get(f"/comparison/runs/{started['run_id']}").json()
    assert detail["run"]["stage"] == "awaiting_confirmation" and detail["result"] is None
    assert client.get(f"/job/{started['job_id']}").json()["status"] == "done"   # the plan job ended; run awaits
    r = client.post("/comparison/runs", json={"run_id": started["run_id"], "plan_revision": detail["run"]["plan_revision"]})
    assert r.status_code == 200, r.text
    return started, r.json()


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


def pin(name: str, value) -> None:
    path = FIXDIR / name
    text = json.dumps(value, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if REGEN:
        FIXDIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == text, f"{name} drifted from the actual HTTP serialization"


def test_real_http_serialization_uses_public_snapshots_everywhere(client, harness):
    started, run = plan_and_confirm(client, body("both", "full"))
    run_id = started["run_id"]
    internal = cr.store.get(run_id)
    cc.validate_internal_snapshot(internal)                       # internal state REQUIRES reports_dir
    without = dict(internal)
    del without["reports_dir"]
    with pytest.raises(cc.ContractError):
        cc.validate_internal_snapshot(without)
    reports_dir = internal["reports_dir"]
    responses = {
        "list": client.get("/comparison/runs").json(),
        "detail": client.get(f"/comparison/runs/{run_id}").json(),
        "plan_job": client.get(f"/job/{started['job_id']}").json(),
        "run_job": client.get(f"/job/{run['job_id']}").json(),
    }
    text = json.dumps(responses)
    assert "reports_dir" not in set(walk_keys(responses))
    assert reports_dir not in text and reports_dir.replace("\\", "\\\\") not in text
    snapshots = [responses["detail"]["run"], responses["detail"]["result"]["snapshot"], responses["run_job"]["run"],
                 responses["plan_job"]["run"], *responses["list"]["runs"]]
    for snapshot in snapshots:
        assert cc.validate_public_snapshot(snapshot) == snapshot
    result = responses["detail"]["result"]
    assert cc.validate_run_result(result) == result == responses["run_job"]["results"]
    for report in result["reports"]:
        assert report["url"] == f"/comparison/runs/{run_id}/reports/{report['report_id']}"
        assert Path(cr.report_path(run_id, report["report_id"])).parent == Path(reports_dir)
    pin("public_snapshots_http.json", responses)


def test_local_quick_both_and_full_both_envelopes(client, harness):
    started, _ = plan_and_confirm(client, body("both", "quick", player="just_player", include_alternatives=False))
    quick = client.get(f"/comparison/runs/{started['run_id']}").json()
    assert quick["result"]["measurement"] == "metadata" and quick["result"]["device"]["schema"] == "videosuite-quick/1"
    assert quick["result"]["overall"] == quick["result"]["device"]["overall"]
    assert list(quick['result']['device']['devices']) == ['sony_bravia_8_ii/just_player']
    assert quick['result']['device']['alternatives'] == []
    pin("local_quick_both.json", quick)
    started, _ = plan_and_confirm(client, body("both", "full"))
    full = client.get(f"/comparison/runs/{started['run_id']}").json()
    result = full["result"]
    assert result["overall"] == fixture4b("rank_overall") and result["device"] == fixture4b("rank_device")
    assert result["provenance"]["records_digest"] == fixture4b("compare_plan")["records_digest"]
    assert result["coverage"]["complete"] is True
    pin("local_full_both.json", full)


def test_failed_pairs_and_changed_settings_envelopes(client, harness, tmp_path):
    doc = json.loads(json.dumps(harness.compare_doc))
    doc.update(failed=1)
    doc["pairs"][2]["report"] = None
    doc["coverage"].update(completed_pairs=2, missing_pairs=1)
    harness.compare_doc = doc
    started, run = plan_and_confirm(client, body("both", "full"))
    failed = client.get(f"/comparison/runs/{started['run_id']}").json()
    assert failed["result"]["coverage"]["complete"] is False
    job = client.get(f"/job/{run['job_id']}").json()
    assert (job["status"], job["error_kind"]) == ("error", "failed_pairs")
    pin("failed_pairs.json", {"run": failed, "job": job})
    harness.compare_doc = fixture4b("compare_all")
    settings = tmp_path / "owner-settings.json"
    settings.write_text('{"schema":"videosuite-settings/1","devices":{},"rule_overrides":[]}', encoding="utf-8")
    harness.settings_path = str(settings)
    harness.hooks["compare"] = lambda kwargs: settings.write_text('{"changed":true}', encoding="utf-8") and None
    started, run = plan_and_confirm(client, body("both", "full"))
    changed = client.get(f"/comparison/runs/{started['run_id']}").json()
    job = client.get(f"/job/{run['job_id']}").json()
    assert (job["status"], job["error_kind"]) == ("error", "settings_changed")
    assert changed["result"]["device"] is None and len(changed["result"]["reports"]) == 3
    assert changed['result']['overall'] == fixture4b('rank_overall')
    assert changed['result']['coverage']['complete'] is True
    envelope = json.loads(json.dumps({"run": changed, "job": job}).replace(json.dumps(str(tmp_path))[1:-1], "<SCRATCH>"))
    pin("changed_settings.json", envelope)


def test_just_player_plus_alternatives_survives_http_reconnect(harness, isolated_uploads):
    from fastapi.testclient import TestClient
    first = TestClient(main.app, base_url="http://localhost")       # a browser session (no app lifespan change)
    started, run = plan_and_confirm(first, body("both", "quick"))
    first.close()
    # The browser is gone; the work already finished without any poll. A fresh client reconnects, also while a
    # "Quit when finished" is pending (reads stay available).
    main.lifecycle.request_quit("when_finished")
    with TestClient(main.app, base_url="http://localhost") as fresh:
        response = fresh.get("/comparison/runs")
        assert response.status_code == 200, response.text
        listed = response.json()["runs"]
        detail = fresh.get(f"/comparison/runs/{started['run_id']}").json()
        job = fresh.get(f"/job/{run['job_id']}").json()
    assert [r["run_id"] for r in listed] == [started["run_id"]]
    for snapshot in (listed[0], detail["run"], detail["result"]["snapshot"], job["run"]):
        req = snapshot["request"]
        assert (req["primary_player"], req["player"], req["include_alternatives"]) == ("just_player", "all", True)
        assert snapshot["conditions"]["primary_player"] == "just_player" and snapshot["conditions"]["player"] == "all"
    device = detail["result"]["device"]
    real = fixture4b("quick_just_player_alternatives")
    assert device["selected"]["player"] == "just_player"
    assert device["alternatives"] == real["alternatives"] and device["devices"] == real["devices"]
    assert detail["result"] == job["results"]
    pin("local_just_player_alternatives.json", detail)


def test_plan_request_validation_is_422_without_echo_and_creates_nothing(client, harness):
    secret = "C:\\Секрет & 100%\\x.mkv"
    for raw in (body(extra=1), body(results="overall", device="sony_bravia_8_ii"), body(player="kodi"), body(paths=[]),
                body(paths=[secret], depth="slow")):
        r = client.post("/comparison/plans", json=raw)
        assert r.status_code == 422
        assert secret not in r.text and "kodi" not in r.text
    assert client.post("/comparison/plans", content=b"[1]", headers={"content-type": "application/json"}).status_code in (400, 422)
    assert cr.store.list_all() == [] and len(jobs_module.jobs) == 0


def test_cancel_then_edited_replan_leaves_only_new_plan_pending_after_reload(client, harness):
    old = client.post('/comparison/plans', json=body('overall', 'quick')).json()['run_id']
    response = client.post('/comparison/runs/' + old + '/cancel', json={})
    assert response.status_code == 200 and response.json()['stage'] == 'cancelled'
    new = client.post('/comparison/plans', json=body('both', 'quick')).json()['run_id']
    from fastapi.testclient import TestClient
    with TestClient(main.app, base_url='http://localhost') as reloaded:
        pending = [r for r in reloaded.get('/comparison/runs').json()['runs']
                   if r['stage'] in cr.PENDING_STAGES]
        assert [r['run_id'] for r in pending] == [new]
        assert reloaded.get('/comparison/runs/' + old).json()['run']['stage'] == 'cancelled'


@pytest.mark.parametrize('identity,expected', [(None, None), ({'build_id': 'installed-release-42'}, 'installed-release-42')])
def test_lifespan_uses_verified_installed_release_identity(harness, monkeypatch, identity, expected):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main.app.state, 'app_identity', identity, raising=False)
    with TestClient(main.app, base_url='http://localhost') as client:
        started, _ = plan_and_confirm(client, body('overall', 'quick'))
        detail = client.get('/comparison/runs/' + started['run_id']).json()
        assert detail['result']['provenance']['build_id'] == expected


def test_magnet_full_is_refused_before_probing(client, harness, monkeypatch):
    monkeypatch.setattr(cr.magnet, 'run_magnet_job_threaded', lambda *a, **k: pytest.fail('Full touched torrent'))
    raw = body("overall", "quick", device=None, primary_player=None, player=None, paths=None,
               input_kind="magnets", magnets=[{"uri": "magnet:?xt=urn:btih:" + "a" * 40, "file_index": None}])
    raw['depth'] = 'full'
    r = client.post("/comparison/plans", json=raw)
    assert r.status_code == 422 and cr.store.list_all() == []


def test_capability_missing_ends_the_plan_with_an_update_message(client, harness):
    harness.version_doc["capabilities"] = harness.version_doc["capabilities"][:10]
    r = client.post("/comparison/plans", json=body("overall", "quick"))
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert job["status"] == "error" and "0.7.0" in job["error"]
    assert client.get(f"/comparison/runs/{r.json()['run_id']}").json()["run"]["stage"] == "error"
    assert harness.work() == []


def test_confirm_twice_stale_revision_busy_and_unknown_runs(client, harness):
    r = client.post("/comparison/plans", json=body("overall", "full"))
    run_id = r.json()["run_id"]
    revision = client.get(f"/comparison/runs/{run_id}").json()["run"]["plan_revision"]
    assert client.post("/comparison/runs", json={"run_id": run_id, "plan_revision": "rev-9999"}).status_code == 409
    held = suite_routes.acquire_lease("compare")                # another heavy job is running
    try:
        assert client.post("/comparison/runs", json={"run_id": run_id, "plan_revision": revision}).status_code == 409
        assert client.get(f"/comparison/runs/{run_id}").json()["run"]["stage"] == "awaiting_confirmation"
    finally:
        held.release()
    assert client.post("/comparison/runs", json={"run_id": run_id, "plan_revision": revision}).status_code == 200
    assert client.post("/comparison/runs", json={"run_id": run_id, "plan_revision": revision}).status_code == 409
    assert client.get("/comparison/runs/run-unknown").status_code == 404
    assert client.post("/comparison/runs", json={"run_id": "run-unknown", "plan_revision": "x"}).status_code == 404
    assert client.post("/comparison/runs", json={"run_id": run_id}).status_code == 422


def test_confirmation_verification_runs_outside_event_loop(client, harness, monkeypatch):
    import asyncio
    original = cr.verify_current
    checked = []
    def verify(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            checked.append(True)
        else:
            pytest.fail('blocking suite verification ran on the event loop')
        return original(*args, **kwargs)
    monkeypatch.setattr(cr, 'verify_current', verify)
    plan_and_confirm(client, body('overall', 'quick'))
    assert checked


def test_pending_plan_cancel_route(client, harness):
    r = client.post("/comparison/plans", json=body("overall", "quick"))
    run_id = r.json()["run_id"]
    assert client.post(f"/comparison/runs/{run_id}/cancel", json={"x": 1}).status_code == 400
    assert client.post(f"/comparison/runs/{run_id}/cancel", json={}).json() == {"run_id": run_id, "stage": "cancelled"}
    revision = client.get(f"/comparison/runs/{run_id}").json()["run"]["plan_revision"]
    assert client.post("/comparison/runs", json={"run_id": run_id, "plan_revision": revision}).status_code == 409
    assert harness.work() == ["facts"]


def test_quit_refuses_new_plans_and_runs(client, harness):
    main.lifecycle.request_quit("when_finished")
    try:
        assert client.post("/comparison/plans", json=body("overall", "quick")).status_code == 503
        assert client.post("/comparison/runs", json={"run_id": "run-0001", "plan_revision": "x"}).status_code == 503
    finally:
        main.lifecycle.request_quit("keep")


def test_paste_fallback_uses_the_chooser_guards_before_work(client, harness, monkeypatch, tmp_path):
    import native_chooser
    monkeypatch.setattr(comparison_routes, '_guard_paths', native_chooser.guarded_paths)
    a = tmp_path / 'A.mkv'
    a.write_bytes(b'a')
    other = tmp_path / 'other'
    other.mkdir(); (other / 'A.mkv').write_bytes(b'a')
    for paths in ([str(a)], [str(a), str(a).upper()], [str(a), str(other / 'A.mkv')],
                  [str(a), str(a) + ':stream'], [str(a), r'\\server\share\B.mkv']):
        assert client.post('/comparison/plans', json=body('overall', 'quick', paths=paths)).status_code in (400, 404)
    assert harness.work() == [] and cr.store.list_all() == []
