"""Task D7: Dolby Vision parsing fixes (RB-3, RB-4, RB-5, RB-15, RB-16, RB-19, RB-28).

Pure-function tests: no HTTP, no tools. ``estimate_layer_variant_from_bitrate`` is
patched out so no ffprobe is ever started.
"""
from __future__ import annotations

import pytest

import analysis


@pytest.fixture(autouse=True)
def no_bitrate_probe(monkeypatch):
    monkeypatch.setattr(analysis, "estimate_layer_variant_from_bitrate", lambda _p: None)


def layer(filename: str, **kw):
    return analysis.detect_layer_variant("C:\\x\\" + filename, "7", "Yes", None, None, **kw)


# --- RB-4: whole-word FEL/MEL filename tokens ---------------------------------

def test_fellowship_is_not_fel():
    variant, _ = layer("The.Fellowship.of.the.Ring.2001.2160p.UHD.BluRay.DV.P7.mkv")
    assert variant == "Dual-layer (FEL/MEL unknown)"


@pytest.mark.parametrize("name", ["Camelot.mkv", "Melancholia.2011.mkv", "Camelot.2160p.DV.P7.mkv"])
def test_camelot_and_melancholia_are_not_mel(name):
    variant, _ = layer(name)
    assert variant == "Dual-layer (FEL/MEL unknown)"


@pytest.mark.parametrize("name,expected", [
    ("Movie.2160p.DV.P7.FEL.mkv", "FEL"),
    ("Movie.2160p.DV.P7.MEL.mkv", "MEL"),
    ("Movie 2160p DV P7 fel.mkv", "FEL"),
    ("Movie-2160p-DV-P7-mel-GRP.mkv", "MEL"),
])
def test_whole_word_token_sets_variant_but_is_unverified(name, expected):
    variant, reason = layer(name)
    assert variant == expected
    assert reason == "filename hint (unverified)"


# --- RB-28: a filename-derived layer earns no confidence ----------------------

def _confidence(layer_variant, layer_reason=""):
    return analysis.score_confidence(
        mediainfo_data=None, ffprobe_data=None, video_track=None, ffprobe_video=None,
        dovi_scan={"status": "partial"}, layer_variant=layer_variant, tv_support="Partial",
        layer_reason=layer_reason,
    )


def test_filename_only_fel_confidence_equals_unknown_layer():
    unknown = _confidence("Dual-layer (FEL/MEL unknown)", "Dual-layer DV detected; metadata cannot prove FEL vs MEL.")
    filename = _confidence("FEL", "filename hint (unverified)")
    assert filename == unknown


def test_verified_layer_still_adds_confidence():
    verified = _confidence("FEL", "dovi_tool reports FEL")
    unknown = _confidence("Dual-layer (FEL/MEL unknown)", "")
    assert verified == unknown + 8   # +6 verified instead of -2


def test_score_confidence_old_call_signature_still_works():
    # layer_reason is optional: the old behaviour (+6 for a known layer) is kept
    assert _confidence("FEL") == _confidence("Dual-layer (FEL/MEL unknown)") + 8


# --- RB-5: "FEL/MEL unknown" is not MEL ---------------------------------------

def test_unknown_layer_is_not_treated_as_mel_in_recommendation():
    rec = analysis.build_recommendation(
        70, 80, 50, {"profile_supported": "Partial"}, "7", "Dual-layer (FEL/MEL unknown)", "", "")
    assert "Consider converting to P8.1 MP4" not in rec
    assert "Strong source" in rec


def test_real_mel_still_gets_the_conversion_hint():
    rec = analysis.build_recommendation(
        70, 80, 50, {"profile_supported": "Partial"}, "7", "MEL", "", "")
    assert "Consider converting to P8.1 MP4" in rec


# --- RB-15: AVC and alternative HEVC DV codec strings -------------------------

@pytest.mark.parametrize("text,expected", [
    ("dvhe.07.06", "7"),
    ("dvav.04", "4"),
    ("dvav.09.03", "9"),
    ("dvh1.05", "5"),
    ("dva1.09", "9"),
    ("Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU", "8"),
    ("Dolby Vision, Version 1.0, Profile 8.1", "8"),
    ("no dolby here", "None"),
])
def test_parse_profile_prefixes(text, expected):
    assert analysis.parse_profile(text) == expected


# --- RB-16: dovi_tool "Profile: 7 (FEL)" --------------------------------------

DOVI_TEXT = "Summary:\n  Frames: 30\n  Profile: 7 (FEL)\n  DM version: 1\n"


def test_parse_dovi_summary_splits_number_and_layer():
    s = analysis.parse_dovi_summary(DOVI_TEXT)
    assert s["profile"] == "7 (FEL)"          # verbatim kept
    assert s["profile_number"] == "7"
    assert s["layer"] == "FEL"


@pytest.mark.parametrize("text,num,lay", [
    ("Summary:\n  Profile: 8.1\n", "8.1", None),
    ("Summary:\n  Profile: 7 (MEL)\n", "7", "MEL"),
    ("Summary:\n  Profile: 7.6 FEL\n", "7.6", "FEL"),
])
def test_parse_dovi_summary_tolerant(text, num, lay):
    s = analysis.parse_dovi_summary(text)
    assert s["profile_number"] == num
    assert s["layer"] == lay


