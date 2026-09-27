"""Twelve advisory scenarios, at least two per state, plus the engine's guarantees.

Each scenario is a situation a farmer could actually be in on a given date, so the
assertions are about what the advisory tells them to do - not about internals.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.advisory import (
    HAZARDS,
    HORIZONS,
    LANGUAGES,
    advise,
    crops_for,
    derive_stage,
    risk_level,
    zone_window,
)
from src.common import load_config
from src.report import DataError


def probs(**overrides: float) -> dict[str, float]:
    """All hazards quiet unless stated."""
    base = {f"{h}_{k}": 0.05 for h in HAZARDS for k in HORIZONS}
    base.update(overrides)
    return base


def run(state, zone, date, *, onset_happened=None, **overrides):
    return advise(probs(**overrides), state=state, zone=zone,
                  date=dt.date.fromisoformat(date), onset_happened=onset_happened)


# ==========================================================================
# 12 scenarios: 3 x Uttar Pradesh, 3 x Bihar, 3 x Madhya Pradesh, 3 x Maharashtra
# ==========================================================================

# ---- Uttar Pradesh -------------------------------------------------------
def test_up_bundelkhand_late_monsoon_delays_sowing():
    """Jhansi, mid-June, monsoon not coming: do not dry-sow."""
    a = run("Uttar Pradesh", "UP-10", "2026-06-19",
            onset_happened=False, onset_7=0.10, onset_14=0.18)
    assert a.stage == "sowing_window"
    assert a.action == "DELAY_SOWING_10D"
    assert a.confidence == "high"
    assert "groundnut" in a.crops, "groundnut is a Bundelkhand crop and belongs here"


def test_up_eastern_plain_excludes_groundnut():
    """Gorakhpur is UP-8; advising groundnut there would be wrong."""
    a = run("Uttar Pradesh", "UP-8", "2026-06-19",
            onset_happened=False, onset_7=0.85)
    assert a.action == "SOW_NOW"
    assert "groundnut" not in a.crops
    assert "paddy" in a.crops


def test_up_dry_spell_at_flowering_triggers_life_saving_irrigation():
    a = run("Uttar Pradesh", "UP-10", "2026-09-15", onset_happened=True, dry_14=0.74)
    assert a.stage == "flowering"
    assert a.action == "LIFE_SAVING_IRRIGATION_MULCH"
    assert "mulch" in a.render("en").lower()


# ---- Bihar ---------------------------------------------------------------
def test_bihar_heavy_rain_triggers_flood_prep_not_just_drainage():
    """Purnia floods; a red 14-day heavy signal must escalate past drainage."""
    a = run("Bihar", "BI-2", "2026-07-19",
            onset_happened=True, heavy_7=0.71, heavy_14=0.82)
    assert a.action == "FLOOD_PREP"
    assert "flood_prep" in a.matched_rules
    assert "higher ground" in a.render("en")


def test_madhya_pradesh_does_not_get_flood_prep():
    """The flood rule is restricted to flood-prone states; MP gets drainage."""
    a = run("Madhya Pradesh", "MP-10", "2026-07-19",
            onset_happened=True, heavy_7=0.71, heavy_14=0.82)
    assert a.action == "DRAINAGE_POSTPONE_FERTILIZER_SPRAY"
    assert "flood_prep" not in a.matched_rules


def test_bihar_gaya_sowing_window_opens_in_late_may():
    """BI-3's window is 4th week of May to 4th week of June, unlike the MP zones."""
    assert derive_stage(dt.date(2026, 5, 28), "BI-3") == "sowing_window"
    assert derive_stage(dt.date(2026, 5, 28), "MP-10") == "pre_sowing"
    a = run("Bihar", "BI-3", "2026-05-28", onset_happened=False, onset_7=0.80)
    assert a.action == "SOW_NOW"
    assert a.crops == ["paddy", "maize", "arhar"]


# ---- Madhya Pradesh ------------------------------------------------------
def test_mp_onset_imminent_says_sow_now():
    a = run("Madhya Pradesh", "MP-10", "2026-06-19",
            onset_happened=False, onset_7=0.78, onset_14=0.88)
    assert a.action == "SOW_NOW"
    assert "soybean" in a.crops
    assert a.confidence == "high"


