"""Task D6: verification fixes (RB-7, RB-8, RB-12, RB-13) and ``build_audio_tracks``.

Pure-function tests: no HTTP, no tools. MediaInfo / ffprobe dict fragments are shaped
like the real tool output (the keys ``analysis.py`` already reads).
"""
from __future__ import annotations

import analysis
import filename_intel
import verification


# --- fixtures ----------------------------------------------------------------

def mi_ddp_atmos(**over):
    t = {"@type": "Audio", "Format": "E-AC-3", "Format_Commercial_IfAny": "Dolby Digital Plus",
         "Format_AdditionalFeatures": "JOC", "Channels": "6", "ChannelLayout": "L R C LFE Ls Rs",
         "Language": "en", "Default": "Yes", "BitRate": "768000"}
    t.update(over)
    return t


def mi_ddp_plain(**over):
    t = {"@type": "Audio", "Format": "E-AC-3", "Format_Commercial_IfAny": "Dolby Digital Plus",
         "Channels": "6", "Language": "fr", "Default": "Yes", "BitRate": "384000"}
    t.update(over)
    return t


def mi_truehd_atmos(**over):
    t = {"@type": "Audio", "Format": "MLP FBA", "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
         "Format_AdditionalFeatures": "16-ch", "Compression_Mode": "Lossless",
         "Channels": "8", "Language": "en", "Default": "No"}
    t.update(over)
    return t


def mi_dtshd_x(**over):
    t = {"@type": "Audio", "Format": "DTS", "Format_Commercial_IfAny": "DTS-HD Master Audio",
         "Format_AdditionalFeatures": "XLL X", "Compression_Mode": "Lossless",
         "Channels": "8", "Language": "en", "Default": "Yes"}
    t.update(over)
    return t


def mi_dtshd_ma(**over):
    t = mi_dtshd_x(**over)
    t["Format_AdditionalFeatures"] = "XLL"
    return t


def release(name_audio_claims: str = "Atmos", container: str = "mkv"):
    return filename_intel.parse_release_name(
        f"Movie.2024.2160p.UHD.BluRay.REMUX.{name_audio_claims}.HEVC-GRP.{container}")


def record(**over):
    rec = {
        "audio": "E-AC-3",
        "audio_details": "Dolby Digital Plus, 5.1, 768 kbps (2 audio tracks)",
        "media_facts": [{"label": "Container", "value": "Matroska"},
                        {"label": "Resolution", "value": "3840x2160"},
                        {"label": "Video Codec", "value": "HEVC"}],
        "bitrate_mbps": 50, "dv_profile": "None", "rpu": "No",
    }
    rec.update(over)
    return rec


def codes(report):
    return [f["code"] for f in report["flags"]]


# --- build_audio_tracks ------------------------------------------------------

def test_build_audio_tracks_ddp_joc_is_atmos():
    (t,) = analysis.build_audio_tracks([mi_ddp_atmos()], [])
    assert t["atmos"] is True and t["dtsx"] is False
    assert t["channels"] == 6
    assert t["lossless"] is False
    assert t["language"] == "en" and t["default"] is True
    assert "digital plus" in t["codec"].lower()


def test_build_audio_tracks_truehd_commercial_name_is_atmos_and_lossless():
    (t,) = analysis.build_audio_tracks([mi_truehd_atmos()], [])
    assert t["atmos"] is True and t["dtsx"] is False
    assert t["lossless"] is True
    assert t["channels"] == 8 and t["default"] is False


def test_build_audio_tracks_dtshd_xll_x_is_dtsx():
    (t,) = analysis.build_audio_tracks([mi_dtshd_x()], [])
    assert t["dtsx"] is True and t["atmos"] is False
    assert t["lossless"] is True


def test_build_audio_tracks_dtshd_ma_without_x_is_not_dtsx():
    (t,) = analysis.build_audio_tracks([mi_dtshd_ma()], [])
    assert t["dtsx"] is False and t["atmos"] is False and t["lossless"] is True


def test_build_audio_tracks_plain_ddp_51():
    (t,) = analysis.build_audio_tracks([mi_ddp_plain()], [])
    assert t["atmos"] is False and t["dtsx"] is False and t["lossless"] is False
    assert t["channels"] == 6 and t["language"] == "fr"


def test_build_audio_tracks_atmos_in_title():
    (t,) = analysis.build_audio_tracks([mi_ddp_plain(Title="English Atmos 5.1")], [])
    assert t["atmos"] is True


