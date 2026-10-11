"""Task D3: path guard for server-local paths (validate_user_path + the two endpoints).

UNC / device / remote-drive checks are syntactic or monkeypatched: no test touches a
real network host.
"""
from __future__ import annotations

import os
import subprocess

import pytest

import main
import path_guard
from path_guard import PathRejected, validate_user_path


def reject(raw, **kw) -> PathRejected:
    with pytest.raises(PathRejected) as exc:
        validate_user_path(raw, **kw)
    return exc.value


# --- raw-string and syntactic rejections (400) -------------------------------

@pytest.mark.parametrize("raw", [
    r"\\host\share\a.mkv",
    "//host/share/a.mkv",
    r"\\?\UNC\host\share\a.mkv",
    r"\\.\NUL",
    r"\/host\share\a.mkv",
    r"relative\a.mkv",
    "C:foo",
    r"\Windows",
    "/Windows",
    "NUL",
    "",
    "   ",
    '""',
    "C:\\a\x00b.mkv",
    r"C:\Windows::$DATA",
    r"C:\Windows\NUL",
    r"C:\NUL",
    r'C:\Win"dows',
    "1:\\x.mkv",
])
def test_rejected_with_400(raw):
    err = reject(raw)
    assert err.status == 400, (raw, err.status, err.message)


@pytest.mark.parametrize("raw", [
    r"\\host\share\a.mkv",
    r"C:\Windows\NUL",
    r"C:\Windows::$DATA",
    r'C:\Win"dows',
])
def test_messages_are_fixed_strings(raw):
    msg = reject(raw).message
    assert "WinError" not in msg and "Errno" not in msg
    assert "Windows" not in msg and "host" not in msg
    assert len(msg) < 80