def test_inspect_dolby_vision_uses_dovi_tool_layer():
    video_track = {"HDR_Format": "Dolby Vision", "HDR_Format_Settings": "BL+EL+RPU"}
    dovi_scan = {"status": "partial", "summary": analysis.parse_dovi_summary(DOVI_TEXT)}
    info = analysis.inspect_dolby_vision("C:\\x\\Movie.2160p.mkv", video_track, None, dovi_scan)
    assert info["base_profile"] == "7"
    assert info["layer_variant"] == "FEL"
    assert "dovi_tool" in info["layer_reason"]


def test_dovi_tool_layer_wins_over_filename():
    video_track = {"HDR_Format": "Dolby Vision", "HDR_Format_Settings": "BL+EL+RPU"}
    dovi_scan = {"status": "partial", "summary": analysis.parse_dovi_summary(DOVI_TEXT)}
    info = analysis.inspect_dolby_vision("C:\\x\\Movie.2160p.MEL.mkv", video_track, None, dovi_scan)
    assert info["layer_variant"] == "FEL"


def test_detect_layer_variant_explicit_layer_wins():
    variant, reason = analysis.detect_layer_variant(
        "C:\\x\\Movie.MEL.mkv", "7", "Yes", None, None, layer="FEL")
    assert variant == "FEL"
    assert "dovi_tool" in reason


# --- RB-3: DV in MKV warning ---------------------------------------------------

def _usb(container, dv_profile=None, codec="HEVC Main 10"):
    kw = {} if dv_profile is None else {"dv_profile": dv_profile}
    return analysis.check_usb_compatibility(container, codec, "E-AC3", 10, "3840x2160", 20, **kw)


def test_dv_in_mkv_warns_when_profile_present():
    warnings = _usb("MKV", "8.1")["warnings"]
    assert any("DV in MKV" in w for w in warnings)


def test_dv_in_mp4_and_non_dv_mkv_do_not_warn():
    assert not any("DV in MKV" in w for w in _usb("MP4", "8.1")["warnings"])
    assert not any("DV in MKV" in w for w in _usb("MKV", "None")["warnings"])
    assert not any("DV in MKV" in w for w in _usb("MKV")["warnings"])   # old call signature


def test_analyze_call_site_passes_dv_profile():
    import inspect
    src = inspect.getsource(analysis._analyze_file_inner)
    assert 'dv_profile=dv_info["profile"]' in src


# --- RB-19 (hardening): bare profile 8 ------------------------------------------

def test_tv_heuristic_bare_profile_8_maps_to_8x_entry():
    tv = analysis.tv_compatibility_heuristic("8", "8", "No", "Single-layer", "MKV", "HEVC")
    assert tv["profile_supported"] == "Yes"
    assert "Generic Profile 8" in tv["note"]


# --- final fix wave: insight text and recommendation ----------------------------------

def _insights(variant, reason=None):
    kw = {} if reason is None else {"layer_reason": reason}
    return analysis.build_insights(
        profile="7", layer_variant=variant, bitrate_mbps=40.0, source="UHD BluRay", audio_details="TrueHD",
        subtitle_count=0, tv_support_note="n/a", dovi_scan={}, confidence=80, **kw)[1]


@pytest.mark.parametrize("variant", ["FEL", "MEL"])
def test_insights_do_not_blame_the_filename_for_a_dovi_tool_layer(variant):
    text = _insights(variant, f"dovi_tool reports {variant}.")
    assert "filename hint" not in text
    assert f"{variant} confirmed by dovi_tool" in text


@pytest.mark.parametrize("variant", ["FEL", "MEL"])
def test_insights_say_filename_hint_only_for_a_filename_hint(variant):
    text = _insights(variant, analysis.FILENAME_HINT_REASON)
    assert f"{variant} detected via filename hint" in text


@pytest.mark.parametrize("variant,reason", [("FEL", "Bitrate-ratio heuristic suggests FEL (EL >= 15% of BL)."),
                                            ("MEL", "Bitrate-ratio heuristic suggests MEL (EL < 15% of BL).")])
def test_insights_name_the_bitrate_heuristic(variant, reason):
    text = _insights(variant, reason)
    assert "filename hint" not in text and "bitrate-ratio heuristic" in text


def test_insights_without_a_reason_make_no_claim_about_the_source():
    text = _insights("FEL")
    assert "filename" not in text and "FEL" in text


def test_analyze_call_site_passes_layer_reason_to_build_insights():
    import inspect
    src = inspect.getsource(analysis._analyze_file_inner)
    call = src[src.index("build_insights("):]
    call = call[:call.index(")\n")]
    assert 'layer_reason=dv_info["layer_reason"]' in call


def test_recommendation_gives_no_dv_advice_for_a_non_dv_mkv():
    rec = analysis.build_recommendation(
        70, 80, 50, {"playable": "Yes", "profile_supported": "No"}, "None", "Single-layer", "MKV", "E-AC-3")
    assert "DV" not in rec and "Remux to MP4" not in rec


def test_recommendation_still_advises_a_remux_for_a_dv_mkv():
    rec = analysis.build_recommendation(
        70, 80, 50, {"playable": "Yes", "profile_supported": "Yes"}, "8", "Single-layer", "MKV", "E-AC-3")
    assert "Remux to MP4 with mp4muxer for safer DV playback" in rec
