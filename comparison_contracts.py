"""Plan 4B unified comparison contracts: requests, run snapshots, run results and capability gates.

* ``RunRequest`` -- what the browser asks for (extra keys forbidden, contradictions refused, never rewritten).
* ``InternalRunSnapshot`` -- the backend-only run state; it REQUIRES ``reports_dir`` (absolute, run-owned).
* ``PublicRunSnapshot`` -- the HTTP/frontend shape; exactly the internal fields minus ``reports_dir``, which is
  forbidden (not null, not empty, not redacted). ``to_public_snapshot`` projects an allowlist explicitly.
* ``RunResult`` (``videosuite-comparison-run/1``) -- nested suite documents are the untouched real documents and
  are validated with the same deep validators as the CLI output (additive suite keys are tolerated).

Validation errors are ``ContractError`` (a ``ValueError``); their messages name the failing field location but
never echo the submitted value (paths, magnets and other user text stay out of 422 bodies and logs).
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Sequence

from pydantic import (BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, StringConstraints,
                      ValidationError)
from pydantic import field_validator, model_validator

import suite_client

SUPPORTED_DEVICES = ("sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra")
EVIDENCE_WORDS = suite_client.EVIDENCE_WORDS
RUN_RESULT_SCHEMA = "videosuite-comparison-run/1"
STAGES = ("planning", "selection_required", "awaiting_confirmation", "facts", "pixels", "rank", "done", "error",
          "cancelled")
COMPLETED_STAGES = ("planning", "facts", "quick", "pixels", "rank")
FAILURE_STAGES = ("planning", "selection", "facts", "quick", "pixels", "rank", "report")
UNIFIED_UPDATE_HINT = ("Install VideoSuite 0.7.0 or later (run the latest Video Comparator installer), then restart "
                       "the app. Advanced and Calibration keep working with the installed suite.")

_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_LANG_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_PATHS = 50                       # the same bound as the /suite routes
PathText = Annotated[StrictStr, StringConstraints(max_length=4096)]


class ContractError(ValueError):
    """A request/snapshot/result failed the frozen contract. The message never contains submitted values."""


class CapabilityError(Exception):
    """The installed VideoSuite lacks a capability the unified flow needs (``missing`` lists them)."""

    def __init__(self, message: str, missing: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.missing = list(missing)


# ── models ──────────────────────────────────────────────────────────────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=False)


def _ident(value: Optional[str], what: str) -> Optional[str]:
    if value is not None and not _ID_RE.fullmatch(value):
        raise ValueError(f"{what} is not a valid id")
    return value


class MagnetInput(_Strict):
    uri: StrictStr
    file_index: Optional[StrictInt] = None

    @field_validator("uri")
    @classmethod
    def _uri(cls, value: str) -> str:
        if not value.startswith("magnet:?") or len(value) > 8192 or any(ord(c) < 32 for c in value):
            raise ValueError("uri must be a magnet link")
        return value

    @field_validator("file_index")
    @classmethod
    def _index(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("file_index must be zero or more")
        return value


class RunRequest(_Strict):
    input_kind: Literal["files", "magnets"]
    results: Literal["overall", "device", "both"]
    depth: Literal["quick", "full"]
    paths: Optional[Annotated[List[PathText], Field(max_length=MAX_PATHS)]] = None
    magnets: Optional[Annotated[List[MagnetInput], Field(max_length=MAX_PATHS)]] = None
    device: Optional[StrictStr] = None
    primary_player: Optional[StrictStr] = None
    player: Optional[StrictStr] = None
    include_alternatives: StrictBool = True
    fs: Optional[Literal["fat32", "exfat", "ntfs"]] = None
    lang: StrictStr = "eng"
    audio_output: Optional[Literal["tv_speakers", "soundbar", "avr"]] = None

    @field_validator("lang")
    @classmethod
    def _lang(cls, value: str) -> str:
        if not _LANG_RE.fullmatch(value):
            raise ValueError("lang is not a valid language code")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RunRequest":
        if self.input_kind == "files":
            if not self.paths or self.magnets is not None:
                raise ValueError("a files run needs a non-empty paths list and no magnets")
        elif not self.magnets or self.paths is not None:
            raise ValueError("a magnets run needs a non-empty magnets list and no paths")
        if self.input_kind == "magnets" and (self.depth != "quick" or not 2 <= len(self.magnets) <= 8):
            raise ValueError("magnets require Quick and two to eight links")
        if self.paths is not None and any(not p.strip() for p in self.paths):
            raise ValueError("paths must not be empty text")
        if self.results == "overall":
            if (self.device, self.primary_player, self.player, self.fs, self.audio_output) != (None,) * 5:
                raise ValueError("overall results take no device, players, file system or audio output")
            return self
        if self.device not in SUPPORTED_DEVICES:
            raise ValueError("device results need one supported device")
        _ident(self.primary_player, "primary_player")
        _ident(self.player, "player")
        if self.primary_player is None or self.primary_player == "all":
            raise ValueError("device results need an explicit primary player")
        expected = "all" if self.include_alternatives else self.primary_player
        if self.player != expected:
            raise ValueError("player must be all with alternatives, otherwise the primary player")
        return self


class RunCandidate(_Strict):
    candidate_id: StrictStr
    kind: Literal["file", "magnet"]
    path: Optional[StrictStr] = None
    uri: Optional[StrictStr] = None
    file_index: Optional[StrictInt] = None
    status: Literal["pending", "ready", "unreadable", "selection_required", "failed"]
    record_id: Optional[StrictStr] = None
    reason: Optional[StrictStr] = None
    size: Optional[StrictInt] = Field(default=None, ge=0)
    mtime_ns: Optional[StrictInt] = Field(default=None, ge=0)
    entries: Optional[List[Dict[str, Any]]] = None

    @model_validator(mode="after")
    def _source(self) -> "RunCandidate":
        if (self.kind == "file") != (self.path is not None) or (self.kind == "magnet") != (self.uri is not None):
            raise ValueError("a candidate names exactly its own source")
        if self.status == "ready" and self.record_id is None:
            raise ValueError("a ready candidate has a record id")
        return self


class Conditions(_Strict):
    device: Optional[StrictStr]
    primary_player: Optional[StrictStr]
    player: Optional[StrictStr]
    evaluated_players: List[StrictStr]
    fs: Optional[Literal["fat32", "exfat", "ntfs"]]
    lang: StrictStr
    audio_output: Optional[Literal["tv_speakers", "soundbar", "avr"]]
    settings_digest: Optional[StrictStr]

    @field_validator("settings_digest")
    @classmethod
    def _digest(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("settings_digest must be a SHA256 hex digest")
        return value


class Versions(_Strict):
    tool_version: Optional[StrictStr]
    engine_tag: Optional[StrictStr]
    facts_version: Optional[StrictInt]
    rules_versions: Dict[StrictStr, StrictInt]


class Estimate(_Strict):
    pairs: StrictInt = Field(ge=0)
    minutes: StrictInt = Field(ge=0)


class PublicRunSnapshot(_Strict):
    run_id: StrictStr
    plan_revision: StrictStr
    request: RunRequest
    stage: Literal[STAGES]  # type: ignore[valid-type]
    job_id: Optional[StrictStr]
    candidates: List[RunCandidate]
    conditions: Conditions
    versions: Versions
    records_digest: Optional[StrictStr]
    estimate: Optional[Estimate]
    created_at: StrictFloat | StrictInt

    @field_validator("run_id", "plan_revision")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if not _OPAQUE_RE.fullmatch(value):
            raise ValueError("must be an opaque id")
        return value

    @field_validator("records_digest")
    @classmethod
    def _records_digest(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("records_digest must be a SHA256 hex digest")
        return value

    @model_validator(mode="after")
    def _conditions_match_request(self) -> "PublicRunSnapshot":
        request, conditions = self.request, self.conditions
        if (conditions.device, conditions.primary_player, conditions.player) != (
                request.device, request.primary_player, request.player):
            raise ValueError("conditions must keep the requested device, primary player and player")
        if request.results == "overall" and conditions.evaluated_players:
            raise ValueError("overall evaluates no players")
        return self


class InternalRunSnapshot(PublicRunSnapshot):
    reports_dir: StrictStr

    @field_validator("reports_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not ntpath.isabs(value) or not ntpath.splitdrive(value)[0] or ".." in value.replace("/", "\\").split("\\"):
            raise ValueError("reports_dir must be an absolute run-owned folder")
        return value


PUBLIC_SNAPSHOT_FIELDS = tuple(PublicRunSnapshot.model_fields)


class Coverage(_Strict):
    submitted: StrictInt = Field(ge=0)
    readable: StrictInt = Field(ge=0)
    expected_pairs: StrictInt = Field(ge=0)
    completed_pairs: StrictInt = Field(ge=0)
    missing_pairs: StrictInt = Field(ge=0)
    complete: StrictBool

    @model_validator(mode="after")
    def _honest(self) -> "Coverage":
        if self.readable > self.submitted or self.completed_pairs + self.missing_pairs != self.expected_pairs:
            raise ValueError("coverage counts are inconsistent")
        if self.complete and (self.missing_pairs or self.readable != self.submitted):
            raise ValueError("coverage cannot be complete with missing pairs or unreadable candidates")
        return self


class Failure(_Strict):
    candidate_id: Optional[StrictStr]
    stage: Literal[FAILURE_STAGES]  # type: ignore[valid-type]
    kind: StrictStr
    message: StrictStr


class ReportRef(_Strict):
    report_id: StrictStr
    name: StrictStr
    kind: Literal["json", "text", "html", "log"]
    url: StrictStr
    sha256: StrictStr

    @model_validator(mode="after")
    def _relative(self) -> "ReportRef":
        if not _OPAQUE_RE.fullmatch(self.report_id) or not _DIGEST_RE.fullmatch(self.sha256):
            raise ValueError("report id/hash invalid")
        if ("/" in self.name or "\\" in self.name or any(ord(c) < 32 for c in self.name)
                or not 0 < len(self.name) <= 255):
            raise ValueError("report name must be a plain file name")
        if not re.fullmatch(r"/comparison/runs/[A-Za-z0-9_-]{1,128}/reports/" + re.escape(self.report_id)
                            + r"(\?download=[01])?", self.url):
            raise ValueError("report url must be a relative registry url")
        return self


class Provenance(_Strict):
    build_id: Optional[StrictStr]
    tool_version: Optional[StrictStr]
    engine_tag: Optional[StrictStr]
    facts_version: Optional[StrictInt]
    rules_versions: Dict[StrictStr, StrictInt]
    records_digest: Optional[StrictStr]
    settings_digest: Optional[StrictStr]


class RunResult(_Strict):
    schema_: Literal["videosuite-comparison-run/1"] = Field(alias="schema")
    run_id: StrictStr
    request: RunRequest
    snapshot: PublicRunSnapshot
    measurement: Literal["metadata", "pixels"]
    completed_stages: List[Literal[COMPLETED_STAGES]]  # type: ignore[valid-type]
    coverage: Coverage
    facts: List[Dict[str, Any]]
    overall: Optional[Dict[str, Any]]
    device: Optional[Dict[str, Any]]
    failures: List[Failure]
    reports: List[ReportRef]
    estimate: Optional[Estimate]
    provenance: Provenance

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)


# ── public API ──────────────────────────────────────────────────────────────────

def _errors(exc: ValidationError) -> str:
    """Locations and messages only: pydantic's ``input`` values are deliberately dropped."""
    parts = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        location = ".".join(str(p) for p in error.get("loc", ())) or "body"
        message = str(error.get("msg", "invalid"))
        parts.append(f"{location}: {message}")
    return "; ".join(parts[:10]) or "invalid"


