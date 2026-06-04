# Claude Code Development Guide

Read this first when picking up work on this repo. It encodes the conventions
the codebase already follows so you don't re-litigate them.

## Code style cheatsheet

- **Python 3.10+.** Use `from __future__ import annotations`, union types
  with `|`, `dict[str, Any]` (not `Dict`), keyword-only args where sensible.
- **No giant files.** When a module passes ~600 lines, split by *responsibility*
  not by length. `analysis.py` is the exception — its size reflects a single
  cohesive responsibility (metadata fusion).
- **Logging over print.** `logger = logging.getLogger("video-analyzer.<module>")`.
  Use `logger.warning` for recoverable, `logger.exception` inside `except` for
  unrecoverable. Never use bare `except:`.
- **`try/except/finally` for any external tool.** ffprobe, MediaInfo,
  libtorrent — all can hang or crash on weird files. Always wrap in a timeout
  and clean up temp paths in `finally`.
- **Magic numbers go to named constants at module top.**
  `HEAD_BYTES = 32 * 1024 * 1024` not `_src.read(32 * 1024 * 1024)`.
- **Comments earn their place.** Comment *why*, not *what*. If a regex is
  non-obvious, show an example of what it's catching. See `magnet.py:_pieces_for_range`.

## When you need to add an external dependency

1. Check `requirements.txt` (if present) or just `pip list`.
2. **Don't add heavy deps for one feature.** `guessit` was tempting for
   filename parsing but pure regex covers our cases — the trade-off was
   covered in conversation. Pattern: prefer a 200-line `_intel.py` over a
   2 MB dep.
3. If the dep is needed only at runtime in one path, lazy-import inside
   the function (see `magnet.py:_load_libtorrent`).

## When you need to add a new tool integration (mediainfo-style)

1. Add `resolve_tool("<exe-name>", "<alt-name>")` lookup so the path is
   discovered, not hard-coded.
2. Wrap the call in `run_command(args, timeout=...)` — never `subprocess.run`
   directly; the shared wrapper handles encoding and exceptions uniformly.
3. Parse stdout (don't trust returncode — see invariant #4 in ARCHITECTURE.md).
4. Add a `ToolReport` to `build_tool_reports()` so the UI shows status.

## When you need to add a new fact to the analysis result

1. Decide: is it derived from filename or from stream?
   - Filename → `filename_intel.py` field
   - Stream → `analysis.py` getter, then thread through `_analyze_file_inner`
2. Add the field to `src/types.ts:VideoData` (or the relevant interface).
3. If it should show in the comparison matrix, add a row to
   `comparison.py:COMPARISON_ROWS`.
4. If it's verifiable, add a `_check_*` to `verification.py`.

## Testing your changes

There is no test suite. The pragmatic verification pattern is:

```bash
# 1. Backend module-level smoke
python filename_intel.py    # has __main__ self-test
python -c "from main import app; ..."   # ensures imports

# 2. Live magnet test (slow — ~2 min per magnet)
curl -X POST http://127.0.0.1:8000/compare-magnets/ \
  -H 'Content-Type: application/json' \
  -d '{"magnets":["magnet:?xt=...","magnet:?xt=..."]}'

# Poll
curl http://127.0.0.1:8000/job/<job_id>
```

When adding a parser/scorer, write a `if __name__ == "__main__":` block at
the bottom that runs a sample and prints the output. This is the project's
de facto unit testing.

## Running the stack

```bash
# Backend
uvicorn main:app --reload --port 8000

# Frontend
npm run dev    # Vite, http://localhost:5173
```

The frontend hard-codes `API = "http://127.0.0.1:8000"`. If you change the
port, update `src/App.tsx`.

## What NOT to do

- Don't add a database. The in-memory `_jobs` dict is intentional — restart
  to clear state.
- Don't auto-download torrent payload. We download head + tail pieces only
  (32 MB + 64 MB). The legal/ethical line is enforced by `prioritize_files`
  + `piece_priority` in `magnet.py`.
- Don't loosen `MAX_UPLOAD_BYTES` past 120 GB without thinking about disk.
- Don't add concurrency to libtorrent sessions on a single port. See
  invariant #2 in ARCHITECTURE.md.
- Don't strip `BRAVIA_8_II` device specs and replace with a generic device
  enum. They're tuned for this user's playback target; making them
  configurable is fine, removing the calibration is not.

## How to read the existing scoring code

`analysis.py` has *two* scoring paths that confuse first-time readers:

- `score_video()` — generic 0-100 quality, useful as a portable signal.
- `score_for_tv()` — same range, but calibrated for Sony Bravia 8 II: e.g.
  Profile 7 FEL scores lower because the TV ignores the EL.

The frontend leans on `tv_score` for the user-facing ranking. The plain
`score` is kept for CSV export and audit purposes.

`comparison.py:composite_score()` is a *third* score for cross-release
comparison — it adds trust + source tier on top of `tv_score`.

## Conventions for new MD docs

- Markdown lives in `docs/`. Top-level `CLAUDE.md` is the *product spec*.
- Use this guide's tone: dense, declarative, with tables and code blocks.
- No marketing language. No emoji in headings. No "Welcome to..." preambles.
