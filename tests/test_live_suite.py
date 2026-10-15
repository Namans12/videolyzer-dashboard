"""Opt-in live test against the REAL installed VideoSuite (plan 2, task D12).

Skipped unless the environment variable ``VIDEOLYZER_LIVE_SUITE`` is ``1``::

    $env:VIDEOLYZER_LIVE_SUITE = "1"
    # optional: point at a suite other than the one in setup\\env.path
    $env:VIDEOSUITE_PYTHON = "D:\\VideoComparatorPro\\.venv\\Scripts\\python.exe"
    $env:VIDEOSUITE_ROOT   = "D:\\VideoComparatorPro"
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_live_suite.py -q

It runs ``devices`` and, when an ffmpeg binary is found, ``score`` on ONE tiny clip generated under
``tmp_path`` (1 second, 640x360 test pattern). The record cache of the suite is redirected into
``tmp_path`` (``VC_CACHE_DIR``) so nothing is written to the user's cache. No owner media is read,
no torrent is started.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from suite_client import SuiteClient, SuiteError, SuiteUnavailable

pytestmark = pytest.mark.skipif(os.environ.get("VIDEOLYZER_LIVE_SUITE") != "1",
                                reason="live suite test: set VIDEOLYZER_LIVE_SUITE=1 to run it")


def _suite_client(tmp_path: Path) -> SuiteClient:
    try:
        return SuiteClient(env={"VC_CACHE_DIR": str(tmp_path / "suite_cache"),
                                "VIDEOSUITE_SETTINGS": str(tmp_path / "no_settings.json")})
    except SuiteUnavailable as exc:
        pytest.skip(f"VideoSuite is not installed or configured: {exc}")


def _find_ffmpeg(client: SuiteClient) -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for base in filter(None, [client.root]):                    # the suite's own bundled ffmpeg, if any
        for sub in (("ffmpeg", "bin"), ("tools", "bin")):
            candidate = Path(base, *sub, "ffmpeg.exe")
            if candidate.is_file():
                return str(candidate)
    return None


def test_live_devices(tmp_path):
    doc = _suite_client(tmp_path).devices()
    assert doc["schema"] == "videosuite-devices/1"
    assert isinstance(doc["tool_version"], str)
    assert doc["devices"], "the suite lists no device"
    for device in doc["devices"]:
        assert isinstance(device["id"], str) and device["id"]
        assert device["players"], f"{device['id']} has no player"
        assert all(isinstance(p["id"], str) and p["id"] for p in device["players"])
    assert doc.exit_code == 0


def test_live_version(tmp_path):
    import suite_version
    client = _suite_client(tmp_path)
    assert suite_version.parse_version(client.version()["tool_version"]) is not None
    assert suite_version.check(client)["status"] == "ok"


def test_live_score_on_one_generated_clip(tmp_path):
    client = _suite_client(tmp_path)
    ffmpeg = _find_ffmpeg(client)
    if ffmpeg is None:
        pytest.skip("ffmpeg not found on PATH or in the suite folder")
    clip = tmp_path / "live clip (a&b).mkv"                       # a space and an & on purpose: no shell may see them
    result = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=1",
         "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p", str(clip)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False)
    if result.returncode != 0 or not clip.is_file():
        pytest.skip("ffmpeg could not generate the test clip")

    try:
        doc = client.score([str(clip)])
    except SuiteError as exc:
        if exc.kind == "ffprobe_missing":                         # an environment gap, not a Dashboard failure
            pytest.skip("the suite cannot find ffprobe")
        raise
    assert doc["schema"] == "videosuite-score/1"
    assert len(doc["records"]) == 1
    record = doc["records"][0]
    assert record["video"]["codec"] == "h264"
    assert record["video"]["width"] == 640
    assert record["playback"], "no per-device playback verdicts"
    assert doc.exit_code == 0


def test_live_score_partial(tmp_path):
    client = _suite_client(tmp_path)
    ffmpeg = _find_ffmpeg(client)
    if ffmpeg is None:
        pytest.skip("ffmpeg not found on PATH or in the suite folder")
    clip = tmp_path / "generated full clip.mkv"
    result = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=3",
         "-c:v", "libx264", "-crf", "26", "-pix_fmt", "yuv420p", str(clip)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, check=False, shell=False)
    assert result.returncode == 0 and clip.is_file(), "ffmpeg could not generate the test clip"
    full = clip.read_bytes()
    partial = tmp_path / "partial clip (a&b).mkv"
    partial.write_bytes(full[:len(full) * 2 // 3])
    doc = client.score_partial(str(partial), real_size=len(full), real_name="Tom & Jerry (2021) 100%.mkv")
    assert doc["records"][0]["source"]["partial"] is True
    assert doc.exit_code == 0
