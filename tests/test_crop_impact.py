"""Crop impact layer: 15 scenarios across crops, stages and threats, plus the safety rules
on config/crop_vulnerability.yaml (no pesticide names or doses; every entry sourced)."""

from __future__ import annotations

import datetime as dt
import re

import pytest
import yaml

from src.common import CONFIG_DIR
from src.crop_impact import active_stages, assess

D = dt.date
QUIET = {"rain_7d": 5.0, "rain_14d": 40.0, "normal_14d": 45.0, "rh_next_7d": 70.0,
         "tmax_next_7d": 31.0, "soil_moisture": 0.3}


def one(state, crop, season, day, weather=None, probs=None, tier="validated",
        season_name="monsoon"):
    out = assess(state, tier, day, [(crop, season)], {**QUIET, **(weather or {})},
                 probs or {}, season_name)
    assert len(out) == 1
    return out[0]


# 1
def test_soybean_at_harvest_with_heavy_rain_is_high_with_numbers():
    i = one("Madhya Pradesh", "soybean", "kharif", D(2026, 10, 1),
            {"rain_7d": 42.0}, {("heavy", 7): 0.65})
    assert (i.level, i.threat) == ("high", "harvest_rain")
    assert "42 mm" in i.reason and "65%" in i.reason and "pod damage" in i.reason


# 2
def test_soybean_dry_spell_at_flowering_links_life_saving_irrigation():
    i = one("Madhya Pradesh", "soybean", "kharif", D(2026, 8, 5), probs={("dry", 14): 0.7})
    assert (i.level, i.threat, i.action) == ("high", "dry_spell", "LIFE_SAVING_IRRIGATION_MULCH")


# 3
def test_waterlogging_needs_both_signals_for_high():
    obs_only = one("Madhya Pradesh", "soybean", "kharif", D(2026, 7, 1), {"rain_7d": 130.0})
    both = one("Madhya Pradesh", "soybean", "kharif", D(2026, 7, 1), {"rain_7d": 130.0},
               {("heavy", 7): 0.4})
    assert obs_only.level == "medium" and both.level == "high"
    assert both.threat == "waterlogging" and both.action == "DRAINAGE_POSTPONE_FERTILIZER_SPRAY"


# 4
def test_paddy_heat_at_flowering_scales_with_temperature():
    warm = one("Uttar Pradesh", "paddy", "kharif", D(2026, 9, 1), {"tmax_next_7d": 36.0})
    hot = one("Uttar Pradesh", "paddy", "kharif", D(2026, 9, 1), {"tmax_next_7d": 39.0})
    assert (warm.level, warm.threat) == ("medium", "heat_stress")
    assert hot.level == "high"


# 5
def test_humidity_names_the_disease_risk_only():
    i = one("Uttar Pradesh", "paddy", "kharif", D(2026, 8, 20), {"rh_next_7d": 90.0})
    assert i.threat == "humidity_disease" and "blast" in i.reason
    assert "agriculture office" in i.reason


# 6
def test_cotton_boll_rot_uses_late_heavy_after_september():
    i = one("Maharashtra", "cotton", "kharif", D(2026, 10, 15), probs={("late_heavy", 7): 0.7},
            season_name="post_monsoon")
    assert (i.level, i.threat) == ("high", "harvest_rain") and "boll rot" in i.reason


# 7
def test_quiet_weather_reports_low_with_a_sentence():
    i = one("Madhya Pradesh", "soybean", "kharif", D(2026, 8, 5))
    assert i.level == "low" and i.reason.startswith("No weather threat to Soybean")


# 8
def test_experimental_states_carry_the_general_guidance_note():
    i = one("Rajasthan", "bajra", "kharif", D(2026, 8, 10), probs={("dry", 14): 0.7},
            tier="experimental")
    assert "General guidance, not yet validated for this state." in i.notes


# 9
def test_medium_severity_caps_the_level():
    i = one("Maharashtra", "cotton", "kharif", D(2026, 8, 10), probs={("dry", 14): 0.95})
    assert i.threat == "dry_spell" and i.level == "medium"