def test_build_audio_tracks_ffprobe_truehd_atmos_profile():
    streams = [{"codec_type": "audio", "codec_name": "truehd", "profile": "Dolby TrueHD + Dolby Atmos",
                "channels": 8, "tags": {"language": "eng"}, "disposition": {"default": 1}}]
    (t,) = analysis.build_audio_tracks([], streams)
    assert t["atmos"] is True and t["dtsx"] is False
    assert t["lossless"] is True and t["channels"] == 8
    assert t["language"] == "eng" and t["default"] is True


def test_build_audio_tracks_ffprobe_dts_x_profile():
    streams = [{"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA + DTS:X",
                "channels": 8, "tags": {}, "disposition": {}}]
    (t,) = analysis.build_audio_tracks([], streams)
    assert t["dtsx"] is True and t["atmos"] is False and t["lossless"] is True
    assert t["default"] is False


def test_build_audio_tracks_ffprobe_plain_dts_hd_ma():
    streams = [{"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA",
                "channels": 6, "tags": {}, "disposition": {}}]
    (t,) = analysis.build_audio_tracks([], streams)
    assert t["dtsx"] is False and t["lossless"] is True


def test_build_audio_tracks_mediainfo_wins_over_ffprobe():
    streams = [{"codec_type": "audio", "codec_name": "truehd", "profile": "Dolby TrueHD + Dolby Atmos",
                "channels": 8}]
    tracks = analysis.build_audio_tracks([mi_ddp_plain()], streams)
    assert len(tracks) == 1 and tracks[0]["atmos"] is False


def test_build_audio_tracks_empty():
    assert analysis.build_audio_tracks([], []) == []


# --- RB-7 / RB-8: Atmos / DTS:X against ANY track ----------------------------

def test_atmos_claim_with_ddp_default_and_truehd_atmos_track_is_not_fake():
    tracks = analysis.build_audio_tracks([mi_ddp_plain(), mi_truehd_atmos()], [])
    rec = record(audio_tracks=tracks, has_atmos=True, has_dtsx=False)
    report = verification.verify_release(rec, release("Atmos"))
    assert "fake_atmos" not in codes(report)
    assert report["trust_score"] == 100


def test_atmos_claim_with_no_atmos_anywhere_is_still_flagged():
    tracks = analysis.build_audio_tracks([mi_ddp_plain(), mi_dtshd_ma()], [])
    rec = record(audio_tracks=tracks, has_atmos=False, has_dtsx=False)
    report = verification.verify_release(rec, release("Atmos"))
    assert "fake_atmos" in codes(report)


def test_atmos_claim_not_rescued_by_a_misleading_summary_when_tracks_present():
    # Tracks are authoritative once present: the summary string mentions "atmos"
    # (e.g. a track title) but no track carries it.
    tracks = analysis.build_audio_tracks([mi_ddp_plain()], [])
    rec = record(audio_tracks=tracks, audio_details="Dolby Digital Plus, 5.1")
    assert "fake_atmos" in codes(verification.verify_release(rec, release("Atmos")))


def test_atmos_fallback_to_string_check_without_audio_tracks_key():
    # Old stored results / magnet records have no ``audio_tracks``: unchanged behaviour.
    ok = record(audio_details="Dolby TrueHD Atmos, 7.1, 4500 kbps")
    assert "audio_tracks" not in ok
    assert "fake_atmos" not in codes(verification.verify_release(ok, release("Atmos")))
    bad = record(audio_details="Dolby Digital Plus, 5.1, 768 kbps")
    assert "fake_atmos" in codes(verification.verify_release(bad, release("Atmos")))


def test_dtsx_claim_found_in_xll_x_track_is_not_fake():
    # The summary line says only "DTS-HD Master Audio": the X marker lives in
    # Format_AdditionalFeatures, which the old string check never saw (RB-8).
    tracks = analysis.build_audio_tracks([mi_ddp_plain(), mi_dtshd_x()], [])
    rec = record(audio_tracks=tracks, audio_details="DTS-HD Master Audio, 7.1, 4000 kbps (2 audio tracks)")
    report = verification.verify_release(rec, release("DTS-X"))
    assert "fake_dts_x" not in codes(report)


def test_dtsx_claim_without_dtsx_track_is_flagged():
    tracks = analysis.build_audio_tracks([mi_dtshd_ma()], [])
    rec = record(audio_tracks=tracks)
    assert "fake_dts_x" in codes(verification.verify_release(rec, release("DTS-X")))


def test_dtsx_fallback_to_string_check_without_audio_tracks_key():
    ok = record(audio_details="DTS:X, 7.1")
    assert "fake_dts_x" not in codes(verification.verify_release(ok, release("DTS-X")))
    bad = record(audio_details="DTS-HD Master Audio, 7.1")
    assert "fake_dts_x" in codes(verification.verify_release(bad, release("DTS-X")))


