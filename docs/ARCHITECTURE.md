# Architecture

A reference for future Claude Code sessions. Skim this before touching the
backend; it'll save you from re-deriving the data flow.

## One-paragraph mental model

A FastAPI backend wraps three offline tools (MediaInfo, ffprobe, dovi_tool) and
one online tool (libtorrent). Every user-facing request becomes a **job** stored
in an in-process dict (`_jobs`) and runs in a `BackgroundTasks` thread. The
frontend polls `/job/{job_id}` until `status == "done"` or `"error"`, then
renders the result. There is no database — restart the server and jobs vanish.

## Module map

```
main.py             FastAPI routes, job orchestration, SSE streaming.
analysis.py         analyze_file() — MediaInfo + ffprobe + dovi_tool fusion.
magnet.py           libtorrent magnet metadata + head/tail piece download.
filename_intel.py   Pure-regex parser for release names → traits dict.
verification.py     Cross-check filename traits vs stream metadata → flag list.
comparison.py       Multi-release side-by-side matrix + winner selection.
```

Frontend is one big `src/App.tsx` (~60 KB) — be ready to grep before editing.

## Data flow: single magnet

```
POST /magnet/  { magnet }
    ↓                                  (background task)
_run_magnet_job
    → magnet.fetch_magnet_metadata     # libtorrent session, head/tail pieces
    → analysis.analyze_file × each video
        → mediainfo + ffprobe (parallel)
        → dovi_tool (if HEVC + DV signaled)
    → comparison.enrich_release × each result
        → filename_intel.parse_release_name
        → verification.verify_release
    → store in _jobs[id]
    ↓
Client polls /job/{id} or SSE /job/{id}/events
```

## Data flow: multi-magnet compare

```
POST /compare-magnets/  { magnets: [...] }
    ↓
_run_compare_job
    ├─ ≤2 magnets: sequential, one libtorrent session on port 6881
    └─ 3-8 magnets: ThreadPoolExecutor(max=2), each session on its own port
                    starting at 6881+i to avoid collisions
    → per magnet: fetch_magnet_metadata + analyze best file
    → comparison.compare_releases([enriched, ...])
       → side-by-side matrix + winner with reasons
    → store on _jobs[id].comparison
```

## Job record shape

```python
_jobs[job_id] = {
    "status":   "running" | "done" | "error",
    "progress": "i/total",
    "current":  "currently-processing filename",
    "total":    int,
    "results":  list[VideoData],     # raw analyze_file outputs
    "enriched": list[EnrichedRelease],  # only for /magnet/
    "error":    str | None,
    "events":   [ {msg, ts}, ... ],  # SSE timeline
    # Magnet-only fields:
    "magnet_files":   list[MagnetFile],
    "magnet_torrent": {name, info_hash} | None,
    # Compare-only fields:
    "per_magnet": list[PerMagnetRecord],
    "comparison": ComparisonPayload | None,
    "winner":     {winner_index, winner_name, reasons} | None,
}
```

## Critical invariants

1. **`analyze_file()` is the single source of truth for stream metadata.**
   Anything filename-derived is a *hint*; the verification engine treats
   stream metadata as ground truth when they disagree.

2. **libtorrent's listen port must be unique per concurrent session.**
   See `magnet.py:fetch_magnet_metadata(listen_port=...)`. If you spawn
   parallel magnet jobs without distinct ports, only the first one binds
   and the rest silently fail to receive metadata.

3. **The probe file uses `truncate()` + sparse flag.** See `magnet.py`
   near `fsutil sparse setflag`. On NTFS, `truncate()` alone allocates
   the full file size and fills the temp drive for big REMUXes.

4. **MediaInfo's exit code is unreliable on partial files.** We parse stdout
   as JSON regardless of returncode. `analysis.py:run_mediainfo` documents this.

5. **DV profile detection has three sources of truth.** In priority order:
   dovi_tool RPU scan → ffprobe `side_data_list` → MediaInfo `HDR_Format`.
   See `analysis.py:inspect_dolby_vision`.

## Common pitfalls

- **Don't share libtorrent sessions across threads.** Create one session per
  job; tear it down in `finally`.
- **`os.path.splitext()` on a name containing dots is sharp.** A name like
  `Movie.2024.x265.MKV.mkv` returns `(".mkv", ...)` as expected, but be
  careful with custom truncation.
- **Sparse files only work on NTFS/ReFS.** If a user puts their temp on
  FAT32/exFAT, the truncate fallback will allocate full size and fail
  with ENOSPC.
- **The frontend caches the last result set in `localStorage`** under the
  key `last_results`. Stale data after a code change → tell the user to
  clear it from devtools.

## Where to add things

| You want to…                              | Edit                          |
|-------------------------------------------|-------------------------------|
| Detect a new HDR format                   | `analysis.py:get_hdr_summary` + `filename_intel.py` HDR section |
| Add a new release-name token              | `filename_intel.py:PLATFORM_TOKENS` etc. |
| Add a fake-release check                  | New `_check_*` in `verification.py`, then list in `verify_release` |
| Score a new release feature               | `comparison.py:composite_score` |
| Add a new comparison row                  | `comparison.py:COMPARISON_ROWS` |
| Add a new API route                       | `main.py`, follow `/magnet/` pattern |
| Add a UI panel                            | `src/App.tsx`, find a sibling section by greping `dashboard-section` |
