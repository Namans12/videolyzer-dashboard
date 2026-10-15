"""Regenerate contract fixtures from isolated, generated media and the real suite."""
from __future__ import annotations

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


def main():
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
        for name, doc in documents.items():
            if doc["tool_version"] != "0.4.0":
                raise RuntimeError(f"{name}: expected tool_version 0.4.0")
            (destination / (name + ".json")).write_text(
                json.dumps(normalize(doc, temporary), indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
            print(f"{name}.json: tool_version {doc['tool_version']}")
    finally:
        shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