# 10
def test_wheat_terminal_heat():
    mild = one("Madhya Pradesh", "wheat", "rabi", D(2027, 2, 20), {"tmax_next_7d": 33.0})
    severe = one("Madhya Pradesh", "wheat", "rabi", D(2027, 2, 20), {"tmax_next_7d": 36.0})
    assert (mild.threat, mild.level) == ("heat_stress", "medium") and severe.level == "high"


# 11
def test_gram_rabi_sowing_moisture_from_probability():
    i = one("Madhya Pradesh", "gram", "rabi", D(2026, 10, 20),
            probs={("rabi_moisture", 28): 0.7}, season_name="post_monsoon")
    assert (i.threat, i.level) == ("rabi_moisture", "high") and "70%" in i.reason


# 12
def test_mustard_dry_topsoil_at_sowing_is_medium():
    i = one("Rajasthan", "mustard", "rabi", D(2026, 10, 10), {"soil_moisture": 0.1},
            tier="experimental", season_name="post_monsoon")
    assert (i.threat, i.level) == ("rabi_moisture", "medium")


# 13
def test_observed_dry_fortnight_raises_a_low_forecast_to_medium():
    i = one("Madhya Pradesh", "soybean", "kharif", D(2026, 8, 5),
            {"rain_14d": 5.0, "normal_14d": 60.0}, {("dry", 14): 0.1})
    assert (i.threat, i.level) == ("dry_spell", "medium")
    assert "only 5 mm fell in the last 2 weeks against a normal of 60 mm" in i.reason


# 14
def test_crops_outside_the_table_are_skipped_and_stage_basis_reported():
    out = assess("Madhya Pradesh", "validated", D(2026, 8, 5),
                 [("vegetables", "kharif"), ("soybean", "kharif")], QUIET, {}, "monsoon")
    assert [i.crop for i in out] == ["soybean"]
    stages, basis = active_stages("soybean", "kharif", "Madhya Pradesh", D(2026, 8, 5))
    assert stages == ["vegetative", "flowering"] and basis == "derived"


# 15
def test_groundnut_pod_sprouting_at_harvest_in_hindi():
    out = assess("Gujarat", "experimental", D(2026, 10, 5), [("groundnut", "kharif")],
                 {**QUIET, "rain_7d": 60.0}, {("heavy", 7): 0.35}, "monsoon", lang="hi")
    assert out[0].crop_name == "मूंगफली" and out[0].level == "high"


# --------------------------------------------------------------------------
# safety rules on the table itself
# --------------------------------------------------------------------------
BANNED = r"\b(mancozeb|carbendazim|imidacloprid|chlorpyrifos|propiconazole|hexaconazole|" \
         r"tricyclazole|copper oxychloride|carbofuran|metalaxyl|thiram|spray (?:of|with)|" \
         r"\d+\s*(?:ml|g|kg|l)\s*(?:/|per)\s*(?:l|litre|ha|acre)|dose|dosage)\b"


def test_vulnerability_table_names_no_pesticide_or_dose():
    """Scans every value a user could see (the file's own comments state the rule)."""
    table = yaml.safe_load((CONFIG_DIR / "crop_vulnerability.yaml").read_text(encoding="utf-8"))

    def strings(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)
        elif isinstance(node, str):
            yield node

    text = "\n".join(strings(table)).lower()
    assert not re.search(BANNED, text), re.search(BANNED, text)
    assert "mancozeb" in "x mancozeb x" and re.search(BANNED, "spray of mancozeb 2 g/l")


def test_every_crop_threat_cites_a_known_source():
    table = yaml.safe_load((CONFIG_DIR / "crop_vulnerability.yaml").read_text(encoding="utf-8"))
    for crop, spec in table["crops"].items():
        for threat, entry in spec.items():
            if threat == "name":
                continue
            assert threat in table["threats"], f"{crop}: unknown threat {threat}"
            assert entry.get("source") in table["sources"], f"{crop}/{threat}: no source"
    assert table["meta"]["review"] == "pending"