# --- RB-12: container ---------------------------------------------------------

def test_mp4_with_mpeg4_mediainfo_format_is_not_a_container_mismatch():
    rec = record(media_facts=[{"label": "Container", "value": "MPEG-4"},
                              {"label": "Resolution", "value": "3840x2160"},
                              {"label": "Video Codec", "value": "HEVC"}])
    report = verification.verify_release(rec, release("Atmos", "mp4"))
    assert "container_mismatch" not in codes(report)
    # no Atmos in this record's summary -> only the unrelated Atmos flag may appear
    clean = verification.verify_release(
        rec, filename_intel.parse_release_name("Movie.2024.2160p.UHD.BluRay.REMUX.HEVC-GRP.mp4"))
    assert clean["flags"] == [] and clean["trust_score"] == 100


def test_mp4_with_quicktime_format_is_not_a_container_mismatch():
    rec = record(media_facts=[{"label": "Container", "value": "QuickTime / MOV"}])
    intel = filename_intel.parse_release_name("Movie.2024.2160p.WEB-DL.mp4")
    assert "container_mismatch" not in codes(verification.verify_release(rec, intel))


def test_mkv_named_file_that_is_really_mp4_is_still_flagged():
    rec = record(media_facts=[{"label": "Container", "value": "MPEG-4"}])
    intel = filename_intel.parse_release_name("Movie.2024.2160p.WEB-DL.mkv")
    assert "container_mismatch" in codes(verification.verify_release(rec, intel))


def test_m2ts_and_ts_and_mkv_match_their_mediainfo_names():
    for fname, fmt in (("a.2160p.m2ts", "BDAV"), ("a.2160p.ts", "MPEG-TS"), ("a.2160p.mkv", "Matroska")):
        rec = record(media_facts=[{"label": "Container", "value": fmt}])
        report = verification.verify_release(rec, filename_intel.parse_release_name(fname))
        assert "container_mismatch" not in codes(report), (fname, fmt)


# --- RB-13: resolution width rules ------------------------------------------

def res_record(resolution: str):
    return record(media_facts=[{"label": "Container", "value": "Matroska"},
                               {"label": "Resolution", "value": resolution}])


def claim(res: str):
    return filename_intel.parse_release_name(f"Movie.2024.{res}.UHD.BluRay.HEVC-GRP.mkv")


def test_scope_3840x1600_claimed_2160p_is_not_flagged():
    assert "resolution_mismatch" not in codes(
        verification.verify_release(res_record("3840x1600"), claim("2160p")))


def test_1920x1080_claimed_2160p_is_flagged():
    assert "resolution_mismatch" in codes(
        verification.verify_release(res_record("1920x1080"), claim("2160p")))


def test_other_width_rule_cases():
    cases = [
        ("3840x2160", "2160p", False),
        ("3600x1500", "2160p", False),     # width >= 3600
        ("2560x1960", "2160p", False),     # height >= 1960
        ("3000x1200", "2160p", True),
        ("1920x800", "1080p", False),      # 2.40:1 scope at 1080p: width >= 1800
        ("1800x700", "1080p", False),
        ("1280x720", "1080p", True),
        ("1280x544", "720p", False),       # width >= 1200
        ("1000x500", "720p", True),
        ("640x360", "720p", True),
    ]
    for actual, claimed, flagged in cases:
        report = verification.verify_release(res_record(actual), claim(claimed))
        assert ("resolution_mismatch" in codes(report)) is flagged, (actual, claimed)


def test_unparseable_or_missing_resolution_is_ignored():
    assert "resolution_mismatch" not in codes(
        verification.verify_release(res_record("Unknown"), claim("2160p")))
    assert "resolution_mismatch" not in codes(
        verification.verify_release(record(media_facts=[]), claim("2160p")))


# --- the reviewed scratch input (bugs.py B7/B12/B13) -------------------------

def test_review_scratch_input_has_no_false_flags():
    rec = record(media_facts=[{"label": "Container", "value": "MPEG-4"},
                              {"label": "Resolution", "value": "3840x1600"},
                              {"label": "Video Codec", "value": "HEVC"}],
                 audio_tracks=analysis.build_audio_tracks([mi_ddp_plain(), mi_truehd_atmos()], []),
                 has_atmos=True, has_dtsx=False)
    intel = filename_intel.parse_release_name("Movie.2024.2160p.UHD.BluRay.REMUX.Atmos.HEVC-GRP.mp4")
    report = verification.verify_release(rec, intel)
    assert report["flags"] == [] and report["trust_score"] == 100
