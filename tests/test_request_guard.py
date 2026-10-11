"""Request guard (plan 2, D2): Host, Origin, Fetch Metadata and Content-Type rules.

Browsers label ``http://localhost:5173`` -> ``http://127.0.0.1:8000`` as
``cross-site`` and send ``Origin`` on every fetch, so an allowed Origin makes
``Sec-Fetch-Site`` irrelevant. A no-cors ``<img>``/GET carries no Origin but does
carry ``Sec-Fetch-Site``.
"""
from __future__ import annotations

import io
import os

import pytest

import main

UI = "http://localhost:5173"


def cors_origin(resp):
    return resp.headers.get("access-control-allow-origin")


# ── Origin / Sec-Fetch-Site ──────────────────────────────────────────────────

def test_ui_origin_cross_site_is_allowed_and_cors_echoed(client):
    r = client.get("/health/", headers={"Origin": UI, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert cors_origin(r) == UI


def test_loopback_ip_origin_same_site_is_allowed(client):
    o = "http://127.0.0.1:5173"
    r = client.get("/health/", headers={"Origin": o, "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 200
    assert cors_origin(r) == o


def test_ipv6_loopback_origin_is_allowed_by_both_layers(client):
    o = "http://[::1]:5173"
    r = client.get("/health/", headers={"Origin": o, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert cors_origin(r) == o


def test_preflight_from_ui_is_answered_by_cors(client):
    r = client.options("/magnet/", headers={
        "Origin": UI,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
        "Sec-Fetch-Site": "cross-site",
    })
    assert r.status_code == 200
    assert cors_origin(r) == UI


def test_evil_origin_is_forbidden(client):
    r = client.get("/health/", headers={"Origin": "https://evil.example",
                                        "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert r.json() == {"detail": "origin not allowed"}
    assert cors_origin(r) is None


def test_lookalike_origins_are_forbidden(client):
    for o in ("http://localhost.evil.example", "http://localhost:5173.evil.example",
              "http://evil.example/localhost", "http://127.0.0.1.evil.example",
              "ftp://localhost", "http://localhost:abc"):
        r = client.get("/health/", headers={"Origin": o})
        assert r.status_code == 403, o


def test_origin_null_is_forbidden(client):
    r = client.get("/health/", headers={"Origin": "null"})
    assert r.status_code == 403
    assert r.json() == {"detail": "origin not allowed"}


def test_origin_match_is_case_insensitive(client):
    r = client.get("/health/", headers={"Origin": "HTTP://LOCALHOST:5173"})
    assert r.status_code == 200


def test_no_origin_cross_site_fetch_metadata_is_refused_before_path_logic(client, monkeypatch):
    called = []
    monkeypatch.setattr(main.os.path, "isfile", lambda p: called.append(p) or False)
    monkeypatch.setattr(main.os.path, "isdir", lambda p: called.append(p) or False)
    r = client.get("/analyze-path/", params={"path": r"C:\x"},
                   headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site request refused"}
    assert called == []
    assert len(main._jobs) == 0


def test_no_origin_same_site_fetch_metadata_is_refused(client):
    r = client.get("/health/", headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("value", ["same-origin", "none", "SAME-ORIGIN"])
def test_no_origin_same_origin_or_user_navigation_is_allowed(client, value):
    r = client.get("/health/", headers={"Sec-Fetch-Site": value})
    assert r.status_code == 200


def test_no_headers_passes(client):
    assert client.get("/health/").status_code == 200


def test_guard_rejection_for_allowed_origin_carries_cors_headers(client):
    r = client.post("/magnet/", content="{}",
                    headers={"Content-Type": "text/plain", "Origin": UI})
    assert r.status_code == 415
    assert cors_origin(r) == UI


# ── Host ─────────────────────────────────────────────────────────────────────

def test_foreign_host_is_rejected(client):
    r = client.get("/health/", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert r.json() == {"detail": "host not allowed"}


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:8000", "127.0.0.1:8000",
                                  "127.0.0.1", "[::1]:8000", "[::1]"])
def test_loopback_hosts_are_allowed(client, host):
    assert client.get("/health/", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["localhost.evil.example", "evil.example:8000",
                                  "localhost:80a", "127.0.0.1.evil.example",
                                  "localhost@evil.example"])
def test_lookalike_hosts_are_rejected(client, host):
    assert client.get("/health/", headers={"Host": host}).status_code == 400


def test_host_rule_is_evaluated_before_origin(client):
    r = client.get("/health/", headers={"Host": "evil.example", "Origin": UI})
    assert r.status_code == 400


# ── Content-Type ─────────────────────────────────────────────────────────────

def test_magnet_post_text_plain_is_415_and_creates_no_job(client):
    r = client.post("/magnet/", content='{"magnet": "magnet:?xt=urn:btih:abc"}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert len(main._jobs) == 0


def test_magnet_post_without_content_type_is_415(client):
    r = client.post("/magnet/", content=b"")
    assert r.status_code == 415
    assert len(main._jobs) == 0


def test_compare_magnets_post_form_encoded_is_415(client):
    r = client.post("/compare-magnets/", data={"magnets": "x"})
    assert r.status_code == 415
    assert len(main._jobs) == 0


def test_suite_prefix_post_requires_json(client):
    # No /suite/ route exists yet (D11); the guard answers before routing.
    r = client.post("/suite/anything", content="x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = client.post("/suite/anything", json={})
    assert r.status_code != 415


def test_json_content_type_parameters_are_ignored(client, monkeypatch):
    monkeypatch.setattr(main, "run_magnet_job_threaded",
                        lambda *a, **k: {"analyses": [], "files": []})
    r = client.post("/magnet/", content='{"magnet": "magnet:?xt=urn:btih:abc"}',
                    headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_json_rule_does_not_apply_to_get(client):
    assert client.get("/job/nope").status_code == 404


@pytest.mark.parametrize("path", ["/magnet/abc/cancel", "/compare-magnets/abc/cancel",
                                  "/job/abc/cancel"])
def test_cancel_posts_without_content_type_reach_the_route(client, path):
    r = client.post(path, headers={"Origin": UI, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 404           # route reached (unknown job), not 415
    assert cors_origin(r) == UI


def test_cancel_post_of_known_job_without_content_type_succeeds(client):
    jid = main._jobs.create("magnet")
    r = client.post(f"/magnet/{jid}/cancel", headers={"Origin": UI})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_multipart_upload_still_reaches_the_handler(client, monkeypatch, upload_dir):
    monkeypatch.setattr(main, "analyze_file",
                        lambda p, skip_dovi_scan=False: {"file": os.path.basename(p),
                                                         "path": p, "tv_score": 1})
    r = client.post("/analysis/?fast=true",
                    files={"file": ("a.mkv", io.BytesIO(b"x" * 100), "video/x-matroska")},
                    headers={"Origin": UI, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert main._jobs.get(job_id)["status"] == "done"
    assert main._jobs.get(job_id)["results"][0]["file"] == "a.mkv"
    assert os.listdir(upload_dir) == []


# ── Structure ────────────────────────────────────────────────────────────────

def test_guard_is_pure_asgi_and_cors_is_outermost():
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.cors import CORSMiddleware

    import request_guard

    assert not issubclass(request_guard.RequestGuard, BaseHTTPMiddleware)
    names = [m.cls.__name__ for m in main.app.user_middleware]
    # user_middleware is ordered outermost first
    assert names[0] == CORSMiddleware.__name__
    assert names.index("RequestGuard") < names.index("MaxBodySizeMiddleware")


def test_non_http_scopes_pass_through():
    import asyncio

    import request_guard

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    guard = request_guard.RequestGuard(inner)
    asyncio.run(guard({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
