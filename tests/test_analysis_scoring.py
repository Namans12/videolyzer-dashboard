"""Task D8: TV scoring and playability (RB-1, RB-2, RB-6 token normalisation).

Pure-function tests: no HTTP, no tools.
"""
from __future__ import annotations

import pytest

import analysis


def tv(base, hdr="", **kw):
    args = dict(bitrate_mbps=20.5, bit_depth=10, audio_score=10,
                container_short="MP4", source="WEB-DL", hdr_summary=hdr)
    args.update(kw)
    return analysis.score_for_tv(base, **args)


# --- RB-1: non-DV base scores --------------------------------------------------

def test_non_dv_provisional_base_scores_exist():
    t = analysis.BRAVIA_8_II["dv_tv_score"]
    assert (t["HDR10"], t["HLG"], t["SDR"]) == (28, 24, 12)


def test_reproduction_input_exact_tv_score():
    # Reproduction input of the review: MP4, 20.5 Mbps, 10-bit, audio score 10
    # (DD+), WEB-DL, HDR10 HEVC, no Dolby Vision (base_profile "None").
    # Components: HDR10 base 28
    #   + bitrate 20.5 Mbps (> 20)        +7
    #   + 10-bit                          +10
    #   + audio min(10, 10)               +10
    #   + MP4 container                   +6
    #   + WEB-DL source                   +2
    #   = 63   (it was 45 before the fix: the 0-valued "None" key fell to 10)
    assert tv("None", "SMPTE ST 2086, HDR10 compatible") == (63, "Good")


@pytest.mark.parametrize("hdr,expected", [
    ("HDR10", 63),
    ("SMPTE ST 2086, HDR10 compatible", 63),
    ("PQ | BT.2020", 63),
    ("smpte2084 | bt2020", 63),          # ffprobe color_transfer
    ("HLG", 59),                          # 24 + 35
    ("arib-std-b67 | bt2020", 59),        # ffprobe color_transfer
    ("ARIB STD-B67", 59),
    ("", 47),                             # SDR: 12 + 35
    ("bt709 | bt709", 47),
])
def test_non_dv_classified_from_hdr_summary(hdr, expected):
    assert tv("None", hdr)[0] == expected


def test_hdr10_plus_is_classified_hdr10_not_sdr():
    assert tv("None", "SMPTE ST 2094 App 4, Version 1, HDR10+ Profile B compatible")[0] == 63


@pytest.mark.parametrize("profile", ["9", "10", "20"])
def test_unknown_dv_profiles_keep_default_10(profile):
    assert tv(profile, "HDR10")[0] == 10 + 35


def test_known_dv_profile_scores_unchanged():
    assert tv("8.1")[0] == 35 + 35


# --- RB-2: playable ------------------------------------------------------------

def heur(codec, hdr="", container="MKV", profile="None"):
    return analysis.tv_compatibility_heuristic(
        profile, profile, "Unknown", "Single-layer", container, codec, hdr_summary=hdr)


def rec(support):
    return analysis.build_recommendation(70, 80, 50, support, "None", "Single-layer", "MKV", "E-AC-3")


@pytest.mark.parametrize("hdr", ["HDR10", "HLG", ""])
def test_hevc_non_dv_plays_as_hdr10_hlg_sdr(hdr):
    t = heur("HEVC Main 10@L5.1", hdr)
    assert t["playable"] == "Yes"
    assert t["profile_supported"] == "No"                     # vocabulary unchanged
    assert t["note"].startswith("No Dolby Vision; plays as HDR10/HLG/SDR")
    assert "Not a suitable playback target" not in rec(t)


def test_h264_sdr_is_playable_and_recommended():
    t = heur("AVC High@L4")
    assert t["playable"] == "Yes"
    assert "Not a suitable playback target" not in rec(t)


@pytest.mark.parametrize("codec", ["MPEG Video", "VC-1"])
def test_unsupported_codec_is_still_unplayable(codec):
    t = heur(codec)
    assert t["playable"] == "No"
    assert rec(t) == "Not a suitable playback target for this TV."


def test_unsupported_container_is_unplayable():
    t = heur("HEVC Main 10", "HDR10", container="RAW HEVC")
    assert t["playable"] == "No"
    assert rec(t) == "Not a suitable playback target for this TV."


def test_dv_file_keeps_dv_vocabulary_and_plays():
    t = heur("HEVC Main 10", "Dolby Vision", container="MP4", profile="8.1")
    assert t["profile_supported"] == "Yes"
    assert t["playable"] == "Yes"


def test_hdr10_plus_note_still_appended():
    t = heur("HEVC", "SMPTE ST 2094 App 4, HDR10+ Profile B")
    assert "HDR10+ present in stream" in t["note"]


def test_old_dict_without_playable_is_not_flagged_unsuitable():
    assert "Not a suitable playback target" not in rec({"profile_supported": "No"})


# --- RB-6: E-AC-3 token --------------------------------------------------------

def test_eac3_token_scores_like_dd_plus():
    dd_plus = analysis.get_audio_quality_score("E-AC-3", "Dolby Digital Plus")
    assert dd_plus == 10
    assert analysis.get_audio_quality_score("E-AC-3", "E-AC-3, 5.1") == dd_plus
    assert analysis.get_audio_quality_score("E-AC3", "") == dd_plus


def test_dtsx_stays_at_dts_value_and_others_unchanged():
    assert analysis.get_audio_quality_score("DTS:X", "DTS:X") == \
        analysis.get_audio_quality_score("DTS", "DTS 5.1") == 6
    assert analysis.get_audio_quality_score("AC-3", "Dolby Digital") == 5
    assert analysis.get_audio_quality_score("TrueHD", "TrueHD Atmos") == 5