def test_traversal_outside_root_is_403(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "out.mkv"
    outside.write_bytes(b"x")
    sneaky = f"{root}\\..\\out.mkv"
    err = reject(sneaky, allowed_roots=[str(root)])
    assert err.status == 403


def test_traversal_to_windows_is_rejected():
    err = reject(r"C:\a\..\..\Windows\x")
    assert 400 <= err.status < 500


# --- existence, kind, extension ---------------------------------------------

def test_missing_is_404(tmp_path):
    err = reject(str(tmp_path / "nope.mkv"))
    assert (err.status, err.message) == (404, "path not found")


def test_missing_parent_component_is_404(tmp_path):
    err = reject(str(tmp_path / "no" / "such" / "a.mkv"))
    assert err.status == 404


def test_file_under_a_file_is_404_or_400(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    err = reject(str(f / "b.mkv"))
    assert err.status in (400, 404)
    assert str(tmp_path) not in err.message


def test_file_where_dir_required(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert reject(str(f), must_be="dir").status == 400


def test_dir_where_file_required(tmp_path):
    assert reject(str(tmp_path), must_be="file").status == 400


@pytest.mark.parametrize("name", ["notes.txt", "win.ini", "noext", "a.mkv.txt"])
def test_unsupported_extension_is_400(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"x")
    err = reject(str(f))
    assert (err.status, err.message) == (400, "not a supported video file")


@pytest.mark.parametrize("name", ["a.mkv", "a.MP4", "a.ts", "a.m2ts", "a.hevc", "a.h265", "a.m4v", "a.M4V"])
def test_supported_extensions(tmp_path, name):
    f = tmp_path / name
    f.write_bytes(b"x")
    assert validate_user_path(str(f)) == f.resolve()


def test_real_windows_ini_is_rejected_for_extension():
    ini = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "win.ini")
    if not os.path.isfile(ini):
        pytest.skip("no win.ini")
    assert reject(ini).message == "not a supported video file"


def test_directory_needs_no_extension(tmp_path):
    assert validate_user_path(str(tmp_path), must_be="dir") == tmp_path.resolve()


# --- acceptance and normalisation -------------------------------------------

def test_quoted_and_padded_path_accepted(tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    assert validate_user_path(f'  "{f}"  ') == f.resolve()


def test_forward_slashes_are_normalised(tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    out = validate_user_path(str(f).replace("\\", "/"))
    assert out == f.resolve()
    assert "/" not in str(out)


def test_lowercase_drive_accepted(tmp_path):
    f = tmp_path / "Movie.mkv"
    f.write_bytes(b"x")
    s = str(f)
    assert validate_user_path(s[0].lower() + s[1:]) == f.resolve()


def test_unicode_and_spaces_and_specials(tmp_path):
    d = tmp_path / "Ünï cödé & 100% ^x (1)"
    d.mkdir()
    f = d / "Film – 日本語 [2160p].mkv"
    f.write_bytes(b"x")
    assert validate_user_path(str(f)) == f.resolve()


# --- allowed roots -----------------------------------------------------------

def test_inside_allowed_root(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert validate_user_path(str(f), allowed_roots=[str(tmp_path)]) == f.resolve()
    # the root itself is inside the root
    assert validate_user_path(str(tmp_path), allowed_roots=[str(tmp_path)]) == tmp_path.resolve()


def test_outside_allowed_root_is_403(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    err = reject(str(f), allowed_roots=[str(root)])
    assert (err.status, err.message) == (403, "path is outside the allowed folders")
    assert str(root) not in err.message


def test_sibling_prefix_is_not_inside_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    other = tmp_path / "media-evil"
    other.mkdir()
    f = other / "a.mkv"
    f.write_bytes(b"x")
    assert reject(str(f), allowed_roots=[str(root)]).status == 403


def test_roots_from_environment(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", f"{root};{tmp_path / 'other'}")
    assert reject(str(f)).status == 403
    inside = root / "b.mkv"
    inside.write_bytes(b"x")
    assert validate_user_path(str(inside)) == inside.resolve()


def test_empty_roots_means_any_local_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", "")
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert validate_user_path(str(f)) == f.resolve()
    assert validate_user_path(str(f), allowed_roots=[]) == f.resolve()


def test_junction_escaping_root_is_403(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.mkv").write_bytes(b"x")
    link = root / "link"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not link.exists():
        pytest.skip("mklink /J failed")
    err = reject(str(link / "secret.mkv"), allowed_roots=[str(root)])
    assert err.status == 403


def test_junction_to_unc_is_rejected(tmp_path, monkeypatch):
    # Syntactic check on the RESOLVED path: fake resolve() into a UNC path.
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(path_guard, "_resolve",
                        lambda p, strict: type(f)(r"\\host\share\a.mkv"))
    assert reject(str(f)).status == 400


# --- remote drives -----------------------------------------------------------

def test_remote_drive_rejected_unless_allowed(tmp_path, monkeypatch):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(path_guard, "_is_remote_drive", lambda drive: True)
    monkeypatch.delenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", raising=False)
    err = reject(str(f))
    assert (err.status, err.message) == (400, "network drives are not allowed")
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    assert validate_user_path(str(f)) == f.resolve()


# --- mapped network drives (VIDEOLYZER_ALLOW_REMOTE_DRIVES) ---------------------
# Z: is faked as a mapped drive that resolve() turns into \\server\share. Nothing touches the network:
# resolve is monkeypatched and the exists/is_file/is_dir probes are answered for the fake share.

FAKE_SHARE = "\\\\server\\share"


@pytest.fixture
def mapped_drive(monkeypatch):
    import pathlib
    from pathlib import Path

    def fake_resolve(p, strict):
        text = str(p)
        if text[:2].upper() == "Z:":
            return Path(FAKE_SHARE + "\\" + text[3:])
        return Path(text).resolve(strict=strict)

    def fake(name, answer):
        real = getattr(pathlib.Path, name)

        def probe(self, *a, **k):
            return answer if str(self).lower().startswith(FAKE_SHARE) else real(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, name, probe)

    fake("exists", True)
    fake("is_file", True)
    fake("is_dir", False)
    monkeypatch.setattr(path_guard, "_resolve", fake_resolve)
    monkeypatch.setattr(path_guard, "_is_remote_drive", lambda drive: drive.upper() == "Z:")
    monkeypatch.delenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", raising=False)
    monkeypatch.delenv("VIDEOLYZER_ALLOWED_ROOTS", raising=False)


def test_mapped_drive_without_the_env_var_is_a_clear_400(mapped_drive):
    err = reject("Z:\\videos\\a.mkv")
    assert (err.status, err.message) == (400, "network drives are not allowed")
    assert "server" not in err.message and "Z:" not in err.message


def test_mapped_drive_with_the_env_var_is_accepted(mapped_drive, monkeypatch):
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    got = validate_user_path("Z:\\videos\\a.mkv")
    assert str(got) == FAKE_SHARE + "\\videos\\a.mkv"


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_mapped_drive_needs_exactly_the_value_1(mapped_drive, monkeypatch, value):
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", value)
    assert reject("Z:\\videos\\a.mkv").status == 400


def test_mapped_drive_allowed_roots_given_as_the_drive_letter(mapped_drive, monkeypatch):
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    assert validate_user_path("Z:\\videos\\a.mkv", allowed_roots=["Z:\\videos"])
    assert validate_user_path("Z:\\videos\\a.mkv", allowed_roots=["Z:\\"])
    assert validate_user_path("Z:\\videos\\a.mkv", allowed_roots=["Z:"])          # drive-only root = Z:\
    assert reject("Z:\\videos\\a.mkv", allowed_roots=["Z:\\other"]).status == 403


def test_unc_result_from_a_local_drive_is_rejected_even_with_the_env_var(mapped_drive, monkeypatch, tmp_path):
    # a junction on a LOCAL drive that points at a share: the original drive is not remote
    from pathlib import Path
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setattr(path_guard, "_resolve", lambda p, strict: Path(FAKE_SHARE + "\\a.mkv"))
    err = reject(str(f))
    assert (err.status, err.message) == (400, "network and device paths are not allowed")


@pytest.mark.parametrize("raw", [FAKE_SHARE + "\\a.mkv", "//server/share/a.mkv", "\\\\?\\UNC\\server\\share\\a.mkv"])
def test_a_raw_unc_input_is_rejected_even_with_the_env_var(mapped_drive, monkeypatch, raw):
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    assert reject(raw).status == 400


@pytest.mark.parametrize("resolved", ["\\\\?\\GLOBALROOT\\Device\\x\\a.mkv", "\\\\.\\pipe\\a.mkv"])
def test_device_path_result_is_rejected_even_for_a_mapped_drive(mapped_drive, monkeypatch, resolved):
    from pathlib import Path
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    monkeypatch.setattr(path_guard, "_resolve", lambda p, strict: Path(resolved))
    assert reject("Z:\\videos\\a.mkv").status == 400


def test_mapped_drive_output_folder_with_the_env_var(mapped_drive, monkeypatch):
    # must_exist=False: the parent is resolved first and must pass the same remote-drive rule
    import pathlib
    monkeypatch.setenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES", "1")
    real_exists = pathlib.Path.exists
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda self, *a, **k: True)
    monkeypatch.setattr(pathlib.Path, "exists",
                        lambda self, *a, **k: False if str(self).endswith("newout") else real_exists(self))
    monkeypatch.setattr(path_guard.os.path, "lexists", lambda p: False)
    got = validate_user_path("Z:\\videos\\newout", must_exist=False, must_be="dir")
    assert str(got) == FAKE_SHARE + "\\videos\\newout"
    monkeypatch.delenv("VIDEOLYZER_ALLOW_REMOTE_DRIVES")
    assert reject("Z:\\videos\\newout", must_exist=False, must_be="dir").message == "network drives are not allowed"


def test_plain_long_path_prefix_is_stripped(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert validate_user_path("\\\\?\\" + str(f)) == f.resolve()
    assert reject("\\\\?\\" + str(tmp_path / "missing.mkv")).status == 404      # a normal path error, not "device path"


def test_drive_only_allowed_root_is_normalised(tmp_path, monkeypatch):
    drive = os.path.splitdrive(str(tmp_path))[0]
    seen = []
    real = path_guard._resolve
    monkeypatch.setattr(path_guard, "_resolve", lambda p, strict: (seen.append(str(p)), real(p, strict))[1])
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert validate_user_path(str(f), allowed_roots=[drive]) == f.resolve()
    assert drive + "\\" in seen                                  # the root was resolved as X:\, never as bare X:
    assert drive not in seen


def test_real_drive_type_query_runs_for_local_drive(tmp_path):
    assert path_guard._is_remote_drive(os.path.splitdrive(str(tmp_path))[0]) is False


# --- must_exist=False (output folders) --------------------------------------

def test_must_exist_false_new_child_of_existing_dir(tmp_path):
    out = validate_user_path(str(tmp_path / "newdir"), must_exist=False, must_be="dir")
    assert out == tmp_path.resolve() / "newdir"
    assert not out.exists()


def test_must_exist_false_missing_parent_is_404(tmp_path):
    assert reject(str(tmp_path / "a" / "b"), must_exist=False).status == 404


def test_must_exist_false_parent_is_a_file(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert reject(str(f / "b"), must_exist=False).status == 400


def test_must_exist_false_reserved_leaf_and_outside_root(tmp_path):
    assert reject(str(tmp_path / "NUL"), must_exist=False).status == 400
    assert reject(str(tmp_path / "NUL.txt"), must_exist=False).status == 400
    root = tmp_path / "root"
    root.mkdir()
    assert reject(str(tmp_path / "x"), must_exist=False,
                  allowed_roots=[str(root)]).status == 403


# --- walking: junctions inside a validated folder ----------------------------

def _mklink(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not link.exists():
        pytest.skip("mklink /J failed")


def test_iter_safe_video_files_skips_escaping_junction(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    (root / "notes.txt").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    _mklink(root / "link", outside)
    found = sorted(os.path.basename(p) for p in
                   path_guard.iter_safe_video_files(str(root), [str(root)]))
    assert found == ["a.mkv"]


def test_iter_safe_video_files_does_not_loop_through_a_junction_cycle(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.mkv").write_bytes(b"x")
    (root / "sub" / "b.mkv").write_bytes(b"x")
    _mklink(root / "sub" / "loop", root)               # points back at an ancestor
    found = sorted(os.path.basename(p) for p in path_guard.iter_safe_video_files(str(root), []))
    assert found == ["a.mkv", "b.mkv"]


def test_iter_safe_video_files_skips_a_second_link_to_an_already_visited_folder(tmp_path):
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.mkv").write_bytes(b"x")
    _mklink(root / "alias", root / "real")
    found = list(path_guard.iter_safe_video_files(str(root), []))
    assert len(found) == 1


def test_iter_safe_video_files_visited_set_with_a_fake_tree(tmp_path, monkeypatch):
    # fake os.walk + realpath: "root/x" and "root/y" are the same real folder
    root = str(tmp_path)
    tree = {root: (["x", "y"], []), os.path.join(root, "x"): ([], ["v.mkv"]), os.path.join(root, "y"): ([], ["v.mkv"])}

    def fake_walk(top):
        stack = [top]
        while stack:
            cur = stack.pop()
            dirs, files = tree[cur]
            dirs = list(dirs)
            yield cur, dirs, files
            stack.extend(os.path.join(cur, d) for d in dirs)         # follows what the caller left in ``dirs``

    monkeypatch.setattr(path_guard.os, "walk", fake_walk)
    monkeypatch.setattr(path_guard.os.path, "realpath",
                        lambda p: os.path.join(root, "x") if p.endswith(("x", "y")) else p)
    monkeypatch.setattr(path_guard, "validate_user_path", lambda *a, **k: None)
    assert len(list(path_guard.iter_safe_video_files(root, []))) == 1


def test_video_extension_lists_agree():
    import analysis
    assert path_guard.VIDEO_EXTENSIONS == analysis.VIDEO_EXTENSIONS
    assert ".m4v" in path_guard.VIDEO_EXTENSIONS


def test_path_job_does_not_follow_junction_out_of_root(client, tmp_path, monkeypatch):
    def fake_analyze(path, skip_dovi_scan=False):
        return {"file": os.path.basename(path), "path": path, "score": 1, "tv_score": 1,
                "confidence_score": 1, "bitrate_mbps": 1.0}

    monkeypatch.setattr(main, "analyze_file", fake_analyze)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    _mklink(root / "link", outside)
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(root))
    r = client.get("/analyze-path/", params={"path": str(root)})
    assert r.status_code == 200
    job = client.get(f"/job/{r.json()['job_id']}").json()
    assert [x["file"] for x in job["results"]] == ["a.mkv"]
    assert job["total"] == 1


def test_scan_folder_does_not_follow_junction_out_of_root(client, monkeypatch, tmp_path):
    import analysis
    seen = []

    def fake_analyze(path, skip_dovi_scan=False):
        seen.append(os.path.basename(path))
        return None

    monkeypatch.setattr(analysis, "analyze_file", fake_analyze)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    _mklink(root / "link", outside)
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(root))
    client.get("/scan-folder/", params={"path": str(root)})
    assert seen == ["a.mkv"]


# --- endpoints ---------------------------------------------------------------

def test_analyze_path_unc_is_400_and_creates_no_job(client):
    before = set(main._jobs)
    r = client.get("/analyze-path/", params={"path": r"\\host\share\x.mkv"})
    assert r.status_code == 400
    assert "detail" in r.json()
    assert set(main._jobs) == before


def test_analyze_path_non_video_file_is_400_and_creates_no_job(client, tmp_path):
    f = tmp_path / "win.ini"
    f.write_bytes(b"x")
    before = set(main._jobs)
    r = client.get("/analyze-path/", params={"path": str(f)})
    assert r.status_code == 400
    assert r.json() == {"detail": "not a supported video file"}
    assert set(main._jobs) == before


def test_analyze_path_outside_roots_is_403(client, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    monkeypatch.setenv("VIDEOLYZER_ALLOWED_ROOTS", str(root))
    r = client.get("/analyze-path/", params={"path": str(f)})
    assert r.status_code == 403
    assert not main._jobs


def test_scan_folder_missing_is_404_without_exception_text(client, tmp_path):
    missing = tmp_path / "nope"
    r = client.get("/scan-folder/", params={"path": str(missing)})
    assert r.status_code == 404
    assert r.json() == {"detail": "path not found"}
    assert "WinError" not in r.text and "Errno" not in r.text


def test_scan_folder_on_a_file_is_400(client, tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    r = client.get("/scan-folder/", params={"path": str(f)})
    assert r.status_code == 400


def test_scan_folder_unc_is_400(client):
    r = client.get("/scan-folder/", params={"path": "//host/share/dir"})
    assert r.status_code == 400


# --- fix round 1 -------------------------------------------------------------

def test_must_exist_false_junction_leaf_escaping_root_is_403(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _mklink(allowed / "jx", outside)
    for spelling in ("jx", "jx."):
        err = reject(str(allowed / spelling), allowed_roots=[str(allowed)],
                     must_exist=False, must_be="dir")
        assert err.status == 403, spelling


def test_must_exist_false_junction_leaf_returns_real_target_without_roots(tmp_path, monkeypatch):
    # No roots configured: the checks of step 5 must see the REAL target.
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _mklink(allowed / "jx", outside)
    out = validate_user_path(str(allowed / "jx"), must_exist=False, must_be="dir", allowed_roots=[])
    assert out == outside.resolve()
    seen = []
    monkeypatch.setattr(path_guard, "_check_resolved", lambda p, *a: seen.append(p))
    validate_user_path(str(allowed / "jx"), must_exist=False, must_be="dir", allowed_roots=[])
    assert outside.resolve() in seen


def test_must_exist_false_junction_leaf_to_unc_is_rejected(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _mklink(allowed / "jx", outside)
    real = path_guard._resolve

    def fake(p, strict):
        if str(p).endswith("jx"):
            return type(p)(r"\\host\share\x")
        return real(p, strict)

    monkeypatch.setattr(path_guard, "_resolve", fake)
    assert reject(str(allowed / "jx"), must_exist=False, allowed_roots=[]).status == 400


def test_must_exist_false_trailing_dot_leaf_is_normalised(tmp_path):
    out = validate_user_path(str(tmp_path / "new."), must_exist=False, must_be="dir")
    assert out == tmp_path.resolve() / "new"
    assert reject(str(tmp_path / "..."), must_exist=False).status == 400
    assert reject(str(tmp_path) + "\\..", must_exist=False).status == 400


def test_scan_folder_route_skips_unc_resolving_file_with_no_roots(client, monkeypatch, tmp_path):
    import analysis
    seen = []

    def fake_analyze(path, skip_dovi_scan=False):
        seen.append(os.path.basename(path))
        return None

    monkeypatch.setattr(analysis, "analyze_file", fake_analyze)
    monkeypatch.delenv("VIDEOLYZER_ALLOWED_ROOTS", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x")
    _mklink(root / "link", outside)
    real = path_guard._resolve

    def fake(p, strict):  # a file below the junction resolves to a network share
        if "link" in str(p):
            return type(p)(r"\\host\share\secret.mkv")
        return real(p, strict)

    monkeypatch.setattr(path_guard, "_resolve", fake)
    client.get("/scan-folder/", params={"path": str(root)})
    assert seen == ["a.mkv"]


def test_scan_folder_route_skips_remote_drive_file_with_no_roots(client, monkeypatch, tmp_path):
    import analysis
    seen = []
    monkeypatch.setattr(analysis, "analyze_file",
                        lambda path, skip_dovi_scan=False: seen.append(path))
    monkeypatch.delenv("VIDEOLYZER_ALLOWED_ROOTS", raising=False)
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    # every file looks like it is on a network drive; the folder itself is vetted
    # first (so make only file checks fail): patch after the folder check.
    calls = {"n": 0}

    def remote(drive):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(path_guard, "_is_remote_drive", remote)
    client.get("/scan-folder/", params={"path": str(root)})
    assert seen == []
    assert calls["n"] >= 2
