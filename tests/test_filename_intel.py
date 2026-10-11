"""Task D9: release-name parsing (RB-17 platform tokens, RB-18 source guess,
RB-26 None-safe bitrate in composite_score).

Pure-function tests: no HTTP, no tools.
"""
from __future__ import annotations

import pytest

import analysis
import comparison
import filename_intel


def platform(name: str):
    return filename_intel.parse_release_name(name)["platform"]


def guess(name: str, bitrate: float = 20.0, base_profile: str = "None", el: str = "Unknown") -> str:
    return analysis.guess_source(name, bitrate, base_profile, el)


# --- RB-17: platform tokens ----------------------------------------------------

def test_dts_hd_ma_is_not_movies_anywhere():
    assert platform("Movie.2015.2160p.DTS-HD.MA.7.1.x265.BluRay-GRP.mkv") is None
    assert platform("DTS-HD.MA.7.1.Movie.2015.2160p.BluRay") is None


def test_mad_max_is_not_the_max_platform():
    assert platform("Mad.Max.Fury.Road.2015.2160p.UHD.BluRay") is None


def test_max_next_to_web_tag_is_the_max_platform():
    assert platform("Show.S01.2160p.MAX.WEB-DL.DDP5.1") == "Max"


def test_nf_next_to_web_tag_is_netflix():
    assert platform("Movie.2160p.NF.WEB-DL") == "Netflix"


@pytest.mark.parametrize("name,expected", [
    ("Movie.2160p.AMZN.WEB-DL.mkv", "Amazon"),
    ("Movie.2160p.DSNP.WEB-DL.mkv", "Disney+"),
    ("Movie.2160p.ATVP.WEB-DL.mkv", "Apple TV+"),
    ("Movie.2160p.PCOK.WEB-DL.mkv", "Peacock"),
    ("Movie.2160p.PMTP.WEB-DL.mkv", "Paramount+"),
    ("Movie.2160p.iT.WEB-DL.mkv", "iTunes"),
    ("Movie.2160p.itunes.mkv", "iTunes"),
    ("Movie.2160p.AMZN.mkv", "Amazon"),                 # unambiguous: no web tag needed
    ("Movie.2160p.HMAX.WEB-DL.mkv", "HBO Max"),
    ("Movie.2160p.WEB-DL.NF.mkv", "Netflix"),           # web tag before the token
    ("Movie.2160p.Disney.WEBRip.mkv", "Disney+"),
])
def test_known_platforms_still_detected(name, expected):
    assert platform(name) == expected


@pytest.mark.parametrize("name", [
    "Movie.2160p.NF.mkv",             # ambiguous token without a web tag
    "Movie.2020.Hulu.Documentary.1080p.BluRay.mkv",
    "Stan.Lee.Doc.2020.1080p.BluRay.mkv",
    "It.2017.2160p.UHD.BluRay.mkv",
    "Movie.2160p.MAX.BluRay.mkv",     # bare max never
    "Movie.2160p.MA.BluRay.mkv",      # bare ma never
])
def test_ambiguous_tokens_need_a_web_tag(name):
    assert platform(name) is None


# --- RB-18: guess_source uses the release parser, not substrings ---------------

def test_hdr_name_is_not_web_dl_from_nf_substring():
    # "Infinity" contains "nf": the old substring test said WEB-DL.
    assert guess("Infinity.War.2018.2160p.HDR.mkv") != "WEB-DL"


def test_inferno_is_not_web_dl_from_nf_substring():
    assert guess("Inferno.2160p.mkv") != "WEB-DL"


def test_webrip_is_webrip():
    assert guess("Movie.WEBRip.mkv") == "WEBRip"


def test_nf_web_dl_still_web_dl():
    assert guess("Movie.2160p.NF.WEB-DL.mkv") == "WEB-DL"


@pytest.mark.parametrize("name,expected", [
    ("Infinity.War.2018.2160p.BluRay.mkv", "BluRay Encode"),
    ("Movie.2020.2160p.UHD.BluRay.REMUX.mkv", "REMUX"),
    ("Movie.2020.2160p.BDRemux.HEVC-GRP.mkv", "REMUX"),
    ("Movie.2020.2160p.UHD.BluRay.x265-GRP.mkv", "UHD BluRay"),
    ("Movie.2020.UHD.2160p.BluRay.x265-GRP.mkv", "UHD BluRay"),
    ("Movie.2020.1080p.BDRip.x264-GRP.mkv", "BluRay Encode"),
    ("Movie.2020.1080p.WEB.H264-GRP.mkv", "WEB-DL"),
    ("Movie.2160p.AMZN.mkv", "WEB-DL"),                  # platform token, no source tag
])
def test_guess_source_mapping(name, expected):
    assert guess(name) == expected


@pytest.mark.parametrize("bitrate,bp,el,expected", [
    (70.0, "None", "Unknown", "Likely REMUX"),
    (35.0, "None", "Unknown", "High Encode"),
    (20.0, "None", "Unknown", "WEB/Encode"),
    (8.0, "None", "Unknown", "WEB/Compressed"),
    (20.0, "7", "Yes", "Likely BluRay"),
])
def test_bitrate_heuristics_still_apply_when_name_has_no_source(bitrate, bp, el, expected):
    assert guess("Movie.2020.2160p.HEVC.mkv", bitrate, bp, el) == expected


def test_webrip_has_a_source_bonus_in_both_score_tables():
    kw = dict(bitrate_mbps=20.5, bit_depth=10, audio_score=10, container_short="MP4")
    web_dl, _ = analysis.score_for_tv("None", source="WEB-DL", hdr_summary="HDR10", **kw)
    webrip, _ = analysis.score_for_tv("None", source="WEBRip", hdr_summary="HDR10", **kw)
    none, _ = analysis.score_for_tv("None", source="Whatever", hdr_summary="HDR10", **kw)
    assert (web_dl, webrip, none) == (none + 2, none + 1, none)

    vkw = dict(base_profile="None", layer_variant="", bitrate_mbps=20.5, bit_depth=10,
               audio_score=10, container_short="MP4", tv_support="No", el="No",
               hdr_summary="HDR10")
    base = analysis.score_video(source="Whatever", **vkw)
    rip = analysis.score_video(source="WEBRip", **vkw)
    assert rip == base + 1


# --- RB-26: composite_score tolerates a None bitrate ---------------------------

def test_composite_score_with_none_bitrate_does_not_raise():
    release = {
        "analysis": {"bitrate_mbps": None, "tv_score": 50},
        "intel": {"source": "WEB-DL"},
        "verification": {"trust_score": 80},
    }
    score, reasons = comparison.composite_score(release)
    assert isinstance(score, int)
    assert reasons


def test_release_recommendation_with_none_bitrate_does_not_raise():
    # Same RB-26 family: the per-release blurb formatted the raw value with :.1f.
    release = {
        "analysis": {"bitrate_mbps": None},
        "intel": {"source": "WEB-DL"},
        "verification": {"error_count": 0},
    }
    assert "0.0 Mbps" in comparison.build_release_recommendation(release)
