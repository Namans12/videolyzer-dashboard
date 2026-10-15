"""VideoSuite version handshake."""
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from suite_client import SuiteError, SuiteUnavailable, get_suite_client

MIN_SUITE_VERSION = "0.3.0"
CACHE_SECONDS = 300.0
FAILURE_CACHE_SECONDS = 10.0
ENV_STARTUP_CHECK = "VIDEOLYZER_SUITE_STARTUP_CHECK"
REINSTALL_HINT = "Reinstall the Comparizer (run the latest Video Comparator installer), then restart the Dashboard."
_now = time.monotonic
logger = logging.getLogger("video-analyzer.suite-version")
_lock = threading.Lock()
_stored: Optional[Dict[str, Any]] = None
_stored_at = 0.0


def parse_version(text: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(text, str):
        return None
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.groups())
    except ValueError:
        return None


def check(client: Any) -> Dict[str, Any]:
    """Return a handshake result, including for legacy clients and failed calls."""
    result = {"status": "error", "tool_version": None, "minimum": MIN_SUITE_VERSION,
              "capabilities": [], "engine_tag": None, "schemas": {}, "message": "",
              "checked_at": _now()}
    try:
        if isinstance(client, _DiscoveryFailure):
            raise client.error
        if not hasattr(client, "version"):
            result.update(status="unchecked", message="this client cannot report a version")
            return result
        fallback = False
        try:
            doc = client.version()
        except SuiteError as exc:
            if exc.kind != "usage":
                raise
            fallback = True
            try:
                doc = client.devices()
            except SuiteError as fallback_exc:
                if fallback_exc.kind != "usage":
                    raise
                result.update(status="too_old", message=(
                    f"The installed VideoSuite is older than {MIN_SUITE_VERSION} "
                    f"(it has no 'devices' command). {REINSTALL_HINT}"))
                return result
        if not isinstance(doc, dict):
            raise SuiteError("contract", "invalid version document")
        version = doc.get("tool_version")
        capabilities = ["devices"] if fallback else doc.get("capabilities", [])
        schemas = {} if fallback else doc.get("schemas", {})
        engine_tag = None if fallback else doc.get("engine_tag")
        if (version is not None and not isinstance(version, str)
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)
                or not isinstance(schemas, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in schemas.items())
                or engine_tag is not None and not isinstance(engine_tag, str)):
            raise SuiteError("contract", "invalid version metadata")
        # Copy validated fields into plain containers; do not cache client-owned
        # objects that could later mutate into a non-JSON response.
        result.update(tool_version=version,
                      capabilities=list(capabilities), schemas=dict(schemas), engine_tag=engine_tag)
        parsed = parse_version(version)
        if parsed is None:
            result["message"] = f"VideoSuite reported an unreadable version {version!r}."
        elif parsed < parse_version(MIN_SUITE_VERSION):
            result.update(status="too_old", message=(
                f"The installed VideoSuite is {version}; the Dashboard needs {MIN_SUITE_VERSION} "
                f"or later. {REINSTALL_HINT}"))
        else:
            message = f"VideoSuite {version}"
            if fallback:
                message += " (no 'version' command; detected through 'devices')"
            result.update(status="ok", message=message)
    except (SuiteUnavailable, SuiteError) as exc:
        if isinstance(exc, SuiteUnavailable) or exc.kind == "unavailable":
            # Imported at call time: suite_routes imports this module too.
            from suite_routes import UNAVAILABLE_DETAIL
            result.update(status="unavailable", message=UNAVAILABLE_DETAIL)
        else:
            result["message"] = f"VideoSuite did not answer the version check ({exc.kind})."
    except Exception:
        result["message"] = "VideoSuite did not answer the version check (internal)."
    return result


def ensure(client: Any, refresh: bool = False) -> Dict[str, Any]:
    """Serialize checks and retain definitive results longer than transient failures."""
    global _stored, _stored_at
    with _lock:
        if _stored is not None and not refresh:
            ttl = CACHE_SECONDS if _stored["status"] in ("ok", "too_old") else FAILURE_CACHE_SECONDS
            if _now() - _stored_at < ttl:
                return _stored
        _stored = check(client)
        _stored_at = _now()
        return _stored


def cached() -> Optional[Dict[str, Any]]:
    """Return the last stored result, even after its TTL expires."""
    with _lock:
        return _stored


def reset() -> None:
    global _stored, _stored_at
    with _lock:
        _stored = None
        _stored_at = 0.0


class _DiscoveryFailure:
    """Retain a discovery failure for check(), without treating usage as legacy."""
    def __init__(self, error: Exception):
        self.error = error


def _discover(client_factory: Callable[[], Any]) -> Any:
    """Feed every discovery exception through the ordinary short failure cache."""
    try:
        return client_factory()
    except Exception as exc:
        return _DiscoveryFailure(exc)


def start_background_check(
    client_factory: Callable[[], Any] = get_suite_client,
) -> Optional[threading.Thread]:
    def run():
        result = ensure(_discover(client_factory))
        if result["status"] != "ok":
            logger.warning(result["message"])

    thread = threading.Thread(target=run, name="suite-version-check", daemon=True)
    thread.start()
    return thread
