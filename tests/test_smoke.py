import importlib

import pytest

EXPECTED_ROUTES = {
    "/health/",
    "/analysis/",
    "/analyze-multiple/",
    "/analyze-path/",
    "/scan-folder/",
    "/magnet/",
    "/magnet/{job_id}/cancel",
    "/compare-magnets/",
    "/compare-magnets/{job_id}/cancel",
    "/job/{job_id}",
    "/job/{job_id}/events",
    "/job/{job_id}/cancel",
}
# Added by D11 (suite_routes.py); the twelve routes above keep their paths and methods.
SUITE_ROUTES = {
    "/suite/version",
    "/suite/devices",
    "/suite/settings",
    "/suite/score",
    "/suite/rank",
    "/suite/compare",
}
# Added by Plan 4A task D2 (calibration_routes.py): exactly these eight.
CALIBRATION_ROUTES = {
    "/suite/calibration/plan",
    "/suite/calibration/status",
    "/suite/calibration/session",
    "/suite/calibration/record",
    "/suite/calibration/build",
    "/suite/calibration/apply",
    "/suite/calibration/undo",
    "/suite/calibration/report",
}
FRAMEWORK_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_main_imports():
    import main  # noqa: F401


def test_app_has_its_twelve_own_routes():
    import main

    paths = set()
    for r in main.app.routes:
        inner = getattr(r, "original_router", None)     # include_router() entries (FastAPI >= 0.143)
        if inner is not None:
            paths |= {r.include_context.prefix + sub.path for sub in inner.routes}   # sub.path has the router prefix
        else:
            paths.add(getattr(r, "path", None))
    paths -= FRAMEWORK_ROUTES
    assert paths - SUITE_ROUTES - CALIBRATION_ROUTES == EXPECTED_ROUTES
    assert SUITE_ROUTES <= paths
    assert CALIBRATION_ROUTES <= paths
    assert paths == EXPECTED_ROUTES | SUITE_ROUTES | CALIBRATION_ROUTES      # exactly the new routes, nothing else
    assert len(EXPECTED_ROUTES) == 12 and len(CALIBRATION_ROUTES) == 8


@pytest.mark.parametrize(
    "name",
    ["analysis", "comparison", "verification", "filename_intel", "series_compare",
     "hdr10plus_scan"],
)
def test_pure_modules_import(name):
    assert importlib.import_module(name) is not None


def test_magnet_module_imports_without_libtorrent():
    import magnet

    assert hasattr(magnet, "MagnetUnavailable")
