"""Bounded, nonfatal VideoSuite scoring of retained magnet probe files."""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

import suite_version
from suite_client import SuiteArgumentError, SuiteError, SuiteUnavailable, get_suite_client
from suite_routes import UNAVAILABLE_DETAIL

logger = logging.getLogger("video-analyzer.magnet-suite")
SUITE_MAGNET_MAX_FILES = 8
SUITE_MAGNET_BUDGET_S = 300.0
NEEDS_CAPABILITY = "score_partial"
TOO_OLD_MESSAGE = "The installed VideoSuite cannot score partial downloads (needs 0.4.0 or later); reinstall the Comparizer."


def _internal() -> Dict[str, Any]:
    return {"status": "error", "error_kind": "internal", "message": "VideoSuite scoring failed"}


def _never_raises(fn):
    @wraps(fn)
    def guarded(entries, emit, cancel_check, **kwargs):
        try:
            return fn(entries, emit, cancel_check, **kwargs)
        except Exception:
            logger.exception("VideoSuite partial scoring failed unexpectedly")
            return {entry["key"]: _internal() for entry in entries}
    return guarded


@_never_raises
def score_partials(entries: List[Dict[str, Any]], emit: Callable[[str], None],
                   cancel_check: Callable[[], bool], *, client_factory: Optional[Callable[[], Any]] = None,
                   clock: Callable[[], float] = time.monotonic) -> Dict[str, Dict[str, Any]]:
    start = clock()
    scores: Dict[str, Dict[str, Any]] = {}
    if not entries:
        return scores
    try:
        try:
            client = (client_factory or get_suite_client)()
        except SuiteUnavailable:
            return {entry["key"]: {"status": "unavailable", "message": UNAVAILABLE_DETAIL} for entry in entries}
        info = suite_version.ensure(client)
        if info.get("status") in ("error", "unchecked"):
            # The version check itself failed: the suite may be current, so do not tell the owner to reinstall.
            detail = info.get("message") or ""
            message = "VideoSuite version could not be checked" + (f": {detail}" if detail else "")
            return {entry["key"]: {"status": "error", "error_kind": "internal", "message": message}
                    for entry in entries}
        if NEEDS_CAPABILITY not in info["capabilities"]:
            return {entry["key"]: {"status": "unavailable", "message": TOO_OLD_MESSAGE,
                                   "tool_version": info.get("tool_version")} for entry in entries}
        for i, entry in enumerate(entries):
            try:
                if i >= SUITE_MAGNET_MAX_FILES:
                    result = {"status": "skipped", "message": "only the first 8 video files of a torrent are scored by VideoSuite"}
                elif cancel_check():
                    result = {"status": "skipped", "message": "cancelled"}
                elif clock() - start > SUITE_MAGNET_BUDGET_S:
                    result = {"status": "skipped", "message": "VideoSuite time budget (300 s) used up"}
                else:
                    emit(f"VideoSuite: scoring {entry['real_name']} ({i + 1}/{len(entries)})")
                    doc = client.score_partial(entry["path"], real_size=entry["real_size"],
                                               real_name=entry["real_name"], coverage=entry["coverage"], cancel=cancel_check)
                    if doc is None:
                        result = {"status": "skipped", "message": "cancelled"}
                    elif not doc["records"]:
                        result = {"status": "error", "error_kind": "contract", "message": "VideoSuite returned no record"}
                    else:
                        record = doc["records"][0]
                        result = {"status": "ok", "tool_version": doc["tool_version"], "partial": True,
                                  "record": record, "best_player": record.get("best_player", {})}
            except SuiteArgumentError as exc:
                result = {"status": "error", "error_kind": "usage", "message": str(exc)}
            except SuiteError as exc:
                result = {"status": "error", "error_kind": exc.kind, "message": exc.message}
            except Exception:
                logger.exception("VideoSuite partial scoring failed")
                result = _internal()
            scores[entry["key"]] = result
    except Exception:
        logger.exception("VideoSuite partial scoring setup failed")
        for entry in entries:
            scores.setdefault(entry["key"], _internal())
    return scores