def _model(model: type, raw: Any, what: str) -> BaseModel:
    if not isinstance(raw, dict):
        raise ContractError(f"{what} must be a JSON object")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ContractError(f"invalid {what}: {_errors(exc)}") from None


def _dump(model: BaseModel) -> Dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True)


def validate_run_request(raw: Any) -> Dict[str, Any]:
    """The strict ``RunRequest``; the unused one of ``paths``/``magnets`` is omitted from the returned dict."""
    data = _dump(_model(RunRequest, raw, "run request"))
    data.pop("magnets" if data["input_kind"] == "files" else "paths")
    return data


def validate_internal_snapshot(raw: Any) -> Dict[str, Any]:
    data = _dump(_model(InternalRunSnapshot, raw, "internal run snapshot"))
    for candidate in data['candidates']:
        if candidate.get('entries') is None:
            candidate.pop('entries', None)
    data["request"] = validate_run_request(raw["request"])
    return data


def validate_public_snapshot(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict) and "reports_dir" in raw:
        raise ContractError("a public run snapshot never contains reports_dir")
    data = _dump(_model(PublicRunSnapshot, raw, "public run snapshot"))
    for candidate in data['candidates']:
        if candidate.get('entries') is None:
            candidate.pop('entries', None)
    data["request"] = validate_run_request(raw["request"])
    return data


