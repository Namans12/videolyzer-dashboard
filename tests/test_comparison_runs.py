"""Backend-owned unified comparison runs (Plan 4B U5): direct worker tests.

The suite is a fake that answers with the REAL U4 emitted documents (``tests/fixtures/suite4b``) for the shared
deterministic harness inputs ``C:\\plan4b-fixture-inputs\\A/B/C.mkv`` (sizes 4096/8192/12288, mtimes 1700000000000000000
+0/1/2). Those virtual files are never created or read: the run module's stat seam maps them and refuses any other
virtual path. Boundary cancellation/disconnect use Events/barriers, never sleeps.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import comparison_contracts as cc
import comparison_runs as cr
import jobs as jobs_module
import suite_routes
from suite_client import SuiteClient

FIX4B = Path(__file__).parent / "fixtures" / "suite4b"
FIX = Path(__file__).parent / "fixtures" / "suite"
VIRTUAL = "C:\\plan4b-fixture-inputs\\"
PATHS = [VIRTUAL + n + ".mkv" for n in "ABC"]
STATS = {p: SimpleNamespace(st_size=(i + 1) * 4096, st_mtime_ns=1700000000000000000 + i) for i, p in enumerate(PATHS)}
WORK = ("facts", "facts_partial", "plan_compare", "compare", "rank", "quick", "score", "score_partial")


def fixture4b(name: str) -> dict:
    return json.loads((FIX4B / (name + ".json")).read_text(encoding="utf-8"))


def virtual_stat(path, *args, **kwargs):
    key = str(path)
    if key.startswith(VIRTUAL):
        if key not in STATS:
            raise AssertionError("unmapped virtual input")
        return STATS[key]
    return os.stat(path, *args, **kwargs)


class FakeSuite:
    """Answers with real emitted documents; records every call; ``hooks[name]`` runs before answering."""

    def __init__(self, settings_path: str | None = None, **hooks):
        self.calls: list[tuple[str, dict]] = []
        self.hooks = hooks
        self.settings_path = settings_path
        self.version_doc = fixture4b("version")
        self.compare_doc = fixture4b("compare_all")
        self.settings_files: list = []

    def _call(self, name, kwargs):
        self.calls.append((name, kwargs))
        hook = self.hooks.get(name)
        if hook is not None:
            out = hook(kwargs)
            if out is not None:
                return out
        return None

    def work(self):
        return [name for name, _ in self.calls if name in WORK]

    def kwargs(self, name, index=0):
        return [k for n, k in self.calls if n == name][index]

    def for_settings(self, path):
        self.settings_files.append(path)
        return self

    def version(self):
        return self._call("version", {}) or copy.deepcopy(self.version_doc)

    def devices(self):
        doc = json.loads((FIX / "devices.json").read_text(encoding="utf-8"))
        if self.settings_path is not None:
            doc["settings"]["path"] = self.settings_path
        return self._call("devices", {}) or doc

    def facts(self, paths, deep_dv=False, *, cancel=None):
        out = self._call("facts", dict(paths=list(paths), deep_dv=deep_dv, cancel=cancel))
        if out is not None:
            return None if out == "cancel" else out
        plan = fixture4b("compare_plan")
        return {"schema": "videosuite-facts/1", "tool_version": plan["tool_version"], "records": plan["records"]}

    def plan_compare(self, paths, out_dir, **kwargs):
        out = self._call("plan_compare", dict(kwargs, paths=list(paths), out_dir=out_dir))
        if out is not None:
            return None if out == "cancel" else out
        assert not Path(out_dir).exists()
        return fixture4b("compare_plan")

    def compare(self, paths, out_dir, mode="fast", top=4, **kwargs):
        out = self._call("compare", dict(kwargs, paths=list(paths), out_dir=out_dir, mode=mode))
        if out is not None:
            return None if out == "cancel" else out
        doc = copy.deepcopy(self.compare_doc)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for pair in doc["pairs"]:
            if pair["report"]:
                # The suite writes its pair reports into the requested folder (the fixture's <SCRATCH> root).
                pair["report"] = pair["report"].replace("<SCRATCH>\\reports", out_dir)
                Path(pair["report"]).write_text(json.dumps({"pair": [pair["a"], pair["b"]], "winner": "video1"},
                                                           sort_keys=True), encoding="utf-8", newline="\n")
        return doc

    def rank(self, paths, reports_dir, **kwargs):
        out = self._call("rank", dict(kwargs, paths=list(paths), reports_dir=reports_dir))
        if out is not None:
            return None if out == "cancel" else out
        return fixture4b("rank_overall" if kwargs.get("results") == "overall" else "rank_device")

    def quick(self, snapshot, **kwargs):
        out = self._call("quick", dict(kwargs, snapshot=snapshot))
        if out is not None:
            return None if out == "cancel" else out
        name = {"overall": "quick_overall", "device": "quick_device", "both": "quick_both"}[kwargs["results"]]
        if kwargs.get("player") == "all" and kwargs.get("results") == "both":
            name = "quick_just_player_alternatives"
        return fixture4b(name)


@pytest.fixture
def seams(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "_stat", virtual_stat)
    ids = iter("run-%04d" % i for i in range(1, 1000))
    cr.store.configure(data_root=tmp_path / "app", clock=lambda: 1700000000.0, new_id=lambda: next(ids))
    return tmp_path


def request(results="both", depth="full", **over) -> dict:
    device = {} if results == "overall" else {"device": "sony_bravia_8_ii", "primary_player": "just_player",
                                              "player": "all", "include_alternatives": True}
    raw = {"input_kind": "files", "results": results, "depth": depth, "paths": list(PATHS), "lang": "eng", **device}
    raw.update(over)
    return cc.validate_run_request(raw)


def planned(req, client) -> str:
    """Create, plan and leave the run awaiting confirmation (the route does the same through HTTP)."""
    run_id = cr.store.create(req)
    job_id = jobs_module.jobs.create("comparison_plan", run_id=run_id)
    cr.store.update(run_id, job_id=job_id)
    cr.plan_run(run_id, client)
    snap = cr.store.get(run_id)
    assert snap["stage"] == "awaiting_confirmation", jobs_module.jobs.get(job_id)
    assert jobs_module.jobs.get(job_id)["status"] == "done"
    return run_id


def confirmed(run_id, client, *, slot) -> tuple[str, suite_routes.SlotLease]:
    snap = cr.store.get(run_id)
    lease = suite_routes.acquire_lease(slot)
    cr.store.confirm(run_id, snap["plan_revision"])
    cr.verify_current(run_id, client)
    job_id = jobs_module.jobs.create("comparison_run", run_id=run_id)
    cr.store.update(run_id, job_id=job_id)
    return job_id, lease


def run_to_end(req, client):
    run_id = planned(req, client)
    job_id, lease = confirmed(run_id, client, slot="score_rank" if req["depth"] == "quick" else "compare")
    cr.execute_run(run_id, client, lease=lease)
    return run_id, job_id


SIX = [("quick", "overall", ["facts", "quick"]), ("quick", "device", ["facts", "quick"]),
       ("quick", "both", ["facts", "quick"]), ("full", "overall", ["plan_compare", "compare", "rank"]),
       ("full", "device", ["plan_compare", "compare", "rank"]),
       ("full", "both", ["plan_compare", "compare", "rank", "rank"])]


@pytest.mark.parametrize("depth,results,work", SIX)
def test_six_modes_run_only_requested_stages(seams, depth, results, work):
    client = FakeSuite()
    run_id, job_id = run_to_end(request(results, depth), client)
    assert client.work() == work
    names = [n for n, _ in client.calls]
    assert "devices" in names if results != "overall" else "devices" not in names   # Overall: no catalogue
    result = cr.store.result(run_id)
    assert cc.validate_run_result(result) == result
    assert result["completed_stages"] == (["planning", "facts", "quick"] if depth == "quick"
                                          else ["planning", "facts", "pixels", "rank"])
    assert result["measurement"] == ("metadata" if depth == "quick" else "pixels")
    assert (result["overall"] is None) == (results == "device")
    assert (result["device"] is None) == (results == "overall")
    job = jobs_module.jobs.get(job_id)
    assert job["status"] == "done" and job["results"] == result
    if depth == "quick":
        quick = client.kwargs("quick")
        assert quick["snapshot"]["records_digest"] == result["snapshot"]["records_digest"]
        assert "deep_dv" not in client.kwargs("facts") or client.kwargs("facts")["deep_dv"] is False
    else:
        compare = client.kwargs("compare")
        assert compare["all_candidates"] is True and compare["mode"] == "accurate" and compare["results"] == results
        assert compare["records"]["records_digest"] == result["snapshot"]["records_digest"]
        assert compare["paths"] == PATHS
        ranks = [k for n, k in client.calls if n == "rank"]
        assert [k["results"] for k in ranks] == (["overall", "device"] if results == "both" else [results])
        assert all(k["records"]["records_digest"] == result["snapshot"]["records_digest"] for k in ranks)
        assert len(result["reports"]) == 3
    selection = [k for n, k in client.calls if n in ("quick", "compare", "rank", "plan_compare")]
    for kwargs in selection:
        if kwargs.get("results") == "overall":
            assert kwargs.get("primary_player") is None and kwargs.get("player") is None and kwargs.get("device") is None
        else:
            assert (kwargs["device"], kwargs["primary_player"], kwargs["player"]) == ("sony_bravia_8_ii", "just_player", "all")


def test_browser_disconnect_between_compare_and_rank_does_not_stop_chain(seams):
    entered, release = threading.Event(), threading.Event()
    def compare_hook(kwargs):
        entered.set()
        assert release.wait(10)
    client = FakeSuite(compare=compare_hook)
    run_id = planned(request("both", "full"), client)
    job_id, lease = confirmed(run_id, client, slot="compare")
    worker = threading.Thread(target=cr.execute_run, args=(run_id, client), kwargs={"lease": lease}, daemon=True)
    worker.start()
    assert entered.wait(10)
    # No frontend request, poll or SSE connection exists from here on: the backend owns the chain.
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert client.work()[-2:] == ["rank", "rank"]
    assert cr.store.get(run_id)["stage"] == "done" and cr.store.result(run_id)["device"] is not None
    assert jobs_module.jobs.get(job_id)["status"] == "done"


def test_cancel_between_stages_never_starts_rank(seams):
    holder = {}
    def compare_hook(kwargs):
        jobs_module.jobs.request_cancel(holder["job"])       # cancelled while compare finishes its last pair
    client = FakeSuite(compare=compare_hook)
    run_id = planned(request("device", "full"), client)
    job_id, lease = confirmed(run_id, client, slot="compare")
    holder["job"] = job_id
    cr.execute_run(run_id, client, lease=lease)
    assert "rank" not in client.work()
    assert cr.store.get(run_id)["stage"] == "cancelled"
    assert jobs_module.jobs.get(job_id)["status"] == "cancelled"
    result = cr.store.result(run_id)
    assert result["completed_stages"] == ["planning", "facts", "pixels"] and len(result["reports"]) == 3
    assert suite_routes._slots.try_acquire("compare") is None              # lease released


def test_settings_or_profile_change_stops_later_stage_and_preserves_reports(seams, tmp_path):
    settings = tmp_path / "owner-settings.json"
    original = b'{"schema":"videosuite-settings/1","devices":{},"rule_overrides":[]}'
    settings.write_bytes(original)
    def compare_hook(kwargs):
        settings.write_bytes(original + b" ")                    # the owner edits settings during pixels
    client = FakeSuite(settings_path=str(settings), compare=compare_hook)
    run_id = planned(request("both", "full"), client)
    snap = cr.store.get(run_id)
    run_settings = Path(snap["reports_dir"]).parent / "settings.json"
    assert run_settings.read_bytes() == original and client.settings_files == []
    job_id, lease = confirmed(run_id, client, slot="compare")
    cr.execute_run(run_id, client, lease=lease)
    assert client.settings_files == [run_settings]               # the per-run client uses the frozen copy
    assert [k['results'] for n, k in client.calls if n == 'rank'] == ['overall']
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error_kind"]) == ("error", "settings_changed")
    result = cr.store.result(run_id)
    assert result["device"] is None and result["overall"] == fixture4b('rank_overall') and len(result["reports"]) == 3
    assert result['coverage']['complete'] is True
    assert any(f["kind"] == "settings_changed" for f in result["failures"])
    assert all(Path(cr.report_path(run_id, r["report_id"])).is_file() for r in result["reports"])
    assert settings.read_bytes() == original + b" "              # the original is never written
    # A profile/rules revision change is the same stop.
    client2 = FakeSuite()
    def bump(kwargs):
        client2.version_doc["rules_versions"]["sony_bravia_8_ii"] += 1
    client2.hooks["compare"] = bump
    run2 = planned(request("device", "full"), client2)
    job2, lease2 = confirmed(run2, client2, slot="compare")
    cr.execute_run(run2, client2, lease=lease2)
    assert "rank" not in client2.work() and jobs_module.jobs.get(job2)["error_kind"] == "settings_changed"


@pytest.mark.parametrize("ending", ["success", "error", "cancel", "exception", "failed_pairs"])
def test_compare_rank_share_one_heavy_lease_and_release_on_every_exit(seams, ending):
    seen = []
    def compare_hook(kwargs):
        seen.append(suite_routes._slots.try_acquire("compare"))
        if ending == "error":
            from suite_client import SuiteError
            raise SuiteError("timeout", "videosuite compare timed out.")
        if ending == "exception":
            raise RuntimeError("bug")
        if ending == "cancel":
            return "cancel"
        if ending == "failed_pairs":
            doc = copy.deepcopy(client.compare_doc)
            doc.update(failed=1)
            doc["pairs"][2]["report"] = None
            doc["coverage"].update(completed_pairs=2, missing_pairs=1)
            client.compare_doc = doc
    def rank_hook(kwargs):
        seen.append(suite_routes._slots.try_acquire("compare"))
    client = FakeSuite(compare=compare_hook, rank=rank_hook)
    run_id = planned(request("overall", "full"), client)
    job_id, lease = confirmed(run_id, client, slot="compare")
    acquired = []
    original = suite_routes._Slots.try_acquire
    def spy(self, slot):
        acquired.append(slot)
        return original(self, slot)
    suite_routes._Slots.try_acquire = spy
    try:
        cr.execute_run(run_id, client, lease=lease)
    finally:
        suite_routes._Slots.try_acquire = original
    assert all(detail is not None for detail in seen)            # held during compare and rank
    assert acquired == ["compare"] * len(seen)                   # only the probes: no nested acquisition
    assert suite_routes._slots.try_acquire("compare") is None    # released on every exit
    lease.release()                                              # idempotent: never frees another holder
    assert suite_routes._slots.try_acquire("compare") is not None
    status = jobs_module.jobs.get(job_id)["status"]
    assert status == {"success": "done", "error": "error", "cancel": "cancelled", "exception": "error",
                      "failed_pairs": "error"}[ending]


def test_failed_pair_keeps_partial_tables_and_blocks_whole_set_winner(seams):
    client = FakeSuite()
    doc = copy.deepcopy(client.compare_doc)
    doc.update(failed=1)
    doc["pairs"][2]["report"] = None
    doc["coverage"].update(completed_pairs=2, missing_pairs=1)
    client.compare_doc = doc
    run_id, job_id = run_to_end(request("both", "full"), client)
    job = jobs_module.jobs.get(job_id)
    assert (job["status"], job["error_kind"]) == ("error", "failed_pairs")
    result = cr.store.result(run_id)
    assert result["overall"] is not None and result["device"] is not None      # useful partial tables
    assert result["coverage"] == {"submitted": 3, "readable": 3, "expected_pairs": 3, "completed_pairs": 2,
                                  "missing_pairs": 1, "complete": False}
    assert len(result["reports"]) == 2
    assert any(f["kind"] == "failed_pairs" for f in result["failures"])
    assert cr.store.get(run_id)["stage"] == "error" and job["results"] == result


def test_confirm_rejects_stale_plan_or_file_change(seams, monkeypatch):
    client = FakeSuite()
    run_id = planned(request("overall", "full"), client)
    snap = cr.store.get(run_id)
    with pytest.raises(cr.RunConflict):
        cr.store.confirm(run_id, "rev-stale")
    assert cr.store.get(run_id)["stage"] == "awaiting_confirmation"
    cr.store.confirm(run_id, snap["plan_revision"])
    with pytest.raises(cr.RunConflict):                           # no second start / double click
        cr.store.confirm(run_id, snap["plan_revision"])
    # A file changed after planning: the confirmation is refused before any work starts.
    run2 = planned(request("overall", "full"), client)
    changed = dict(STATS)
    changed[PATHS[1]] = SimpleNamespace(st_size=8193, st_mtime_ns=1700000000000000001)
    monkeypatch.setattr(cr, "_stat", lambda p, *a, **k: changed[str(p)] if str(p) in changed else os.stat(p))
    cr.store.confirm(run2, cr.store.get(run2)["plan_revision"])
    with pytest.raises(cr.RunConflict, match="changed"):
        cr.verify_current(run2, client)
    assert cr.store.get(run2)["stage"] == "error"
    assert "compare" not in client.work()


def test_just_player_plus_alternatives_quick_keeps_primary_and_evaluation_separate(seams):
    client = FakeSuite()
    run_id, _ = run_to_end(request("both", "quick"), client)
    quick = client.kwargs("quick")
    assert (quick["device"], quick["primary_player"], quick["player"]) == ("sony_bravia_8_ii", "just_player", "all")
    result = cr.store.result(run_id)
    real = fixture4b("quick_just_player_alternatives")
    assert result["device"] == real                               # the untouched real suite document
    assert result["snapshot"]["conditions"]["evaluated_players"] == ["builtin_usb", "kodi", "just_player"]
    assert (result["request"]["primary_player"], result["request"]["player"]) == ("just_player", "all")


@pytest.mark.parametrize('plan_status', ['done', 'running'])
def test_finished_plan_job_keeps_its_final_snapshot_after_confirmation(seams, plan_status):
    client = FakeSuite()
    run_id = planned(request('both', 'quick'), client)
    plan_id = cr.store.get(run_id)['job_id']
    # Also cover confirmation between publishing the ready plan and finishing its job.
    jobs_module.jobs.update(plan_id, status=plan_status)
    before = jobs_module.jobs.get(plan_id)
    job_id, lease = confirmed(run_id, client, slot='score_rank')
    try:
        assert jobs_module.jobs.get(plan_id) == before
        assert before['run']['stage'] == 'awaiting_confirmation'
        cr.execute_run(run_id, client, lease=lease)
        assert jobs_module.jobs.get(plan_id) == before
        assert jobs_module.jobs.get(job_id)['run']['stage'] == 'done'
    finally:
        lease.release()
        jobs_module.jobs.finish(plan_id)


def test_single_player_both_uses_real_single_player_document(seams):
    client = FakeSuite()
    run_id, _ = run_to_end(request('both', 'quick', player='just_player', include_alternatives=False), client)
    result = cr.store.result(run_id)
    assert result['device'] == fixture4b('quick_both')
    assert list(result['device']['devices']) == ['sony_bravia_8_ii/just_player']
    assert result['device']['alternatives'] == []
    assert result['snapshot']['conditions']['evaluated_players'] == ['just_player']


def test_internal_state_keeps_reports_dir_and_public_projection_omits_it(seams):
    client = FakeSuite()
    run_id, job_id = run_to_end(request("overall", "full"), client)
    internal = cr.store.get(run_id)
    assert Path(internal["reports_dir"]).is_absolute() and Path(internal["reports_dir"]).is_dir()
    for value in (cr.store.result(run_id), jobs_module.jobs.get(job_id), cr.public(run_id)):
        assert "reports_dir" not in json.dumps(value) and internal["reports_dir"].replace("\\", "\\\\") not in json.dumps(value)


def test_basis_digest_covers_request_settings_and_versions():
    req = request("device", "full")
    versions = {"tool_version": "0.7.0", "engine_tag": "562ab353", "facts_version": 2, "rules_versions": {"x": 3}}
    base = cr.basis_digest(req, settings_bytes=b"a", versions=versions)
    assert base == cr.basis_digest(copy.deepcopy(req), settings_bytes=b"a", versions=dict(versions))
    assert base != cr.basis_digest(req, settings_bytes=b"b", versions=versions)
    assert base != cr.basis_digest(req, settings_bytes=None, versions=versions)
    assert base != cr.basis_digest(req, settings_bytes=b"a", versions=dict(versions, rules_versions={"x": 4}))
    assert base != cr.basis_digest(dict(req, player="just_player", include_alternatives=False),
                                   settings_bytes=b"a", versions=versions)


def test_client_for_snapshot_never_mutates_the_shared_client(tmp_path):
    shared = SuiteClient(argv_prefix=["PY", "-m", "videosuite"], env={"VIDEOSUITE_SETTINGS": "C:\\owner\\settings.json"},
                         root=str(tmp_path), popen=lambda *a, **k: None, killer=lambda pid: None)
    before = dict(shared.env_overrides)
    run_client = cr.client_for_snapshot(shared, tmp_path / "run" / "settings.json")
    assert run_client is not shared
    assert run_client.env_overrides["VIDEOSUITE_SETTINGS"] == str(tmp_path / "run" / "settings.json")
    assert run_client.argv_prefix == shared.argv_prefix and run_client.root == shared.root
    assert shared.env_overrides == before
    assert cr.client_for_snapshot(shared, None) is shared


def test_run_store_lists_active_and_bounds_finished_runs(seams):
    client = FakeSuite()
    first = planned(request("overall", "quick"), client)
    assert [r["run_id"] for r in cr.store.list_active()] == [first]
    run_dirs = []
    for _ in range(cr.KEEP_FINISHED + 2):
        run_id, _ = run_to_end(request("overall", "quick"), client)
        run_dirs.append(Path(cr.store.get(run_id)["reports_dir"]).parent if cr.store.get(run_id) else None)
    assert cr.store.get(first) is not None                       # an awaiting plan is never purged
    finished = [r for r in cr.store.list_all() if r["stage"] in ("done", "error", "cancelled")]
    assert len(finished) <= cr.KEEP_FINISHED + 1
    assert not run_dirs[0].exists()                               # bounded cleanup removes its own folder only


@pytest.mark.parametrize('depth,trigger', [('quick', 'quick'), ('full', 'rank')])
def test_both_keeps_overall_if_settings_change_during_answer(seams, tmp_path, depth, trigger):
    settings = tmp_path / 'settings-owner.json'
    settings.write_text('{}')
    client = FakeSuite(settings_path=str(settings))
    def changed(kwargs):
        settings.write_text('{"changed":true}')
    client.hooks[trigger] = changed
    run_id, job_id = run_to_end(request('both', depth), client)
    result = cr.store.result(run_id)
    assert result['overall'] is not None and result['device'] is None
    assert result['coverage']['complete'] is True
    assert jobs_module.jobs.get(job_id)['error_kind'] == 'settings_changed'
    assert cc.validate_run_result(result) == result


def test_installed_build_identity_reaches_results_and_development_is_null(seams):
    cr.store.configure(build_id='release-build-123')
    run_id, _ = run_to_end(request('overall', 'quick'), FakeSuite())
    assert cr.store.result(run_id)['provenance']['build_id'] == 'release-build-123'
    cr.store.configure(build_id=None)
    run_id, _ = run_to_end(request('overall', 'quick'), FakeSuite())
    assert cr.store.result(run_id)['provenance']['build_id'] is None


def test_startup_cleanup_only_old_marked_direct_children_and_is_bounded(tmp_path):
    root = tmp_path / 'app'
    store = cr.RunStore()
    store.configure(data_root=root, clock=lambda: 1.0)
    marked = [store.create(request('overall', 'quick')) for _ in range(5)]
    runs = root / 'runs'
    foreign = runs / 'run-foreign'
    foreign.mkdir()
    (foreign / 'keep').write_text('not created by app')
    recent = runs / store.create(request('overall', 'quick'))
    marker = recent / cr.RUN_MARKER
    data = json.loads(marker.read_text())
    data['created_at'] = 40 * 86400
    marker.write_text(json.dumps(data))
    fresh = cr.RunStore()
    fresh.configure(data_root=root, clock=lambda: 40 * 86400)
    assert fresh.cleanup_orphans(limit=2) == 2
    assert sum((runs / r).exists() for r in marked) == 3
    assert foreign.exists() and recent.exists()
    assert fresh.cleanup_orphans() == 3
    assert foreign.exists() and recent.exists()


def test_cleanup_refuses_reparse_descendants_and_active_runs(tmp_path):
    import subprocess
    root = tmp_path / 'app'
    outside = tmp_path / 'outside'
    outside.mkdir(); (outside / 'keep').write_bytes(b'owner')
    store = cr.RunStore()
    store.configure(data_root=root, clock=lambda: 1.0)
    run_id = store.create(request('overall', 'quick'))
    folder = root / 'runs' / run_id
    link = folder / 'link'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        fresh = cr.RunStore()
        fresh.configure(data_root=root, clock=lambda: 40 * 86400)
        assert fresh.cleanup_orphans() == 0
        assert folder.exists() and (outside / 'keep').read_bytes() == b'owner'
        store.configure(clock=lambda: 40 * 86400)
        assert store.cleanup_orphans() == 0
    finally:
        link.rmdir()  # remove only our junction, never traverse it
    assert store.cleanup_orphans() == 0  # now a plain old tree, but still active in this store
    assert fresh.cleanup_orphans() == 1
    assert (outside / 'keep').read_bytes() == b'owner'


def test_both_does_not_keep_neutral_answer_after_engine_change(seams):
    client = FakeSuite()
    def changed(kwargs):
        client.version_doc['engine_tag'] = 'changed-engine'
    client.hooks['quick'] = changed
    run_id, _ = run_to_end(request('both', 'quick'), client)
    result = cr.store.result(run_id)
    assert result['overall'] is None and result['device'] is None
    assert result['coverage']['complete'] is False
