"""Magnet-link metadata fetcher.

Fetches a torrent's metadata (file list) via libtorrent without downloading
full files, then pulls the header slice of each video file so ffprobe can
read the real codec/HDR/audio data. Returns a structured verdict for each
file. Always cleans up the temporary download directory.

The libtorrent import is intentionally lazy so the rest of the FastAPI app
keeps booting even when the binding is missing on this machine.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable

from analysis import analyze_file
import proc_guard

logger = logging.getLogger("video-analyzer.magnet")

VIDEO_EXTS = (".mkv", ".mp4", ".ts", ".m2ts", ".hevc", ".h265")
JUNK_NAME_PATTERNS = (
    r"\bsample\b", r"\btrailer\b", r"\brarbg\b\.txt",
)
JUNK_EXTS = (".exe", ".rar", ".zip", ".7z", ".iso", ".nfo", ".txt", ".srr", ".srt")

# MKV: EBML header (codec/HDR/audio) is always at the start → head only.
# MP4/M2TS: moov atom may be at the end → download both head and tail.
HEAD_BYTES = 32 * 1024 * 1024  # 32 MB head — gives MediaInfo enough SEI / Atmos data
TAIL_BYTES = 64 * 1024 * 1024  # 64 MB tail — large MP4 moov atoms (4K HEVC, multi-audio)

# Middle-piece sampling: HDR10+ SEI messages live scattered through the
# bitstream (per-scene dynamic metadata), so head/tail alone won't catch
# them. We pull ~1 piece every MIDDLE_SAMPLE_INTERVAL_BYTES so a 24-frame
# slice from a few interior points lands on disk for hdr10plus_tool to
# scan. Cost: ~+96 MB for an 80 GB file (one piece per 10 GB).
MIDDLE_SAMPLE_INTERVAL_BYTES = 10 * 1024 * 1024 * 1024   # 10 GB
MIDDLE_SAMPLE_MIN_FILE_SIZE  = 15 * 1024 * 1024 * 1024   # only sample for files > 15 GB

METADATA_TIMEOUT_S = 90
PIECE_TIMEOUT_S    = 180

# Probing (MediaInfo + ffprobe + HDR10+ scan) is the slow phase for big packs —
# ~110 s/file was measured on 11 GB DV/HDR10+ MP4 episodes. The probe phase gets
# its own time budget that SCALES with the video-file count, so a season pack
# isn't killed mid-probe (which used to drop ALL results). When the budget is
# hit we return the episodes analyzed so far instead of failing the whole job.
PROBE_BASE_S     = 60     # fixed overhead allowance
PROBE_PER_FILE_S = 150    # per video file (covers the ~110 s/file worst case)
PROBE_MAX_S      = 1200   # hard ceiling on the probe phase regardless of count

SUITE_STAGE_MAX_S = 420
PartialScorer = Callable[[list[dict[str, Any]], Callable[[str], None], Callable[[], bool]], dict[str, dict[str, Any]]]
_partial_scorer: PartialScorer | None = None


def set_partial_scorer(fn: PartialScorer | None) -> None:
    global _partial_scorer
    _partial_scorer = fn


# Same rules as videosuite/cli.py `_check_partial` (--real-name): the suite refuses names with
# control characters, DEL/C1, line separators and bidi controls, or longer than 255 characters.
_SUITE_NAME_BAD = re.compile("[\x00-\x1f\x7f-\x9f -‮⁦-⁩]")
SUITE_NAME_MAX = 255
SUITE_NAME_FALLBACK = "partial.bin"


def suite_real_name(torrent_path: str) -> str:
    """File name sent to the suite as ``--real-name``: the last path component, made acceptable to the CLI.

    Only the value handed to the suite is changed; the file name shown in job results stays as in the torrent.
    """
    name = re.split(r"[\\/]", str(torrent_path))[-1]
    name = _SUITE_NAME_BAD.sub("_", name).strip()
    if len(name) > SUITE_NAME_MAX:
        ext = os.path.splitext(name)[1]
        if len(ext) > 32:
            ext = ""
        name = (name[:SUITE_NAME_MAX - len(ext)] + ext).strip()
    return name or SUITE_NAME_FALLBACK

# All well-known public DHT bootstrap nodes.
_DHT_ROUTERS = [
    ("router.bittorrent.com",  6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com",    6881),
    ("dht.libtorrent.org",    25401),
    ("dht.aelitis.com",        6881),
    ("router.bitcomet.com",    6881),
]


class MagnetUnavailable(RuntimeError):
    """Raised when libtorrent isn't installed."""


class NoFreePort(RuntimeError):
    """Raised when every port of the torrent port pool is in use."""


# ── Pure helpers (no libtorrent import: unit-testable with fakes) ─────────────

