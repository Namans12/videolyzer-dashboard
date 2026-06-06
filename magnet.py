"""Magnet-link metadata fetcher.

Fetches a torrent's metadata (file list) via libtorrent without downloading
full files, then pulls the header slice of each video file so ffprobe can
read the real codec/HDR/audio data. Returns a structured verdict for each
file. Always cleans up the temporary download directory.

The libtorrent import is intentionally lazy so the rest of the FastAPI app
keeps booting even when the binding is missing on this machine.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from analysis import analyze_file

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


def _load_libtorrent():
    try:
        import libtorrent as lt  # type: ignore
    except ImportError as exc:
        raise MagnetUnavailable(
            "libtorrent is not installed. Install it with "
            "`pip install libtorrent` (you may also need the system "
            "package, e.g. `brew install libtorrent-rasterbar` on macOS "
            "or `apt install python3-libtorrent` on Debian/Ubuntu)."
        ) from exc
    return lt


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
) -> dict[str, Any]:
    """Download torrent metadata + a small header slice of each video file,
    then run ffprobe for real codec/HDR/audio results.

    Returns a dict with `files` (per-file verdicts), `analyses` (full VideoData
    for files that survived ffprobe), and `info_hash`/`name` torrent details.

    Always tears down the libtorrent session and removes the temp directory.
    """
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
        session = lt.session({
            "listen_interfaces":        f"0.0.0.0:{listen_port}",
            "alert_mask":               lt.alert.category_t.all_categories,
            "enable_dht":               True,
            "enable_lsd":               True,    # local service discovery
            "enable_natpmp":            True,
            "enable_upnp":              True,
            "connection_speed":         100,     # connect to more peers/sec
            "peer_connect_timeout":     5,
            "handshake_timeout":        10,
            "request_timeout":          15,
            "min_reconnect_time":       1,
            "announce_to_all_tiers":    True,    # use every tracker tier
            "announce_to_all_trackers": True,
        })

        for host, port in _DHT_ROUTERS:
            session.add_dht_router(host, port)

        try:
            params = lt.parse_magnet_uri(magnet_uri)
            params.save_path = workdir
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
            f_size = files.file_size(i)
            cls = _classify_file(f_path, f_size)
            rec = {
                "index":         i,
                "name":          f_path,
                "size_bytes":    f_size,
                "size_gb":       round(f_size / (1024**3), 3),
                "ext":           cls["ext"],
                "verdict":       cls["verdict"],
                "reasons":       cls["reasons"],
                "ffprobe_ok":    False,
                "analysis_path": None,
            }
            file_records.append(rec)
            if cls["verdict"] == "good":
                video_indexes.append(i)

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
        file_priorities = [1 if i in set(video_indexes) else 0 for i in range(num_files)]
        handle.prioritize_files(file_priorities)

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
                    result = subprocess.run(
                        ["fsutil", "sparse", "setflag", target],
                        check=False, capture_output=True, timeout=5,
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

        # Probe phase gets a budget scaled by the number of video files so a
        # large season pack isn't killed mid-probe. We always probe the first
        # file (loop_i == 0), then stop once the budget is exhausted and return
        # whatever was analyzed — partial results beat losing everything.
        num_videos   = len(video_indexes)
        probe_budget = min(PROBE_BASE_S + PROBE_PER_FILE_S * num_videos, PROBE_MAX_S)
        probe_deadline = time.time() + probe_budget

        analyses: list[dict[str, Any]] = []
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
                    # head-only probe which will fail more cleanly.
                    tail_present = False
                    if real_size > HEAD_BYTES + TAIL_BYTES:
                        try:
                            with open(local_path, "rb") as _src:
                                _src.seek(real_size - 4096)
                                sample = _src.read(4096)
                                tail_present = any(b != 0 for b in sample)
                        except OSError as exc:
                            logger.warning("Tail probe failed on %s: %s",
                                            local_path, exc)
                    if tail_present:
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
                    # MKV with interior HDR10+ sampling active. Probe the
                    # sparse local_path directly (like MP4) instead of a
                    # head-only copy — otherwise the interior pieces we just
                    # downloaded would be invisible to the HDR10+ scanner,
                    # which reads from local_path. Head + middle pieces are on
                    # disk; the gaps are sparse holes. MediaInfo reads the EBML
                    # from the head; the hdr10plus scanner seeks (-ss) only to
                    # the sampled positions, which hold real bytes. Pass the
                    # real size so bitrate/scores reflect the true file, not
                    # the sparse allocation.
                    emit(f"Probing local MKV with interior HDR10+ samples "
                         f"({real_size / 1024**3:.2f} GB)…")
                    result = analyze_file(local_path, skip_dovi_scan=skip_dovi_scan,
                                          size_override_bytes=real_size)
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
            except Exception as exc:
                logger.warning("ffprobe failed on partial %s: %s", local_path, exc)
                result = None
            finally:
                if probe_path:
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

        return {
            "torrent_name": torrent_name,
            "info_hash":    info_hash,
            "files":        file_records,
            "analyses":     analyses,
        }
    finally:
        emit("Cleaning up torrent session and temp files…")
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


def run_magnet_job_threaded(magnet_uri: str, skip_dovi_scan: bool,
                            emit: Callable[[str], None],
                            cancel_check: Callable[[], bool],
                            listen_port: int = 6881,
                            enable_hdr10plus: bool = False) -> dict[str, Any]:
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

    def _run() -> None:
        try:
            result_holder[0] = fetch_magnet_metadata(
                magnet_uri, skip_dovi_scan=skip_dovi_scan,
                emit=emit, cancel_check=cancel_check,
                listen_port=listen_port,
                enable_hdr10plus=enable_hdr10plus,
            )
        except BaseException as e:  # noqa: BLE001
            error_holder[0] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=METADATA_TIMEOUT_S + PIECE_TIMEOUT_S + PROBE_MAX_S + 30)
    if t.is_alive():
        raise TimeoutError(
            "Magnet job exceeded the hard timeout — a probe likely wedged. "
            "The torrent may also have no live seeders."
        )
    if error_holder[0]:
        raise error_holder[0]
    return result_holder[0]