def test_mp_quiet_forecast_returns_monitor():
    a = run("Madhya Pradesh", "MP-10/MP-5", "2026-08-20", onset_happened=True)
    assert a.action == "MONITOR"
    assert a.matched_rules == []
    assert all(level == "green" for level in a.risk.values())


def test_mp_missed_window_with_late_monsoon_switches_variety():
    a = run("Madhya Pradesh", "MP-10", "2026-07-25",
            onset_happened=False, onset_28=0.20)
    assert a.stage == "early_vegetative", "past the 7 July window close"
    assert a.action == "SWITCH_SHORT_DURATION"
    assert "short-duration" in a.render("en")


# ---- Maharashtra ---------------------------------------------------------
def test_maharashtra_mid_season_dry_spell_gets_irrigation_and_mulch():
    a = run("Maharashtra", "MH-6/MH-7", "2026-08-08", onset_happened=True, dry_14=0.74)
    assert a.action == "LIFE_SAVING_IRRIGATION_MULCH"
    assert a.confidence == "high"
    assert "cotton" in a.crops


def test_maharashtra_conflicting_hazards_pick_one_operation_and_drop_confidence():
    """Dry 0.72 vs heavy 0.66, both red: act on dry, report heavy as a watch."""
    a = run("Maharashtra", "MH-6/MH-7", "2026-08-08", onset_happened=True,
            dry_14=0.72, heavy_14=0.66, heavy_7=0.64)
    assert a.conflict is not None
    assert a.conflict["primary"] == "dry"
    assert a.action == "LIFE_SAVING_IRRIGATION_MULCH", (
        "the action must follow the conflict resolution, not the priority list"
    )
    assert a.confidence == "low"
    assert "heavy" in a.render("en"), "the losing hazard must still be reported"


def test_maharashtra_exact_tie_goes_to_heavy_rain():
    """On an equal probability the cheaper, reversible operation wins."""
    a = run("Maharashtra", "MH-6/MH-7", "2026-08-08", onset_happened=True,
            dry_14=0.70, heavy_14=0.70, heavy_7=0.70)
    assert a.conflict["primary"] == "heavy"
    assert a.action == "DRAINAGE_POSTPONE_FERTILIZER_SPRAY"
    assert a.confidence == "low"


# ==========================================================================
# Engine guarantees
# ==========================================================================
@pytest.mark.parametrize("p,expected", [
    (0.0, "green"), (0.29, "green"), (0.2999, "green"),
    (0.30, "amber"), (0.45, "amber"), (0.60, "amber"),
    (0.601, "red"), (0.95, "red"), (1.0, "red"),
])
def test_risk_bands_match_the_specification(p, expected):
    assert risk_level(p) == expected


def test_risk_band_boundaries_belong_to_amber():
    """Neither 0.30 nor 0.60 may fall through unclassified."""
    assert risk_level(0.30) == "amber"
    assert risk_level(0.60) == "amber"


def test_risk_level_rejects_an_impossible_probability():
    with pytest.raises(DataError):
        risk_level(1.4)


def test_missing_probabilities_fail_loudly():
    with pytest.raises(DataError, match="missing probabilities"):
        advise({"dry_7": 0.5}, state="Bihar", zone="BI-2",
               date=dt.date(2026, 7, 4))


def test_marathi_never_silently_falls_back_to_english():
    """Marathi is pending re-translation; render must say so, not serve English."""
    a = run("Maharashtra", "MH-6/MH-7", "2026-08-08", onset_happened=True, dry_14=0.74)
    if a.text["mr"] is None:
        with pytest.raises(DataError):
            a.render("mr")
    else:
        assert a.render("mr") != a.render("en")


def test_every_action_has_english_and_hindi():
    actions = load_config("rules")["actions"]
    for code, action in actions.items():
        for language in ("en", "hi"):
            assert action.get(language), f"{code} has no {language} text"
        assert action.get("confidence") in ("high", "medium", "low")
        assert action.get("source"), f"{code} has no source"
        assert action.get("source_note"), f"{code} has no source note"


def test_a_translation_must_keep_the_crops_placeholder():
    """The defect that reverted the first Marathi pass: six texts lost `{crops}`,
    so the advisory would never have named the crop. Any language that has text
    must carry the placeholder wherever English does."""
    actions = load_config("rules")["actions"]
    for code, action in actions.items():
        if "{crops}" not in action["en"]:
            continue
        for language in ("hi", "mr"):
            text = action.get(language)
            if text is None:
                continue          # not translated yet, which is allowed
            assert "{crops}" in text, (
                f"{code}/{language} lost the crops placeholder, so the message "
                "would never name the crop"
            )


