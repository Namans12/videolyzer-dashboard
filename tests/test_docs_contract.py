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
    "VIDEOSUITE_PYTHON", "VIDEOSUITE_ROOT", "VIDEOSUITE_COMPARE_TIMEOUT_S",
    "VIDEOLYZER_ALLOWED_ROOTS", "VIDEOLYZER_ALLOW_REMOTE_DRIVES",
    "VIDEOLYZER_SUITE_STARTUP_CHECK",
}


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


def test_readme_documents_the_suite_tab_and_frontend_tests():
    assert "## Suite tab" in README
    assert "npm test" in README
    assert "The core Suite routes require VideoSuite 0.3.0 or later; install 0.4.0 for owner settings, partial-file scoring and current progress features." in README
    assert "Evidence (weakest rule used): official = the maker's documentation; owner = tested by you; reviewed = a professional review; forum = user reports; inferred = our deduction; unknown = no information. A score range (for example 69-99) means the result is uncertain." in README
    testing = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    assert "npm test" in testing
    assert "The frontend has no automated tests." not in testing
