"""Request guard: a pure ASGI middleware that refuses requests a local web UI never makes.

The Dashboard listens on loopback and starts jobs, reads local paths and starts
torrents on a plain GET/POST. Without a guard any web page the owner visits could
drive it (CSRF, DNS rebinding). Rules, evaluated in this order (all matching is
``re.fullmatch`` and case-insensitive):

1. ``Host`` present and not loopback (``localhost``, ``127.0.0.1``, ``[::1]``,
   optional ``:port``)                                          -> 400
2. ``Origin`` present: must be an ``http(s)`` loopback origin (``Origin: null``
   included in the refusals)                                    -> 403
   An allowed Origin makes ``Sec-Fetch-Site`` irrelevant: browsers label the UI on
   ``localhost:5173`` calling ``127.0.0.1:8000`` as ``cross-site``.
3. ``Origin`` absent but ``Sec-Fetch-Site`` present (``<img>``, ``<link>``,
   no-cors GET, navigations from other pages): only ``same-origin`` and ``none``
   are allowed                                                  -> 403
4. Neither header (curl, the suite, tests)                      -> allowed
5. ``POST`` to ``/magnet/``, ``/compare-magnets/`` or anything under ``/suite/``
   must be ``application/json`` (parameters ignored)            -> 415
   The no-body cancel routes are exempt (the UI sends no Content-Type).

It must stay OUTSIDE ``MaxBodySizeMiddleware`` and INSIDE ``CORSMiddleware`` so
preflights are answered by CORS and the refusals for an allowed origin still carry
CORS headers.
"""
from __future__ import annotations

import re

from starlette.responses import JSONResponse

_LOOPBACK = r"(localhost|127\.0\.0\.1|\[::1\])"
HOST_RE = re.compile(_LOOPBACK + r"(:\d+)?", re.IGNORECASE)
ORIGIN_RE = re.compile(r"https?://" + _LOOPBACK + r"(:\d+)?", re.IGNORECASE)
SAFE_FETCH_SITES = {"same-origin", "none"}

JSON_ONLY_EXACT_PATHS = {"/magnet/", "/compare-magnets/"}
JSON_ONLY_PREFIXES = ("/suite/",)


def _requires_json(method: str, path: str) -> bool:
    if method != "POST":
        return False
    return path in JSON_ONLY_EXACT_PATHS or path.startswith(JSON_ONLY_PREFIXES)


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestGuard:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        host = _header(headers, b"host")
        origin = _header(headers, b"origin")
        fetch_site = _header(headers, b"sec-fetch-site")

        deny: tuple[int, str] | None = None
        if host is not None and not HOST_RE.fullmatch(host.strip()):
            deny = (400, "host not allowed")
        elif origin is not None:
            if not ORIGIN_RE.fullmatch(origin.strip()):
                deny = (403, "origin not allowed")
        elif fetch_site is not None and fetch_site.strip().lower() not in SAFE_FETCH_SITES:
            deny = (403, "cross-site request refused")

        if deny is None and _requires_json(scope.get("method", ""), scope.get("path", "")):
            content_type = (_header(headers, b"content-type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                deny = (415, "Content-Type must be application/json")

        if deny is not None:
            response = JSONResponse({"detail": deny[1]}, status_code=deny[0])
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