def to_public_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Project the allowlisted public fields of a validated internal snapshot (never serialize the internal dict)."""
    internal = validate_internal_snapshot(snapshot)
    return validate_public_snapshot({name: internal[name] for name in PUBLIC_SNAPSHOT_FIELDS})


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _nested(command: str, doc: Any, what: str) -> None:
    try:
        suite_client.validate_document(command, doc)
    except suite_client.SuiteError as exc:
        raise ContractError(f"{what}: {exc.message}") from None


def _quick_order(order: Any, what: str) -> None:
    """A bare QuickOrder (Quick Overall answer): validated through a minimal quick envelope."""
    envelope = {"schema": "videosuite-quick/1", "tool_version": "envelope", "results": "overall",
                "measurement": "metadata", "records": [], "overall": order, "devices": {},
                "selected": {"device": None, "player": None}, "alternatives": [], "failures": []}
    _nested("quick", envelope, what)


def _media_records(rows: List[Dict[str, Any]]) -> None:
    for i, row in enumerate(rows):
        source = row.get("source")
        if not (isinstance(row.get("id"), str) and row.get("schema") == "media-record/1"
                and isinstance(row.get("facts_version"), int) and isinstance(source, dict)
                and isinstance(source.get("path"), str)):
            raise ContractError(f"invalid run result: facts[{i}] is not a media record")


def validate_run_result(raw: Any) -> Dict[str, Any]:
    """Strict ``videosuite-comparison-run/1``: public snapshot only, honest coverage, requested answers only."""
    model = _model(RunResult, raw, "run result")
    result = _dump(model)
    result["request"] = validate_run_request(raw["request"])
    result["snapshot"] = validate_public_snapshot(raw["snapshot"])
    if _contains_key(raw, "reports_dir"):
        raise ContractError("a run result never contains reports_dir")
    request, snapshot = result["request"], result["snapshot"]
    if result["run_id"] != snapshot["run_id"] or request != snapshot["request"]:
        raise ContractError("invalid run result: run id/request differ from the snapshot")
    quick = request["depth"] == "quick"
    if result["measurement"] != ("metadata" if quick else "pixels"):
        raise ContractError("invalid run result: measurement does not match the requested depth")
    if request["results"] == "overall" and result["device"] is not None:
        raise ContractError("invalid run result: an overall run carries no device results")
    if request["results"] == "device" and result["overall"] is not None:
        raise ContractError("invalid run result: a device run carries no overall answer")
    if result["overall"] is not None:
        if quick:
            _quick_order(result["overall"], "invalid run result overall")
        else:
            _nested("rank", result["overall"], "invalid run result overall")
            if result["overall"]["devices"]:
                raise ContractError("invalid run result: the overall answer is the neutral rank")
    if result["device"] is not None:
        _nested("quick" if quick else "rank", result["device"], "invalid run result device")
        if quick and result["device"]["results"] == "overall":
            raise ContractError("invalid run result: device answer has no device results")
    _media_records(result["facts"])
    if result["coverage"]["complete"] and any(f["stage"] in ("facts", "pixels") for f in result["failures"]):
        raise ContractError("invalid run result: a complete run has no candidate failures")
    for ref in result["reports"]:
        if not ref["url"].startswith(f"/comparison/runs/{result['run_id']}/reports/"):
            raise ContractError("invalid run result: report url belongs to another run")
    return result


def record_id(path: str, size_bytes: int, mtime_ns: int) -> str:
    """The suite's stable media record id (``videosuite.record.record_id``): sha1 of ``path|size|mtime_ns``."""
    return hashlib.sha1(f"{path}|{int(size_bytes or 0)}|{int(mtime_ns or 0)}".encode("utf-8", "replace")).hexdigest()


def records_digest(records: List[Dict[str, Any]], candidates: List[Dict[str, Any]], facts_version: int) -> str:
    """The suite's ``videosuite-record-snapshot/1`` digest: SHA256 of canonical JSON of the snapshot body."""
    body = {"facts_version": facts_version, "records": records, "candidates": candidates}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def require_capabilities(info: Any, requested: Sequence[str]) -> None:
    """Raise ``CapabilityError`` unless the handshake ``info`` is ``ok`` and lists every requested capability.

    The message is a compatible-update hint; other suite features (Advanced, Calibration) are not gated here."""
    if not isinstance(info, dict) or info.get("status") != "ok":
        status = info.get("status") if isinstance(info, dict) else None
        raise CapabilityError(f"VideoSuite is not ready ({status or 'unknown'}). {UNIFIED_UPDATE_HINT}",
                              list(requested))
    available = info.get("capabilities")
    available = set(available) if isinstance(available, list) else set()
    missing = [name for name in requested if name not in available]
    if missing:
        version = info.get("tool_version") or "unknown"
        raise CapabilityError(f"The installed VideoSuite {version} lacks {', '.join(missing)}. {UNIFIED_UPDATE_HINT}",
                              missing)
