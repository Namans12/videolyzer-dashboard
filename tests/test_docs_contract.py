"""Docs contract (plan 2, task D12): the README must not fall behind the code.

* every route the app itself defines (``app.routes`` without the framework's docs
  routes, with the lazily included ``/suite`` router flattened) is mentioned in README.md;
* every environment variable the top-level ``*.py`` files read is documented in README.md.

An environment variable counts as read when the AST shows ``os.environ.get(X)``,
``os.environ[X]`` or ``os.getenv(X)``, where ``X`` is a string literal or a module-level
string constant (``ENV_ALLOWED_ROOTS = "VIDEOLYZER_ALLOWED_ROOTS"``), or when a module-level
constant named ``ENV_*`` holds a string (``suite_client`` reads its variables through an
injectable mapping, not through ``os.environ`` directly). Only the repo root is scanned
(``Path.glob("*.py")``): never ``.venv``, ``tests`` or ``scripts``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

FRAMEWORK_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
KNOWN_ENV_VARS = {
    "VIDEOLYZER_DATA_ROOT", "LOCALAPPDATA",
    "VIDEOSUITE_PYTHON", "VIDEOSUITE_ROOT", "VIDEOSUITE_COMPARE_TIMEOUT_S",
    "VIDEOLYZER_ALLOWED_ROOTS", "VIDEOLYZER_ALLOW_REMOTE_DRIVES",
    "VIDEOLYZER_SUITE_STARTUP_CHECK",
}


def test_rollback_example_is_fresh_shell_safe():
    block = next(b for b in re.findall(r'```powershell\n(.*?)\n```', README, re.S)
                 if '--expected-previous' in b)
    for text in ('VIDEOLYZER_DATA_ROOT', '[guid]::NewGuid()', 'try {', 'finally {',
                 'Remove-Item -LiteralPath', '$savedDataRoot'):
        assert text in block


def own_route_paths() -> set[str]:
    import main

    paths: set[str] = set()
    for r in main.app.routes:
        inner = getattr(r, "original_router", None)      # include_router() entries (FastAPI >= 0.143)
        if inner is not None:
            paths |= {r.include_context.prefix + sub.path for sub in inner.routes}
        else:
            paths.add(getattr(r, "path", None))
    paths.discard(None)
    return paths - FRAMEWORK_ROUTES


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _string_constants(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    return consts


def env_vars_read(root: Path = ROOT) -> dict[str, set[str]]:
    """``{variable name: {files that read it}}`` for the top-level ``*.py`` files of ``root``."""
    found: dict[str, set[str]] = {}

    def add(name: str, file: str) -> None:
        found.setdefault(name, set()).add(file)

    for py in sorted(root.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        consts = _string_constants(tree)

        def resolve(arg: ast.AST) -> str | None:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            if isinstance(arg, ast.Name):
                return consts.get(arg.id)
            return None

        for name, value in consts.items():
            if name.startswith("ENV_"):
                add(value, py.name)
        for node in ast.walk(tree):
            arg = None
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
                    arg = node.args[0]
                elif isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(func.value, ast.Name) \
                        and func.value.id == "os":
                    arg = node.args[0]
            elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                arg = node.slice
            if arg is not None:
                name = resolve(arg)
                if name:
                    add(name, py.name)
    return found


def test_scanner_finds_the_known_variables_and_stays_in_the_repo_root():
    found = env_vars_read()
    assert KNOWN_ENV_VARS <= set(found)
    scanned = {f for files in found.values() for f in files}
    assert all("/" not in f and "\\" not in f for f in scanned)      # file names only, nothing from a sub-folder


def test_every_own_route_is_mentioned_in_the_readme():
    missing = []
    for path in sorted(own_route_paths()):
        # the path must not be only a prefix of a longer documented path (/job/{job_id} vs /job/{job_id}/events)
        if not re.search(re.escape(path) + r"(?![\w{}/-])", README):
            missing.append(path)
    assert not missing, f"routes missing from README.md: {missing}"
    for text in ('/app/status', 'preflight', 'required Full-engine check', 'release.json'):
        assert text in README, text


def test_app_docs_describe_quit_offline_and_rollback():
    for text in ('Keep running', 'Quit when finished', 'Stop jobs and quit',
                 'You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.',
                 '<I>\\python\\pythonw.exe', '<I>\\runtimes\\<lock_sha256>\\suite',
                 '<I>\\runtimes\\<lock_sha256>\\dashboard', 'no runtime npm',
                 'tools\\activate_app_release.py', '--expected-previous',
                 'install-bootstrap-', 'owner-only', 'Job Object'):
        assert text in README, text
    packaged = README.split('## Offline app launch and rollback', 1)[1].split('\n## ', 1)[0]
    assert 'npm install' not in packaged and 'pip install' not in packaged
    for name in ('ARCHITECTURE', 'TESTING', 'SUITE_INTEGRATION'):
        text = (ROOT / 'docs' / (name + '.md')).read_text('utf-8')
        assert 'offline_payload' in text and 'release.json' in text, name


def test_every_environment_variable_read_is_documented_in_the_readme():
    missing = sorted(name for name in env_vars_read()
                     if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", README))
    assert not missing, f"environment variables missing from README.md: {missing}"


def test_readme_marks_dashboard_scoring_as_frozen_legacy():
    heading = "## Legacy scoring (frozen)"
    assert heading in README
    section = README.split(heading, 1)[1].split("\n## ", 1)[0]
    for field in ("tv_score", "composite_score", "confidence_score"):
        assert field in section


def test_every_suite_route_is_mentioned_in_the_integration_contract():
    contract = (ROOT / "docs" / "SUITE_INTEGRATION.md").read_text(encoding="utf-8")
    missing = sorted(path for path in own_route_paths() if path.startswith("/suite/")
                     and not re.search(re.escape(path) + r"(?![\w{}/-])", contract))
    assert not missing, f"routes missing from docs/SUITE_INTEGRATION.md: {missing}"


def test_settings_route_is_registered_and_documented():
    assert "/suite/settings" in own_route_paths()
    contract = (ROOT / "docs" / "SUITE_INTEGRATION.md").read_text(encoding="utf-8")
    for text in (README, contract):
        assert "/suite/settings" in text
        assert "five `/suite/...` routes" not in text


CALIBRATION_ROUTES = ("plan", "status", "session", "record", "build", "apply", "undo", "report")


def test_calibration_routes_are_registered_and_documented_in_both_places():
    contract = (ROOT / "docs" / "SUITE_INTEGRATION.md").read_text(encoding="utf-8")
    registered = own_route_paths()
    for name in CALIBRATION_ROUTES:
        path = f"/suite/calibration/{name}"
        assert path in registered
        for text in (README, contract):
            assert re.search(re.escape(path) + r"(?![\w{}/-])", text), path
    for text in (README, contract):
        assert "six `/suite/...` routes" not in text
        assert "fourteen `/suite/...` routes" in text


def test_readme_documents_the_suite_tab_and_frontend_tests():
    assert "## Suite tab" in README
    assert "npm test" in README
    assert "The core Suite routes require VideoSuite 0.3.0 or later; install 0.4.0 for owner settings, partial-file scoring and current progress features." in README
    assert "Evidence (weakest rule used): official = the maker's documentation; owner = tested by you; reviewed = a professional review; forum = user reports; inferred = our deduction; unknown = no information. A score range (for example 69-99) means the result is uncertain." in README
    testing = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    assert "npm test" in testing
    assert "The frontend has no automated tests." not in testing


def test_calibration_workflow_gate_and_limits_are_documented():
    section = README.split("## Calibration (TV)", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "second tab", "calibration` capability", "update VideoSuite to 0.5.0",
        "Suite view under Advanced keeps working", "Prepare kit", "Confirm conditions",
        "checklist table", "guided card", "Preview changes", "Apply", "Undo this kit",
        "What changed", "playback only", "500 characters", "100 characters", "single-line",
        "link or junction", "`&` or `%`", "before its first session",
    ):
        assert phrase in section, phrase
    assert "Node.js 22.22.2 or newer" in README


def test_calibration_modules_storage_and_test_coverage_are_documented():
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    integration = (ROOT / "docs" / "SUITE_INTEGRATION.md").read_text(encoding="utf-8")
    testing = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    for module in (
        "CalibrationScreen.tsx", "KitPanel.tsx", "ConditionsBar.tsx", "ChecklistTable.tsx",
        "GuidedCard.tsx", "ApplyPanel.tsx", "useCalibration.ts", "types.ts", "api.ts",
        "messages.ts", "calibration.css",
    ):
        assert f"`{module}`" in architecture, module
    assert "src/suite/calibration/" in architecture
    assert "calibration_routes.py" in architecture
    for text in (README, integration):
        for name in ("calibration.json", "settings.json", "calibration.lock", "settings.json.bak"):
            assert f"`{name}`" in text, name
    for phrase in ("rule_overrides", "read-only", "single-line", "500 characters", "100 characters"):
        assert phrase in integration, phrase
    for phrase in ("422 tests in 30 files", "checklist", "guided", "stale", "capability"):
        assert phrase in testing, phrase


SIX_MODES = ("Quick + Overall", "Quick + Device", "Quick + Both", "Full + Overall", "Full + Device", "Full + Both")
FIXED_DEVICES = (("sony_bravia_8_ii", "Sony Bravia 8 II (TV)"), ("vivobook_pro_16x", "ASUS Vivobook Pro 16X (laptop)"),
                 ("galaxy_tab_s9_ultra", "Galaxy Tab S9 Ultra (tablet)"))
ONE_TITLE = ("Add versions of one movie/title only. If you mixed titles or episodes, remove them and start a "
             "separate run for each title.")


def _section(text: str, heading: str) -> str:
    assert heading in text, heading
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_docs_pin_six_modes_and_fixed_device_scope():
    raw = _section(README, "## Compare (home screen)")
    section = " ".join(raw.split())
    rows = [line for line in raw.splitlines() if line.startswith("| ") and " + " in line.split("|")[1]]
    assert [row.split("|")[1].strip() for row in rows] == list(SIX_MODES)
    for text in ("Compare, Calibration (TV), Devices info, Advanced", "Metadata estimate — picture not measured",
                 "Best playback estimate for [device/player] — picture not measured", "accurate",
                 "every unordered pair", "no playback shortlist", "no whole-set winner", ONE_TITLE,
                 "Download the files first, then choose Downloaded files.", "Check files and estimate",
                 "You can close this tab. Reopen the shortcut to return. Use Quit to stop the app.",
                 "Best overall", "Best for my device", "Quick (metadata)", "Full (picture comparison)",
                 "owner = tested by Naman", "settings_changed", "not resumed halfway through a pair",
                 "/app/choose-files", '<iframe sandbox="">', "?download=1", "Devices info", "read-only",
                 "Analyze", "Suite (all devices)", "fast shortlist", "no runtime downloads", "active_tab"):
        assert text in section, text
    positions = [section.index(device_id) for device_id, _ in FIXED_DEVICES]
    assert positions == sorted(positions)
    for _, label in FIXED_DEVICES:
        assert label in section, label
    assert "no add, remove, restore or reorder" in section
    lowered = section.lower()
    for claim in ("add a device", "add a new device", "remove a device", "manage your devices", "device manager"):
        assert claim not in lowered, claim
    assert "Magnet runs answer 501" not in README           # magnets landed in U6
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for module in ("CompareScreen.tsx", "InputPanel.tsx", "RunOptions.tsx", "useComparison.ts", "ComparisonResult.tsx",
                   "FactsTable.tsx", "ReportPanel.tsx", "supported-devices.json", "DevicesInfo.tsx"):
        assert f"`{module}`" in architecture, module
    assert "src/compare/" in architecture and "src/devices/" in architecture
    integration = (ROOT / "docs" / "SUITE_INTEGRATION.md").read_text(encoding="utf-8")
    unified = " ".join(_section(integration, "## Unified comparison runs (Plan 4B)").split())
    for text in ("/comparison/plans", "/comparison/runs", "videosuite-comparison-run/1", "videosuite-quick/1",
                 "--primary-player", "reports_dir", "settings.path", "build_id"):
        assert text in unified, text
    testing = " ".join((ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8").split())
    for text in ("src/compare/testing/fixtures.ts", "tests/fixtures/comparison", "PLAN4B_REGEN_FIXTURES=1",
                 "never fabricated near-matches"):
        assert text in testing, text


def test_unified_capability_gate_and_real_fixture_check_are_documented():
    section = README.split("## Unified comparison contracts (Plan 4B)", 1)[1].split("\n## ", 1)[0]
    for text in ("VideoSuite 0.7.0", "compare_all", "compare_plan", "neutral_only", "record_snapshot",
                 "quick_recommendations", "Advanced and Calibration keep working",
                 "scripts/make_suite_fixtures.py --plan4b --check", "tests/fixtures/suite4b",
                 "two independent generations", "semantic mutation", "--primary-player", "--player all",
                 "videosuite-record-snapshot/1", "1225 pairs", "never an ETA", "reports_dir"):
        assert text in section, text


def test_final_backend_retention_swarm_and_independent_overall_are_documented():
    testing = (ROOT / 'docs' / 'TESTING.md').read_text(encoding='utf-8')
    integration = (ROOT / 'docs' / 'SUITE_INTEGRATION.md').read_text(encoding='utf-8')
    for text in ('PLAN4B_LOOPBACK_PYTHON', 'test_private_loopback_swarm.py', '127.0.0.1', 'private'):
        assert text in testing
    for text in ('30 days', '100', '.videosuite-run.json', 'release.json', 'Overall answer'):
        assert text in README, text
    assert 'settings.path' in integration and 'cancel' in integration
