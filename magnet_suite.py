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


def _partial_stop(cancel_check, clock, start):
    if cancel_check():
        raise SuiteError('failed', 'cancelled')
    if clock() - start >= SUITE_MAGNET_BUDGET_S:
        raise SuiteError('timeout', 'partial-suite time budget (300 s) used up')


def partial_facts(entries, emit, cancel_check, *, client, clock=time.monotonic, start=None):
    """Detach facts while probes still exist. No catalogue, legacy scorer or ranking."""
    start = clock() if start is None else start
    bounded_cancel = lambda: cancel_check() or clock() - start >= SUITE_MAGNET_BUDGET_S
    detached = []
    for i, entry in enumerate(entries):
        row = dict(entry)
        try:
            if i >= SUITE_MAGNET_MAX_FILES:
                raise SuiteError('failed', 'only the first 8 retained probes are read')
            _partial_stop(cancel_check, clock, start)
            emit(f"Reading partial facts: {entry['real_name']}")
            doc = client.facts_partial(entry['path'], real_size=entry['real_size'], real_name=entry['real_name'],
                                       coverage=entry['coverage'], cancel=bounded_cancel)
            if doc is None:
                _partial_stop(cancel_check, clock, start)
                raise SuiteError('failed', 'cancelled')
            if len(doc['records']) != 1 or not doc['records'][0]['source'].get('partial'):
                raise SuiteError("contract", "VideoSuite returned no partial record")
            row['record'] = doc['records'][0]
        except SuiteUnavailable:
            row['error'] = dict(status='unavailable', error_kind='unavailable', message=UNAVAILABLE_DETAIL)
        except Exception as exc:
            row['error'] = dict(status='error', error_kind=getattr(exc, 'kind', 'probe'),
                                message=getattr(exc, 'message', 'Could not read partial facts'))
        detached.append(row)
    return detached


def partial_snapshot(entries):
    """Candidate identity describes retained evidence, never a full-file stat claim."""
    from comparison_contracts import records_digest
    records, candidates = [], []
    for row in entries:
        r = row.get('record')
        if r is not None:
            records.append(r)
            candidates.append(dict(path=r['source']['path'], id=r['id'], status='ready', reason=None,
                                   size=r['source'].get('probed_bytes', 0), mtime_ns=0))
        else:
            candidates.append(dict(path=row['path'], id=None, status='unreadable',
                                   reason=row.get('error', {}).get('message', 'Could not read partial facts'),
                                   size=None, mtime_ns=None))
    version = records[0]['facts_version'] if records else 2
    return dict(schema='videosuite-record-snapshot/1', facts_version=version, records=records,
                candidates=candidates, records_digest=records_digest(records, candidates, version))


def recommend_partials(entries: List[dict], emit: Callable[[str], None], cancel_check: Callable[[], bool], *,
                       results: str, device: Optional[str], primary_player: Optional[str], player: Optional[str],
                       fs: Optional[str], lang: str, audio_output: Optional[str], client_factory=None,
                       clock=time.monotonic) -> Dict[str, dict]:
    """One honest Quick recommendation over detached facts, with explicit per-candidate failures.

    Already detached entries let the multi-link worker finish every retained-probe callback
    before calling Quick exactly once. Missing suite never falls back to legacy TV scoring.
    """
    start = clock()
    bounded_cancel = lambda: cancel_check() or clock() - start >= SUITE_MAGNET_BUDGET_S
    try:
        client = (client_factory or get_suite_client)()
    except SuiteUnavailable:
        return {e['key']: dict(status='unavailable', record=e.get('record'), recommendation=None,
                               message=UNAVAILABLE_DETAIL) for e in entries}
    rows = []
    for row in entries[:SUITE_MAGNET_MAX_FILES]:
        rows.extend([row] if 'record' in row or 'error' in row else partial_facts(
            [row], emit, cancel_check, client=client, clock=clock, start=start))
    doc, error = None, None
    try:
        _partial_stop(cancel_check, clock, start)
        snapshot = partial_snapshot(rows)
        if snapshot['records']:
            doc = client.quick(snapshot, results=results, device=device, primary_player=primary_player,
                               player=player, fs=fs, lang=lang, audio_output=audio_output, cancel=bounded_cancel)
            if doc is None:
                _partial_stop(cancel_check, clock, start)
                raise SuiteError('failed', 'cancelled')
    except SuiteUnavailable:
        error = dict(status='unavailable', message=UNAVAILABLE_DETAIL)
    except Exception as exc:
        error = dict(status='error', error_kind=getattr(exc, 'kind', 'internal'),
                     message=getattr(exc, 'message', 'VideoSuite recommendation failed'))
    return {r['key']: dict(r.get('error') or error or {'status': 'ok'}, record=r.get('record'),
                           recommendation=doc if 'error' not in r else None) for r in rows}
