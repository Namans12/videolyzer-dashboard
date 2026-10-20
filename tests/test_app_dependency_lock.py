"""Check the real staged cp311 artifacts, including native magnet closure."""
import email
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = Path(r"D:\Video Comparizer\installer\acquisitions.json")
PAYLOAD = Path(r"D:\Video Comparizer\installer\payload\plan4b")


def _validate_wheel(row, artifact, pins, environment):
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == row["sha256"]
    with zipfile.ZipFile(artifact) as wheel:
        metadata_name, = [n for n in wheel.namelist()
                          if n.count("/") == 1 and n.endswith(".dist-info/METADATA")]
        wheel_name, = [n for n in wheel.namelist()
                       if n.count("/") == 1 and n.endswith(".dist-info/WHEEL")]
        metadata = email.message_from_bytes(wheel.read(metadata_name))
        assert canonicalize_name(metadata["Name"]) == row["name"], "top-level name"
        assert metadata["Version"] == row["version"], "top-level version"
        if row["name"] == "libtorrent":
            assert b"Tag: cp311-cp311-win_amd64" in wheel.read(wheel_name)
            assert any(n.lower().endswith(".pyd") for n in wheel.namelist())
        for dependency in metadata.get_all("Requires-Dist", []):
            requirement = Requirement(dependency)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            name = canonicalize_name(requirement.name)
            assert name in pins, f"unlisted runtime dependency: {dependency}"
            assert requirement.specifier.contains(pins[name], prereleases=True)


@pytest.mark.parametrize("top_version", ["1.0", "9.0"])
def test_nested_metadata_is_ignored_but_wrong_top_level_pin_fails(tmp_path, top_version):
    artifact = tmp_path / "sample-1.0-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as wheel:
        wheel.writestr("sample-1.0.dist-info/METADATA",
                       f"Metadata-Version: 2.1\nName: sample\nVersion: {top_version}\n")
        wheel.writestr("sample-1.0.dist-info/WHEEL", "Tag: py3-none-any\n")
        wheel.writestr("sample/_vendor/foreign-999.dist-info/METADATA",
                       "Name: foreign\nVersion: 999\nRequires-Dist: unlisted>=999\n")
        wheel.writestr("sample/_vendor/foreign-999.dist-info/WHEEL", "Tag: invalid\n")
    row = {"name": "sample", "version": "1.0",
           "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    if top_version == "1.0":
        _validate_wheel(row, artifact, {"sample": "1.0"}, default_environment())
    else:
        with pytest.raises(AssertionError, match="top-level version"):
            _validate_wheel(row, artifact, {"sample": "1.0"}, default_environment())


def test_dashboard_lock_has_cp311_libtorrent_closure():
    lock = (ROOT / "requirements-app-lock.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([\w.-]+)==([^\s]+)", lock, re.M))
    assert pins["libtorrent"] == "2.0.11"
    rows = [r for r in json.loads(INVENTORY.read_text(encoding="utf-8"))["artifacts"]
            if "D" in r.get("install", [])]
    assert pins == {r["name"]: r["version"] for r in rows}
    environment = default_environment()
    environment.update(python_version="3.11", python_full_version="3.11.9",
                       sys_platform="win32", platform_system="Windows",
                       platform_machine="AMD64", os_name="nt", extra="")
    for row in rows:
        artifact = PAYLOAD / row["destination"]
        assert artifact.is_file(), f"missing staged artifact: {row['id']}"
        assert "--hash=sha256:" + row["sha256"] in lock
        _validate_wheel(row, artifact, pins, environment)