# Upload is capped, not throttled to nothing: a ~1 B/s limit makes peers choke us
# (tit-for-tat) and downloads stall. 50 KiB/s is the floor the tests enforce.
UPLOAD_RATE_LIMIT_BPS = 51200

TORRENT_PORT_FIRST = 6881
TORRENT_PORT_LAST  = 6999


def session_settings(port: int) -> dict[str, Any]:
    """The ``settings_pack`` dict for one short-lived torrent session.

    Every key is a documented libtorrent settings_pack name (an unknown key makes
    ``lt.session`` raise); tests/test_magnet_session.py freezes the allowed set.
    Exposure is reduced by turning UPnP / NAT-PMP / local service discovery off and
    by giving each job its own port; DHT stays on. ``alert_mask`` needs the
    libtorrent module, so the caller adds it. Unchoke slots are left at default.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError(f"invalid torrent listen port: {port!r}")
    return {
        "listen_interfaces":        f"0.0.0.0:{port}",
        "enable_dht":               True,
        "enable_lsd":               False,
        "enable_natpmp":            False,
        "enable_upnp":              False,
        "upload_rate_limit":        UPLOAD_RATE_LIMIT_BPS,
        "connection_speed":         100,     # connect to more peers/sec
        "peer_connect_timeout":     5,
        "handshake_timeout":        10,
        "request_timeout":          15,
        "min_reconnect_time":       1,
        "announce_to_all_tiers":    True,    # use every tracker tier
        "announce_to_all_trackers": True,
    }


class PortAllocator:
    """Thread-safe pool of listen ports shared by every torrent session.

    ``/magnet/`` and each slot of ``/compare-magnets/`` take a port with
    ``acquire()`` (lowest free first) and give it back with ``release()`` in a
    ``finally`` (or use ``lease()``), so two live sessions never share a port.
    """

    def __init__(self, first: int = TORRENT_PORT_FIRST, last: int = TORRENT_PORT_LAST):
        self._first = first
        self._last = last
        self._lock = threading.Lock()
        self._used: set[int] = set()

    def acquire(self) -> int:
        with self._lock:
            for port in range(self._first, self._last + 1):
                if port not in self._used:
                    self._used.add(port)
                    return port
        raise NoFreePort(
            f"No free torrent port in {self._first}-{self._last}; "
            "too many magnet jobs are running."
        )

    def release(self, port: int) -> None:
        with self._lock:
            self._used.discard(port)

    def in_use(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._used)

    def reset(self) -> None:
        with self._lock:
            self._used.clear()

    @contextlib.contextmanager
    def lease(self):
        port = self.acquire()
        try:
            yield port
        finally:
            self.release(port)


# One allocator for the whole process.
port_allocator = PortAllocator()


def tail_is_present(path: str, real_size: int) -> bool:
    """Whether an MP4/M2TS probe file has its tail (moov atom) on disk.

    A file no larger than HEAD+TAIL is fully covered by the head and tail pieces,
    so it counts as downloaded without reading it (RB-20). Larger files are
    checked by reading the last 4 KiB: all zeros means the tail never arrived.
    """
    if real_size <= HEAD_BYTES + TAIL_BYTES:
        return True
    try:
        with open(path, "rb") as src:
            src.seek(real_size - 4096)
            return any(b != 0 for b in src.read(4096))
    except OSError as exc:
        logger.warning("Tail probe failed on %s: %s", path, exc)
        return False


def file_record(index: int, name: str, size: int) -> dict[str, Any]:
    """Per-file verdict record. ``size_gib`` is the size in GiB (1024**3);
    ``size_gb`` is a DEPRECATED alias carrying the same GiB number, kept for the
    React UI until Plan 3 switches to ``size_gib``."""
    cls = _classify_file(name, size)
    size_gib = round(size / (1024 ** 3), 3)
    return {
        "index":         index,
        "name":          name,
        "size_bytes":    size,
        "size_gib":      size_gib,
        "size_gb":       size_gib,
        "ext":           cls["ext"],
        "verdict":       cls["verdict"],
        "reasons":       list(cls["reasons"]),
        "ffprobe_ok":    False,
        "analysis_path": None,
    }


def _load_libtorrent():
    try:
        import libtorrent as lt  # type: ignore
    except ImportError as exc:
        if os.environ.get('VIDEOSUITE_OFFLINE') == '1':
            from app_preflight import MAGNET_UNAVAILABLE
            raise MagnetUnavailable(MAGNET_UNAVAILABLE) from exc
        raise MagnetUnavailable(
            "libtorrent is not installed. Install it with "
            "`pip install libtorrent` (you may also need the system "
            "package, e.g. `brew install libtorrent-rasterbar` on macOS "
            "or `apt install python3-libtorrent` on Debian/Ubuntu)."
        ) from exc
    return lt


def _wait_storage_alert(session, handle, name, cancel_check, timeout=10):
    """Synchronize async storage changes before piece overrides or disk reads."""
    deadline = time.monotonic() + timeout
    while True:
        if cancel_check():
            raise RuntimeError('Cancelled')
        for alert in session.pop_alerts():
            if type(alert).__name__ == name and alert.handle == handle:
                return
        if time.monotonic() >= deadline:
            raise TimeoutError('Torrent storage did not become ready for partial facts')
        time.sleep(.05)


def _classify_file(name: str, size: int) -> dict[str, Any]:
    base = os.path.basename(name).lower()
    ext = os.path.splitext(base)[1]
    reasons: list[str] = []
    verdict = "good"

    if ext in JUNK_EXTS:
        verdict = "bad"
        reasons.append(f"non-video extension ({ext})")
    elif ext not in VIDEO_EXTS:
        verdict = "skip"
        reasons.append(f"unsupported extension ({ext or 'none'})")

    for pattern in JUNK_NAME_PATTERNS:
        if re.search(pattern, base, re.IGNORECASE):
            verdict = "bad"
            reasons.append(f"matches junk pattern '{pattern}'")

    if ext in VIDEO_EXTS and size < 50 * 1024 * 1024:
        verdict = "bad"
        reasons.append(f"suspiciously small for a video ({size/1024/1024:.1f} MB)")

    if size > 200 * 1024 * 1024 * 1024:
        reasons.append(f"very large ({size/1024**3:.1f} GB)")

    return {"verdict": verdict, "reasons": reasons, "ext": ext}


def _pieces_for_range(piece_length: int, file_offset: int,
                      file_size: int, head_bytes: int, tail_bytes: int) -> list[int]:
    """Piece indexes covering the first `head_bytes` and last `tail_bytes` of a file."""
    if file_size <= 0:
        return []
    first_piece        = file_offset // piece_length
    last_piece_of_file = (file_offset + file_size - 1) // piece_length

    pieces: set[int] = set()

    if head_bytes > 0:
        head_end_byte  = file_offset + min(head_bytes, file_size) - 1
        head_last_piece = head_end_byte // piece_length
        pieces.update(range(first_piece, head_last_piece + 1))

    if tail_bytes > 0:
        tail_start_byte  = file_offset + max(0, file_size - tail_bytes)
        tail_first_piece = tail_start_byte // piece_length
        pieces.update(range(tail_first_piece, last_piece_of_file + 1))

    return sorted(pieces)


def _should_middle_sample(file_size: int, enable_hdr10plus: bool) -> bool:
    """Whether to pull ~1 interior piece per 10 GB for HDR10+ SEI scanning.

    HDR10+ dynamic metadata (ST 2094-40 SEI) is per-scene, scattered through
    the whole bitstream, so head/tail alone can miss it. Sampling interior
    pieces lets hdr10plus_tool scan the middle of the film, not just scene 1.

    Platform behaviour:
      * Linux/macOS — automatic for files > MIDDLE_SAMPLE_MIN_FILE_SIZE
        (libtorrent's storage_mode_sparse keeps the gaps as real holes).
      * Windows — OPT-IN per request via `enable_hdr10plus` (the API's
        `hdr10plus=true` flag). It is disk-safe because every prioritized
        video file is marked FILE_ATTRIBUTE_SPARSE_FILE before libtorrent's
        first write (see the fsutil pre-sparse block in fetch_magnet_metadata),
        so unwritten gaps between sampled pieces stay sparse holes. The
        earlier ENOSPC blow-up came from a non-sparse `truncate()` to the full
        size — NOT from sparse high-offset writes, which is what this does.
        Off by default because the Bravia 8 II ignores HDR10+; turn it on when
        comparing releases for an HDR10+-capable TV (Samsung/Panasonic/Philips).
    """
    if file_size < MIDDLE_SAMPLE_MIN_FILE_SIZE:
        return False
    if os.name == "nt":
        return enable_hdr10plus
    return True


def _middle_sample_pieces(piece_length: int, file_offset: int,
                           file_size: int, enable_hdr10plus: bool = False) -> list[int]:
    """Pick one piece every MIDDLE_SAMPLE_INTERVAL_BYTES through the file.

    Gated by `_should_middle_sample` — see there for the per-platform rules
    and why Windows requires the explicit `enable_hdr10plus` opt-in.
    """
    if not _should_middle_sample(file_size, enable_hdr10plus):
        return []
    pieces: list[int] = []
    # Start after the head region, stop before the tail region.
    first_byte = file_offset + HEAD_BYTES + MIDDLE_SAMPLE_INTERVAL_BYTES
    last_byte  = file_offset + file_size - TAIL_BYTES - MIDDLE_SAMPLE_INTERVAL_BYTES
    cursor = first_byte
    while cursor < last_byte:
        pieces.append(cursor // piece_length)
        cursor += MIDDLE_SAMPLE_INTERVAL_BYTES
    return pieces


def _pieces_for_file(piece_length: int, file_offset: int,
                     file_size: int, ext: str,
                     enable_hdr10plus: bool = False) -> list[int]:
    """Select pieces to download for verification.

    All videos: head (32 MB) for codec/HDR/audio metadata.
    MP4/M2TS:    + tail (64 MB) for the moov atom.
    Large files: + ~1 piece every 10 GB so hdr10plus_tool can scan SEI
                 messages scattered through the dynamic-HDR bitstream
                 (interior sampling is opt-in on Windows — see
                 `_should_middle_sample`).
    """
    if ext in (".mp4", ".m2ts"):
        pieces = _pieces_for_range(piece_length, file_offset, file_size,
                                    HEAD_BYTES, TAIL_BYTES)
    else:
        pieces = _pieces_for_range(piece_length, file_offset, file_size,
                                    HEAD_BYTES, 0)
    pieces.extend(_middle_sample_pieces(piece_length, file_offset, file_size,
                                         enable_hdr10plus))
    return sorted(set(pieces))


def _check_temp_disk_space(workdir: str, min_free_gb: float = 5.0) -> None:
    """Fail fast if the temp drive is too low on free space.

    A magnet job needs ≈ 200 MB for piece slices, plus up to 2.4 GB for the
    multi-slice HDR10+ extraction (8 × 300 MB), plus headroom for MediaInfo
    and ffprobe temp files. 5 GB is a safe floor; below that we bail with
    a clear error instead of letting the user wait 5 min for an ENOSPC.
    """
    try:
        usage = shutil.disk_usage(workdir)
    except OSError:
        return  # can't check — let the job try and report any real failure
    free_gb = usage.free / 1024**3
    if free_gb < min_free_gb:
        raise OSError(
            f"Not enough free space on temp drive ({free_gb:.1f} GB free; "
            f"need at least {min_free_gb:.0f} GB). Free up space in "
            f"{os.path.dirname(workdir)} and retry."
        )


def fetch_magnet_metadata(
    magnet_uri: str,
    skip_dovi_scan: bool = True,
    emit: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    listen_port: int = 6881,
    enable_hdr10plus: bool = False,
    _control=None,
    *,
    partial_scorer: PartialScorer | None = None,
    use_default_scorer: bool = True,
    selected_file_index: int | None = None,
    metadata_only: bool = False,
) -> dict[str, Any]:
    """Download torrent metadata + a small header slice of each video file,
    then run ffprobe for real codec/HDR/audio results.

    Returns a dict with `files` (per-file verdicts), `analyses` (full VideoData
    for files that survived ffprobe), and `info_hash`/`name` torrent details.

    Always tears down the libtorrent session and removes the temp directory.
    """
    scorer = partial_scorer if partial_scorer is not None else (_partial_scorer if use_default_scorer else None)
    if not use_default_scorer:
        skip_dovi_scan, enable_hdr10plus = True, False
    lt = _load_libtorrent()
    emit = emit or (lambda _msg: None)
    cancel_check = cancel_check or (lambda: False)

    workdir = tempfile.mkdtemp(prefix="videolyzer-magnet-")
    session: Any = None
    handle: Any = None

    try:
        # Fail fast on low disk before we spend minutes on DHT/peers.
        _check_temp_disk_space(workdir)
        emit("Starting libtorrent session…")

        # Fix 3: richer session settings for faster peer discovery.
        # listen_port is parameterized so multiple concurrent sessions
        # (used by /compare-magnets/) can run side by side without port collisions.
        # UPnP/NAT-PMP/LSD are off and upload is capped at 50 KiB/s (not 1 B/s):
        # see `session_settings`.
        settings = session_settings(listen_port)
        settings["alert_mask"] = lt.alert.category_t.all_categories
        session = lt.session(settings)
        if _control is not None:
            _control.set_session(session)

        for host, port in _DHT_ROUTERS:
            session.add_dht_router(host, port)

        try:
            params = lt.parse_magnet_uri(magnet_uri)
            params.save_path = workdir
            if not use_default_scorer or metadata_only:
                # Fetch metadata while forbidding payload pieces. Explicit priorities below
                # are the only pieces a unified Quick run may request.
                params.flags |= lt.torrent_flags.upload_mode
            # storage_mode_sparse: tell libtorrent to create files only as
            # pieces arrive, not pre-allocate. On Linux this is enough; on
            # Windows we additionally mark each file FILE_ATTRIBUTE_SPARSE_FILE
            # below — see the `if os.name == "nt"` block after prioritize_files.
            try:
                params.storage_mode = lt.storage_mode_t.storage_mode_sparse  # type: ignore[attr-defined]
            except AttributeError:
                pass   # older libtorrent versions — sparse is already default
            handle = session.add_torrent(params)
            # Fix 1: do NOT set upload_limit to 1 byte/sec — that triggers
            # tit-for-tat choking and peers stop sending us data entirely.
            # We remove the torrent within minutes so brief uploads are fine.
        except Exception as exc:
            raise ValueError(f"Invalid magnet URI: {exc}") from exc

        # Fix 2: alert-based metadata detection (100ms polling instead of 500ms).
        emit("Fetching torrent metadata via DHT/peers…")
        deadline = time.time() + METADATA_TIMEOUT_S
        while True:
            if cancel_check():
                raise RuntimeError("Cancelled")
            # Drain the alert queue — fires metadata_received_alert as soon as ready.
            for alert in session.pop_alerts():
                if type(alert).__name__ == "metadata_received_alert":
                    break
            if handle.status().has_metadata:
                break
            if time.time() > deadline:
                raise TimeoutError(
                    f"No metadata received within {METADATA_TIMEOUT_S}s. "
                    "The torrent may have no live peers — check that your "
                    "firewall allows UDP on port 6881."
                )
            time.sleep(0.1)

        torrent_info = handle.torrent_file()
        files        = torrent_info.files()
        piece_length = torrent_info.piece_length()
        torrent_name = torrent_info.name()
        info_hash    = str(torrent_info.info_hash())
        num_files    = files.num_files()
        emit(f"Metadata received: '{torrent_name}' — {num_files} file(s)")

        file_records: list[dict[str, Any]] = []
        video_indexes: list[int] = []
        for i in range(num_files):
            f_path = files.file_path(i)
            if (os.path.isabs(f_path) or ':' in f_path or
                    any(p in ('..', '') for p in re.split(r'[/\\]', f_path)) or
                    any(ord(c) < 32 for c in f_path)):
                raise ValueError('Unsafe torrent entry path')
            f_size = files.file_size(i)
            rec = file_record(i, f_path, f_size)
            file_records.append(rec)
            if rec["verdict"] == "good":
                video_indexes.append(i)

        if metadata_only or (not use_default_scorer and selected_file_index is None and len(video_indexes) > 1):
            return {"torrent_name": torrent_name, "info_hash": info_hash, "files": file_records, "analyses": []}
        if selected_file_index is not None:
            if selected_file_index not in video_indexes:
                raise ValueError("Selected entry is not a playable video file")
            video_indexes = [selected_file_index]
        if not use_default_scorer:
            video_indexes = video_indexes[:8]
        if not video_indexes:
            emit("No playable video files in this torrent.")
            return {
                "torrent_name": torrent_name,
                "info_hash":    info_hash,
                "files":        file_records,
                "analyses":     [],
            }

        # File priorities ≥1 so libtorrent allocates storage and writes pieces to disk.
        # Non-video files stay at 0 (skipped entirely).
        file_priorities = [(1 if i in set(video_indexes) else 0)
                           for i in range(num_files)]
        handle.prioritize_files(file_priorities)
        if not use_default_scorer:
            # A priority-0 file's bytes may live only in the partfile. Keep the
            # selected file writable, then override piece priorities AFTER the
            # asynchronous file-priority reset has completed, still in upload_mode.
            _wait_storage_alert(session, handle, 'file_prio_alert', cancel_check)
            handle.prioritize_pieces([0] * torrent_info.num_pieces())

        # CRITICAL on Windows/NTFS: pre-create each prioritized video file
        # as empty and mark it FILE_ATTRIBUTE_SPARSE_FILE before libtorrent
        # writes its first piece. Without this flag, libtorrent's first
        # high-offset write (e.g. a middle-piece sample at byte 30 GB) makes
        # NTFS allocate the entire 0–30 GB range as zero blocks on disk,
        # filling the temp drive within seconds. With the flag set, the
        # gap is a sparse hole that costs ~0 bytes until written.
        #
        # libtorrent's storage_mode_sparse does the right thing on Linux but
        # doesn't issue FSCTL_SET_SPARSE on Windows for us. fsutil is the
        # path of least resistance — it's preinstalled, fast (sub-second),
        # and idempotent.
        if os.name == "nt":
            for idx in video_indexes:
                target = os.path.join(workdir, files.file_path(idx))
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    # Create empty file (or leave existing alone — sparse-flag
                    # set is idempotent and won't lose existing content).
                    if not os.path.exists(target):
                        open(target, "ab").close()
                    result = proc_guard.run_tool(
                        ["fsutil", "sparse", "setflag", target],
                        timeout=5, text=False,
                    )
                    if result.returncode != 0:
                        logger.warning(
                            "fsutil could not mark %s sparse: rc=%d, stderr=%s",
                            target, result.returncode,
                            (result.stderr or b"").decode("utf-8", "replace")[:200],
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Pre-sparse failed for %s: %s", target, exc)

        # Fix 5: MKV → head only; MP4/M2TS → head + tail.
        priority_pieces: list[int] = []
        for idx in video_indexes:
            ext      = file_records[idx]["ext"]
            f_offset = files.file_offset(idx)
            f_size   = files.file_size(idx)
            priority_pieces.extend(
                _pieces_for_file(piece_length, f_offset, f_size, ext, enable_hdr10plus)
            )
        priority_pieces = sorted(set(priority_pieces))

        if os.name == "nt" and enable_hdr10plus:
            emit("HDR10+ deep scan ON — fetching interior pieces (~+10 MB/10 GB, "
                 "sparse) so hdr10plus_tool can scan the whole runtime.")

        est_mb = len(priority_pieces) * piece_length / 1024 / 1024
        emit(f"Downloading {len(priority_pieces)} piece(s) (~{est_mb:.1f} MB) for verification…")

        # Fix 4: 2-second deadline per piece instead of 0 (fail-fast).
        # Gives peers a moment to unchoke us before the request expires.
        for p in priority_pieces:
            handle.piece_priority(p, 7)
            handle.set_piece_deadline(p, 2000)
        if not use_default_scorer:
            handle.unset_flags(lt.torrent_flags.upload_mode)

        deadline = time.time() + PIECE_TIMEOUT_S
        while True:
            if cancel_check():
                raise RuntimeError("Cancelled")
            if all(handle.have_piece(p) for p in priority_pieces):
                break
            if time.time() > deadline:
                missing = sum(1 for p in priority_pieces if not handle.have_piece(p))
                emit(f"⚠ Timed out — {missing} piece(s) still missing. "
                     "Running ffprobe on what arrived.")
                break
            time.sleep(0.5)

        if not use_default_scorer:
            # have_piece does not promise disk-cache visibility to another reader.
            handle.flush_cache()
            _wait_storage_alert(session, handle, 'cache_flushed_alert', cancel_check)

        # Probe phase gets a budget scaled by the number of video files so a
        # large season pack isn't killed mid-probe. We always probe the first
        # file (loop_i == 0), then stop once the budget is exhausted and return
        # whatever was analyzed — partial results beat losing everything.
        num_videos   = len(video_indexes)
        probe_budget = min(PROBE_BASE_S + PROBE_PER_FILE_S * num_videos, PROBE_MAX_S)
        probe_deadline = time.time() + probe_budget

        analyses: list[dict[str, Any]] = []
        entries: list[dict[str, Any]] = []
        kept: set[str] = set()
        for loop_i, idx in enumerate(video_indexes):
            if loop_i > 0 and time.time() > probe_deadline:
                remaining = num_videos - loop_i
                emit(f"⚠ Probe budget ({probe_budget:.0f}s) reached — analyzed "
                     f"{len(analyses)}/{num_videos} file(s); skipping {remaining} "
                     f"remaining. Returning partial results.")
                break
            rec        = file_records[idx]
            local_path = os.path.join(workdir, files.file_path(idx))
            if not os.path.isfile(local_path):
                rec["verdict"] = "bad"
                rec["reasons"].append("header slice not written to disk")
                continue
            emit(f"Probing {os.path.basename(rec['name'])}…")
            # Build a probe file that has the real head bytes followed by a
            # sparse zero region padding it out to the real torrent file size.
            # This way MediaInfo reports the true file size (correct bitrate
            # calculation) but does not encounter random partial data libtorrent
            # may have written past the head from speculative piece requests.
            ext_lower = os.path.splitext(local_path)[1].lower()
            real_size = files.file_size(idx)
            # MP4/M2TS need both head AND tail bytes because the moov atom
            # (the metadata index) can live at the end of the file. For MKV
            # the EBML header at the start is sufficient.
            need_tail = ext_lower in (".mp4", ".m2ts")
            probe_path: str | None = None
            try:
                if not use_default_scorer:
                    # Unified Quick never calls legacy analysis/ranking. Retain only the bounded
                    # head (or head/tail sparse source) while this call's facts callback runs.
                    if not need_tail:
                        probe_path = local_path + ".__probe__" + ext_lower
                        with open(local_path, "rb") as src, open(probe_path, "wb") as dst:
                            dst.write(src.read(HEAD_BYTES))
                    entries.append({"key": rec["name"], "path": local_path if need_tail else probe_path,
                                    "real_size": real_size, "real_name": suite_real_name(rec["name"]),
                                    "coverage": "head_tail" if need_tail else "head"})
                    if probe_path:
                        kept.add(probe_path)
                    analyses.append({"path": rec["name"], "file": suite_real_name(rec["name"]),
                                     "magnet_partial": True})
                    continue
                # For MP4/M2TS we probe local_path directly. libtorrent has
                # already written the head + tail pieces (priority 7) and left
                # the middle as sparse zeros. MediaInfo sees the real file size
                # and reads from the right offsets — exactly what we need.
                # For MKV we build a head-only probe so MediaInfo doesn't try
                # to seek past the EBML header into the sparse middle.
                if need_tail:
                    # Quick sanity check: confirm libtorrent actually flushed
                    # the tail bytes. Read 4 KB at the tail offset; if all zeros,
                    # the moov atom isn't there yet — fall through to the
                    # head-only probe which will fail more cleanly. A file
                    # no larger than HEAD+TAIL is fully on disk (RB-20).
                    if tail_is_present(local_path, real_size):
                        emit(f"Probing local file with tail bytes intact "
                             f"({real_size / 1024**3:.2f} GB)…")
                        # Pass real size for consistency. MP4 usually reports
                        # it correctly from the moov atom anyway, but the
                        # override guards against a sparse-file size read
                        # returning the allocated rather than logical size.
                        result = analyze_file(local_path, skip_dovi_scan=skip_dovi_scan,
                                              size_override_bytes=real_size)
                    else:
                        logger.warning(
                            "Tail bytes not present at %d on %s — moov atom "
                            "likely missing; ffprobe will fail.",
                            real_size - 4096, local_path,
                        )
                        result = None
                elif _should_middle_sample(real_size, enable_hdr10plus):
                    # MKV with interior HDR10+ sampling active.
                    #
                    # CRITICAL: do NOT hand MediaInfo the full sparse local_path
                    # here — deep-parsing a 40-79 GB sparse MKV blew past the
                    # 6-min analyze_file timeout and the file was dropped whole.
                    # Instead probe a cheap HEAD-ONLY copy for MediaInfo/ffprobe
                    # (fast), and point ONLY the HDR10+ slice scanner at the full
                    # sparse local_path via hdr10plus_source, so it can still
                    # seek (-ss) to the interior pieces we downloaded.
                    emit(f"Probing MKV head + interior HDR10+ samples "
                         f"({real_size / 1024**3:.2f} GB)…")
                    probe_path = local_path + ".__probe__" + ext_lower
                    with open(local_path, "rb") as _src:
                        head_data = _src.read(HEAD_BYTES)
                    with open(probe_path, "wb") as _dst:
                        _dst.write(head_data)
                    result = analyze_file(probe_path, skip_dovi_scan=skip_dovi_scan,
                                          size_override_bytes=real_size,
                                          hdr10plus_source=local_path)
                else:
                    # MKV head-only probe (default — no interior sampling).
                    #
                    # We deliberately do NOT truncate the probe to the real
                    # torrent file size. On Windows that would force the OS
                    # to allocate the full file (truncate is non-sparse even
                    # with FILE_ATTRIBUTE_SPARSE_FILE set — see the comment
                    # in _middle_sample_pieces for the gory detail). Instead
                    # we leave the probe at exactly HEAD_BYTES of real data
                    # and pass the true size via size_override_bytes so
                    # analyze_file scores against reality (see below).
                    probe_path = local_path + ".__probe__" + ext_lower
                    with open(local_path, "rb") as _src:
                        head_data = _src.read(HEAD_BYTES)
                    with open(probe_path, "wb") as _dst:
                        _dst.write(head_data)
                    # Pass the real torrent file size INTO analyze_file via
                    # size_override_bytes. The probe is only HEAD_BYTES of real
                    # data, so without this analyze_file would compute bitrate
                    # (and the TV/quality scores derived from it) off a 32 MB
                    # file. Feeding the true size up front means bitrate, audio
                    # subtraction, tv_score, score, and confidence are ALL
                    # computed correctly in one pass — no fragile post-hoc
                    # patching of individual fields.
                    result = analyze_file(probe_path, skip_dovi_scan=skip_dovi_scan,
                                          size_override_bytes=real_size)
                if result and scorer is not None:
                    retained_path = local_path if need_tail else probe_path
                    entries.append({"key": rec["name"], "path": retained_path, "real_size": real_size,
                                    "real_name": suite_real_name(rec["name"]),
                                    "coverage": "head_tail" if need_tail else "head"})
                    if probe_path:
                        kept.add(probe_path)
            except Exception as exc:
                logger.warning("ffprobe failed on partial %s: %s", local_path, exc)
                result = None
            finally:
                if probe_path and probe_path not in kept:
                    try:
                        os.remove(probe_path)
                    except OSError:
                        pass
            if result:
                result["file"]           = os.path.basename(rec["name"])
                result["path"]           = rec["name"]
                result["magnet_partial"] = True
                rec["ffprobe_ok"]        = True
                rec["analysis_path"]     = result["path"]
                analyses.append(result)
            else:
                rec["verdict"] = "bad"
                rec["reasons"].append("ffprobe could not parse the header slice")

        if scorer and entries:
            try:
                handle.pause()
            except Exception:
                pass
            try:
                emit(f"Scoring {len(entries)} file(s) with VideoSuite…")
                scores = scorer(entries, emit, cancel_check)
                for analysis in analyses:
                    analysis["suite"] = scores.get(analysis["path"]) or {
                        "status": "error", "error_kind": "internal", "message": "no VideoSuite result"}
            except Exception:
                logger.exception("VideoSuite magnet scoring failed")
                for analysis in analyses:
                    analysis["suite"] = {"status": "error", "error_kind": "internal", "message": "VideoSuite scoring failed"}
            for path in kept:
                try:
                    os.remove(path)
                except OSError:
                    pass

        return {
            "torrent_name": torrent_name,
            "info_hash":    info_hash,
            "files":        file_records,
            "analyses":     analyses,
        }
    finally:
        try:
            emit("Cleaning up torrent session and temp files…")
        except Exception:
            logger.warning("Could not emit magnet cleanup notification", exc_info=True)
        if session is not None and handle is not None:
            try:
                session.remove_torrent(handle, lt.session.delete_files)  # type: ignore[attr-defined]
            except Exception:
                try:
                    session.remove_torrent(handle)
                except Exception:
                    pass
        if session is not None:
            try:
                session.pause()
            except Exception:
                pass
        time.sleep(0.5)
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception as exc:
            logger.warning("Failed to remove magnet workdir %s: %s", workdir, exc)
        session = None
        if _control is not None:
            _control.set_session(None)


class _SessionControl:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.session = None

    def set_session(self, session):
        with self.lock:
            self.session = session
        if session is not None and self.stop.is_set():
            session.pause()

    def cancel(self):
        self.stop.set()
        with self.lock:
            session = self.session
        if session is not None:
            session.pause()


def run_magnet_job_threaded(magnet_uri: str, skip_dovi_scan: bool,
                            emit: Callable[[str], None],
                            cancel_check: Callable[[], bool],
                            listen_port: int = 6881,
                            enable_hdr10plus: bool = False, *,
                            partial_scorer: PartialScorer | None = None,
                            use_default_scorer: bool = True,
                            selected_file_index: int | None = None,
                            metadata_only: bool = False) -> dict[str, Any]:
    """Wrapper that runs fetch_magnet_metadata with a hard outer timeout.

    The inner function now self-bounds every phase — metadata wait, piece
    download, and (new) a probe budget that scales with file count and returns
    partial results. So this outer join timeout is just a last-resort ceiling
    against a genuine hang (e.g. a single MediaInfo call wedging); it must be
    ≥ the inner worst case (METADATA + PIECE + PROBE_MAX) or it would kill a
    large pack mid-probe and drop everything — the bug this fixes.
    """
    result_holder: list[Any]                 = [None]
    error_holder:  list[BaseException | None] = [None]
    control = _SessionControl()
    done = threading.Event()
    worker_id = "magnet:" + uuid.uuid4().hex
    def cancelled():
        return control.stop.is_set() or cancel_check()

    def _run() -> None:
        try:
            result_holder[0] = fetch_magnet_metadata(
                magnet_uri, skip_dovi_scan=skip_dovi_scan,
                emit=emit, cancel_check=cancelled,
                listen_port=listen_port,
                enable_hdr10plus=enable_hdr10plus,
                _control=control,
                partial_scorer=partial_scorer, use_default_scorer=use_default_scorer,
                selected_file_index=selected_file_index, metadata_only=metadata_only,
            )
        except BaseException as e:  # noqa: BLE001
            error_holder[0] = e
        finally:
            done.set()
            proc_guard.workers.unregister(worker_id)

    t = threading.Thread(target=proc_guard.inherit_owner(_run), daemon=True)
    with proc_guard.workers.lock:
        if proc_guard.workers.stopping:
            raise RuntimeError("Cancelled")
        proc_guard.workers.register(worker_id, control.cancel, done.wait,
                                   force=control.cancel, job_id=proc_guard.owner_id())
        t.start()
    t.join(timeout=METADATA_TIMEOUT_S + PIECE_TIMEOUT_S + PROBE_MAX_S + 30
           + (SUITE_STAGE_MAX_S if partial_scorer is not None or (use_default_scorer and _partial_scorer is not None) else 0))
    if t.is_alive():
        # Stop network activity immediately, then give the worker its cleanup
        # budget. Retain its identity if native teardown cannot be completed.
        control.stop.set()
        proc_guard.workers.invoke(control.cancel, job_id=proc_guard.owner_id())
        t.join(timeout=10)
        raise TimeoutError(
            "Magnet job exceeded the hard timeout — a probe likely wedged. "
            "The torrent may also have no live seeders."
        )
    if error_holder[0]:
        raise error_holder[0]
    return result_holder[0]
