"""Regenerate contract fixtures from isolated, generated media and the real suite.

Legacy mode (no flags) rebuilds ``tests/fixtures/suite`` (the 0.4.0 backwards-compat fixtures) from generated clips
and checks every document against the suite's actual version handshake.

``--plan4b`` rebuilds ``tests/fixtures/suite4b`` from the actual VideoSuite CLI emitters under the shared
deterministic harness (Comparizer ``tests/unit/test_videosuite_cli.py::emit_plan4b_documents``: virtual inputs
``C:\\plan4b-fixture-inputs\\A/B/C.mkv`` with fixed sizes/mtimes fed to the real record/candidate/digest code, fixed
facts, fixed pair outcomes and clock). ``--plan4b --check`` writes nothing: it runs two independent generations in
separate processes with fresh scratch roots, validates every document with the Dashboard parsers, recomputes record
IDs and snapshot digests, requires both generations and the checked-in fixtures to be byte-identical canonical JSON,
and runs a semantic-mutation negative control (A's known HDR fact changed) that must be detected as drift. Any drift
exits nonzero. Only transient scratch/report-root prefixes are normalized (``<SCRATCH>``), by the emitter itself.

Needs ``VIDEOSUITE_ROOT`` (the Comparizer checkout) and ``VIDEOSUITE_PYTHON`` (its test interpreter), or
``--suite-root``/``--suite-python``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from suite_client import SuiteClient


def find_ffmpeg(client):
    configured = os.environ.get("FFMPEG")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    if client.root:
        for sub in (("ffmpeg", "bin"), ("tools", "bin")):
            candidate = Path(client.root, *sub, "ffmpeg.exe")
            if candidate.is_file():
                return str(candidate)
    raise SystemExit("ffmpeg not found: set FFMPEG or put ffmpeg on PATH")


def normalize(value, temporary):
    if isinstance(value, str):
        for form in (str(temporary), str(temporary).replace("\\", "/")):
            value = re.sub(re.escape(form), lambda _: r"C:\fixtures\suite", value, flags=re.IGNORECASE)
        return value
    if isinstance(value, list):
        return [normalize(item, temporary) for item in value]
    if isinstance(value, dict):
        return {normalize(key, temporary): normalize(item, temporary) for key, item in value.items()}
    return value


def score_partial(client, partial, size):
    return client._with_list("score", [str(partial)], [
        "--real-size=" + str(3 * size),
        "--real-name=Movie.2160p.WEB-DL.DDP5.1.mkv", "--coverage=head"],
        client._timeout_score_rank(1, False))


def legacy():
    temporary = Path(tempfile.mkdtemp(prefix="suite_fixtures_"))
    try:
        client = SuiteClient(env={"VC_CACHE_DIR": str(temporary / "cache"),
                                  "VIDEOSUITE_SETTINGS": str(temporary / "no_settings.json")})
        ffmpeg = find_ffmpeg(client)
        paths = []
        for name, crf in (("clip_a_crf18.mkv", 18), ("clip_b_crf34.mkv", 34), ("clip_c_crf26.mp4", 26)):
            clip = temporary / name
            subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=640x360:rate=24:duration=10", "-c:v", "libx264",
                            "-crf", str(crf), "-pix_fmt", "yuv420p", str(clip)],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=True)
            paths.append(str(clip))
        documents = {"devices": client.devices(), "version": client.version(), "score": client.score(paths)}
        reports = str(temporary / "reports")
        documents["compare"] = client.compare(paths, reports, mode="fast")
        documents["rank"] = client.rank(paths, reports)
        original = Path(paths[0]).read_bytes()
        partial = temporary / "clip_a_crf18.mkv.__probe__.mkv"
        partial.write_bytes(original[:len(original) // 2])
        documents["score_partial"] = score_partial(client, partial, len(original))
        destination = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "suite"
        # Every document must come from the suite that answered the actual version handshake (no hard-coded
        # version): the checked-in legacy files remain the 0.4.0 backwards-compat fixtures.
        handshake = documents["version"]["tool_version"]
        for name, doc in documents.items():
            if doc["tool_version"] != handshake:
                raise RuntimeError(f"{name}: tool_version {doc['tool_version']} differs from the handshake {handshake}")
            (destination / (name + ".json")).write_text(
                json.dumps(normalize(doc, temporary), indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
            print(f"{name}.json: tool_version {doc['tool_version']}")
    finally:
        shutil.rmtree(temporary)


# ── Plan 4B: actual-emitter fixtures and the cross-repo drift check ──────────────

PLAN4B_FIXTURES = {
    "version": "version", "compare_plan": "compare-plan", "compare_all": "compare", "rank_overall": "rank",
    "rank_device": "rank", "quick_overall": "quick", "quick_device": "quick", "quick_both": "quick",
    "quick_partial": "quick", "quick_just_player_alternatives": "quick",
}
PLAN4B_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "suite4b"
_EMIT = ("import json,sys; from pathlib import Path; from test_videosuite_cli import emit_plan4b_documents; "
         "docs = emit_plan4b_documents(Path(sys.argv[1]), mutate=sys.argv[3] == '1'); "
         "Path(sys.argv[2]).write_text(json.dumps(docs, ensure_ascii=False), encoding='utf-8')")


class Drift(Exception):
    """A generated document differs from another generation or from a checked-in fixture."""


def canonical(doc) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def isolated_env(root: Path, suite_root: Path) -> dict:
    """No inherited PYTHON*; every execution-protocol override points into this generation's own scratch root."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    for key, name in (("VIDEOSUITE_SETTINGS", "settings.json"), ("VIDEOSUITE_CALIBRATION", "calibration.json"),
                      ("VC_CACHE_DIR", "cache"), ("VIDEOLYZER_DATA_ROOT", "app"), ("TORCH_HOME", "torch"),
                      ("HF_HOME", "hf"), ("CLIP_CACHE_DIR", "clip")):
        env[key] = str(root / name)
    env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INDEX="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(suite_root), str(suite_root / "tests" / "unit")]))
    return env