def test_every_message_fits_in_one_sms_for_the_longest_crop_list():
    """300 characters is a hard limit; Uttar Pradesh carries nine crops."""
    from src.advisory import crop_phrase

    cfg = load_config("rules")
    longest = max(cfg["crops"].values(), key=len)
    for code, action in cfg["actions"].items():
        for language in ("en", "hi", "mr"):
            body = action.get(language)
            if body is None:
                continue
            body = " ".join(body.split())
            rendered = body.replace("{crops}", crop_phrase(longest, language, cfg))
            assert len(rendered) <= 300, f"{code}/{language} is {len(rendered)} chars"


def test_crop_names_are_localised_not_latin():
    """A Hindi advisory must not say "soybean" in Latin script mid-sentence."""
    from src.advisory import crop_phrase

    cfg = load_config("rules")
    phrase = crop_phrase(["soybean", "cotton"], "hi", cfg)
    assert not any("a" <= ch.lower() <= "z" for ch in phrase), phrase


def test_hindi_text_is_actually_devanagari():
    actions = load_config("rules")["actions"]
    for code, action in actions.items():
        assert any("ऀ" <= ch <= "ॿ" for ch in action["hi"]), (
            f"{code}'s Hindi text contains no Devanagari"
        )


def test_every_rule_points_at_a_defined_action():
    cfg = load_config("rules")
    for rule in cfg["rules"]:
        assert rule["action"] in cfg["actions"], rule["id"]
    for code in cfg["tie_break"]["priority"]:
        assert code in cfg["actions"], code


def test_tie_break_priority_covers_every_action():
    cfg = load_config("rules")
    assert set(cfg["tie_break"]["priority"]) == set(cfg["actions"])


def test_unzoned_district_is_capped_at_low_confidence():
    a = run("Bihar", None, "2026-07-19", onset_happened=True,
            heavy_7=0.71, heavy_14=0.82)
    assert a.zone_used == "DEFAULT"
    assert a.confidence == "low"
    assert any("no sourced NARP zone" in n for n in a.notes)


def test_sowing_rules_stay_silent_when_onset_status_is_unknown():
    """Better to say nothing than to tell someone to re-sow a standing crop."""
    a = run("Madhya Pradesh", "MP-10", "2026-06-19", onset_happened=None,
            onset_7=0.85)
    assert "sow_now" not in a.matched_rules
    assert a.action == "MONITOR"


def test_onset_rules_do_not_fire_once_onset_has_happened():
    a = run("Madhya Pradesh", "MP-10", "2026-07-25", onset_happened=True,
            onset_28=0.20)
    assert "switch_short_duration" not in a.matched_rules


@pytest.mark.parametrize("zone,date,expected", [
    ("MP-10", "2026-05-01", "pre_sowing"),
    ("MP-10", "2026-06-20", "sowing_window"),
    ("MP-10", "2026-07-20", "early_vegetative"),
    ("MP-10", "2026-09-01", "flowering"),
    ("MP-10", "2026-11-01", "maturity"),
])
def test_stage_progression_through_the_season(zone, date, expected):
    assert derive_stage(dt.date.fromisoformat(date), zone) == expected


def test_every_zone_window_carries_a_source():
    cfg = load_config("rules")
    for zone, entry in cfg["sowing_windows"].items():
        assert entry.get("source"), f"{zone} has no source"
        assert entry.get("confidence") in ("high", "medium", "low"), zone
        window = zone_window(zone, cfg=cfg)
        assert window["start"] < window["end"], zone


def test_crops_cover_every_state_in_the_brief():
    for state in ("Uttar Pradesh", "NCT of Delhi", "Bihar", "Madhya Pradesh",
                  "Maharashtra"):
        assert crops_for(state, "UP-10" if "Pradesh" in state else None), state


def test_render_rejects_an_unknown_language():
    a = run("Bihar", "BI-2", "2026-07-19", onset_happened=True, heavy_14=0.82)
    with pytest.raises(DataError):
        a.render("ta")
    assert set(a.text) == set(LANGUAGES)