def generate(python: Path, suite_root: Path, scratch: Path, name: str, *, mutate: bool) -> dict:
    """One independent generation in its own process and fresh scratch root."""
    root = scratch / name
    root.mkdir(parents=True, exist_ok=False)
    out = scratch / (name + ".json")
    result = subprocess.run([str(python), "-B", "-c", _EMIT, str(root / "emit"), str(out), "1" if mutate else "0"],
                            env=isolated_env(root, suite_root), cwd=str(suite_root), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                            shell=False)
    if result.returncode != 0:
        raise RuntimeError(f"generation {name} failed: {result.stderr[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def validate_generation(docs: dict, handshake_version: str, label: str, scratch: Path) -> None:
    """Dashboard parsers + real identities: record ids and snapshot digests are recomputed here, never trusted."""
    import comparison_contracts
    import suite_client
    if list(docs) != list(PLAN4B_FIXTURES):
        raise Drift(f"{label}: unexpected document set {sorted(docs)}")
    for name, command in PLAN4B_FIXTURES.items():
        try:
            suite_client.validate_document(command, json.loads(canonical(docs[name])))
        except suite_client.SuiteError as exc:
            raise Drift(f"{label}: {name} fails the Dashboard contract: {exc.message}") from None
        if docs[name]["tool_version"] != handshake_version:
            raise Drift(f"{label}: {name} tool_version differs from the handshake {handshake_version}")
    plan = docs["compare_plan"]
    for candidate in plan["candidates"]:
        if candidate["status"] == "ready" and candidate["id"] != comparison_contracts.record_id(
                candidate["path"], candidate["size"], candidate["mtime_ns"]):
            raise Drift(f"{label}: compare_plan record id does not match its path/size/mtime")
    digest = comparison_contracts.records_digest(plan["records"], plan["candidates"], plan["records"][0]["facts_version"])
    if digest != plan["records_digest"]:
        raise Drift(f"{label}: compare_plan records_digest is not the digest of its records")
    for name in ("compare_all", "rank_overall", "rank_device"):
        if docs[name]["records_digest"] != digest:
            raise Drift(f"{label}: {name} records_digest differs from the plan's")
    text = canonical(docs).lower()
    for form in (str(scratch), str(scratch).replace("\\", "/")):
        if form.lower() in text or json.dumps(form)[1:-1].lower() in text:
            raise Drift(f"{label}: a transient scratch path leaked into a document")


def handshake(python: Path, suite_root: Path, scratch: Path) -> dict:
    import suite_version
    from comparison_contracts import require_capabilities
    env = isolated_env(scratch / "handshake", suite_root)
    environ = {k: v for k, v in env.items() if k != "PYTHONPATH"}
    client = SuiteClient(argv_prefix=[str(python), "-m", "videosuite"], root=str(suite_root),
                         env={k: env[k] for k in ("VIDEOSUITE_SETTINGS", "VIDEOSUITE_CALIBRATION", "VC_CACHE_DIR")},
                         environ=environ)
    info = suite_version.check(client)
    require_capabilities(info, suite_version.UNIFIED_CAPABILITIES)
    return info


def plan4b(args) -> int:
    suite_root = Path(args.suite_root or os.environ.get("VIDEOSUITE_ROOT", ""))
    python = Path(args.suite_python or os.environ.get("VIDEOSUITE_PYTHON", ""))
    if not (suite_root / "videosuite").is_dir() or not (suite_root / "tests" / "unit" / "test_videosuite_cli.py").is_file():
        print("VIDEOSUITE_ROOT must be the Comparizer checkout (with tests/unit)", file=sys.stderr)
        return 2
    if not python.is_file():
        print("VIDEOSUITE_PYTHON must be the suite's test interpreter", file=sys.stderr)
        return 2
    fixtures = Path(args.fixtures) if args.fixtures else PLAN4B_DIR
    owned = args.scratch is None
    scratch = Path(tempfile.mkdtemp(prefix="plan4b-g")) if owned else Path(args.scratch)
    if not owned:
        scratch.mkdir(parents=True, exist_ok=False)          # always a fresh, empty, owned root
    try:
        info = handshake(python, suite_root, scratch)
        print(f"handshake: VideoSuite {info['tool_version']} with the unified capabilities")
        first = generate(python, suite_root, scratch, "gen-1", mutate=False)
        second = generate(python, suite_root, scratch, "gen-2", mutate=False)
        validate_generation(first, info["tool_version"], "generation 1", scratch)
        validate_generation(second, info["tool_version"], "generation 2", scratch)
        differing = [n for n in PLAN4B_FIXTURES if canonical(first[n]) != canonical(second[n])]
        if differing:
            raise Drift("independent generations differ: " + ", ".join(differing))
        print(f"two independent generations match ({len(first)} documents)")
        if args.check:
            drifted = []
            for name in PLAN4B_FIXTURES:
                path = fixtures / (name + ".json")
                try:
                    checked_in = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    checked_in = None
                if checked_in is None or canonical(checked_in) != canonical(first[name]):
                    drifted.append(name)
            if drifted:
                raise Drift("checked-in fixtures drifted: " + ", ".join(drifted))
            print("checked-in fixtures match the actual emitters")
            reference = {n: json.loads((fixtures / (n + ".json")).read_text(encoding="utf-8")) for n in PLAN4B_FIXTURES}
        else:
            fixtures.mkdir(parents=True, exist_ok=True)
            for name in PLAN4B_FIXTURES:
                (fixtures / (name + ".json")).write_text(json.dumps(first[name], indent=1, ensure_ascii=False) + "\n",
                                                         encoding="utf-8", newline="\n")
                print(f"{name}.json: tool_version {first[name]['tool_version']}")
            reference = first
        mutated = generate(python, suite_root, scratch, "mutated", mutate=True)
        validate_generation(mutated, info["tool_version"], "mutated generation", scratch)   # digests recomputed by real code
        changed = [n for n in PLAN4B_FIXTURES if canonical(mutated[n]) != canonical(reference[n])]
        if "compare_plan" not in changed or mutated["compare_plan"]["records_digest"] == reference["compare_plan"]["records_digest"]:
            raise Drift("negative control failed: a semantic input change was not detected")
        print("semantic mutation detected as drift: " + ", ".join(changed))
        return 0
    except Drift as exc:
        print("DRIFT: " + str(exc))
        return 1
    except Exception as exc:                       # handshake/capability/generation failure: never a silent pass
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan4b", action="store_true", help="the Plan 4B actual-emitter fixtures (tests/fixtures/suite4b)")
    parser.add_argument("--check", action="store_true", help="with --plan4b: write nothing, fail on any drift")
    parser.add_argument("--scratch", help="a NEW folder for the generations (default: a private temp folder, removed)")
    parser.add_argument("--fixtures", help="fixture folder to write/check (default tests/fixtures/suite4b)")
    parser.add_argument("--suite-root", help="Comparizer checkout (default VIDEOSUITE_ROOT)")
    parser.add_argument("--suite-python", help="suite test interpreter (default VIDEOSUITE_PYTHON)")
    args = parser.parse_args(argv)
    if args.plan4b:
        return plan4b(args)
    if args.check:
        parser.error("--check needs --plan4b")
    legacy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
