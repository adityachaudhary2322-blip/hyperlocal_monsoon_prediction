"""Stage 1 (national data): tiers, region-aware onset, withdrawal, crop calendar, crops.

Data-dependent checks skip cleanly when the (gitignored) national files are absent, so
the suite still runs on a fresh clone and on the website's deploy check.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import yaml

from src.common import CONFIG_DIR, UNIT_RAIN_INDIA_PARQUET, UNITS_INDIA_GPKG


# ==========================================================================
# Tiers
# ==========================================================================
def test_tiers_follow_the_brief():
    from src.tiers import base_tier, seasonal_low, tier_on

    assert base_tier("Uttar Pradesh") == "validated"
    assert base_tier("NCT of Delhi") == "validated"
    assert base_tier("Rajasthan") == "experimental"
    assert base_tier("Mizoram") == "low"
    assert base_tier("Himachal Pradesh", 3200) == "low"
    assert base_tier("Himachal Pradesh", 900) == "experimental"
    assert base_tier("Rajasthan", 3200) == "experimental"   # altitude rule is state-scoped
    assert seasonal_low("Tamil Nadu", "Chennai")
    assert seasonal_low("Andhra Pradesh", "Krishna") and not seasonal_low("Andhra Pradesh", "Kurnool")
    assert tier_on("experimental", True, dt.date(2026, 11, 2)) == "low"
    assert tier_on("experimental", True, dt.date(2026, 7, 2)) == "experimental"


def test_tier_config_uses_real_gadm_state_names():
    import geopandas as gpd

    from src.common import BOUNDARIES_RAW

    shp = BOUNDARIES_RAW / "gadm41_IND_3.shp"
    if not shp.is_file():
        pytest.skip("GADM not present")
    names = set(gpd.read_file(shp, ignore_geometry=True, columns=["NAME_1"])["NAME_1"])
    cfg = yaml.safe_load((CONFIG_DIR / "tiers.yaml").read_text(encoding="utf-8"))
    used = set(cfg["validated"]) | set(cfg["low"]["states"]) | \
        set(cfg["low"]["high_altitude"]["states"]) | set(cfg["seasonal_low"]["states"]) | \
        set(cfg["seasonal_low"]["districts"])
    assert used <= names, f"not GADM spellings: {used - names}"


@pytest.mark.skipif(not UNITS_INDIA_GPKG.is_file(), reason="national units not built")
def test_national_units_cover_every_state_with_a_tier():
    import geopandas as gpd

    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units", ignore_geometry=True)
    assert len(units) == 2347 and units["state"].nunique() == 36
    assert set(units["tier"]) <= {"validated", "experimental", "low"}
    assert (units.loc[units["tier"] == "validated", "state"].nunique()) == 5
    assert units["unit_id"].is_unique


# ==========================================================================
# Onset regions + withdrawal
# ==========================================================================
def test_onset_resolution_order_zone_beats_region_beats_default():
    from src.onset import onset_config

    default = onset_config()
    arid = onset_config(region="Western Dry Region")
    assert arid.accum_mm < default.accum_mm and arid.dry_spell_days > default.dry_spell_days
    assert onset_config(region="Not A Region") == default


def test_region_names_in_config_are_the_fifteen():
    from src.build_zones_national import REGIONS

    for name in ("onset", "withdrawal"):
        cfg = yaml.safe_load((CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        assert set(cfg.get("regions") or {}) <= REGIONS, name
    assert len(REGIONS) == 15


def _season(wet_from: str, wet_to: str, year: int = 2024):
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31")
    rain = np.zeros(len(days))
    rain[(days >= wet_from) & (days <= wet_to)] = 8.0
    return rain, days


def test_withdrawal_after_the_rains_stop():
    from src.withdrawal import detect_withdrawal, withdrawal_config

    rain, days = _season("2024-06-20", "2024-09-25")
    assert detect_withdrawal(rain, days, withdrawal_config()) == pd.Timestamp("2024-09-26")


def test_a_september_break_that_recovers_is_not_withdrawal():
    from src.withdrawal import detect_withdrawal, withdrawal_config

    rain, days = _season("2024-06-20", "2024-10-10")
    rain[(days >= "2024-09-05") & (days <= "2024-09-11")] = 0.0      # 7-day break
    got = detect_withdrawal(rain, days, withdrawal_config())
    assert got == pd.Timestamp("2024-10-11")


def test_withdrawal_needs_a_confirmable_record():
    from src.withdrawal import detect_withdrawal, withdrawal_config

    rain, days = _season("2024-06-20", "2024-12-31")
    cut = days <= "2024-12-31"
    assert detect_withdrawal(rain[cut], days[cut], withdrawal_config()) is None


# ==========================================================================
# Crop calendar
# ==========================================================================
@pytest.mark.parametrize("text,expected", [
    ("Jun(B)-Jul(M)", ("06-01", "07-20")),
    ("May-June", ("05-01", "06-30")),
    ("Nov (L)- Dec (E)", ("11-21", "12-31")),
    ("Feb(M)Apr(E)", ("02-11", "04-30")),
    ("Sept(E)- October(B)", ("09-21", "10-10")),
    ("Jun(M) �Jul(E)", ("06-11", "07-31")),
    ("nan", None),
])
def test_des_windows_parse(text, expected):
    from src.build_crop_calendar import parse_window

    assert parse_window(text if text != "nan" else float("nan")) == expected


def test_stage_fractions_sum_to_one():
    cfg = yaml.safe_load((CONFIG_DIR / "crop_stages.yaml").read_text(encoding="utf-8"))
    for crop, f in cfg["fractions"].items():
        assert sum(f.values()) == pytest.approx(1.0), crop


def test_derived_stages_run_from_early_to_late_fields():
    from src.build_crop_calendar import derive_stages

    s = derive_stages(("06-11", "07-20"), ("09-21", "10-10"),
                      {"vegetative": 0.45, "flowering": 0.25, "maturity": 0.30})
    assert s["vegetative"]["window"][0] == "06-11"
    assert s["maturity"]["window"][1] == "10-10"
    assert all(v["basis"] == "derived" for v in s.values())


def test_calendar_keeps_sources_and_verbatim_text():
    path = CONFIG_DIR / "crop_calendar.yaml"
    if not path.is_file():
        pytest.skip("crop calendar not built")
    cal = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "desagri.gov.in" in cal["source"]["url"]
    soy = cal["crops"]["soybean"]["kharif"]["Madhya Pradesh"]
    assert soy["sowing"]["basis"] == "sourced" and soy["sowing"]["verbatim"]
    assert cal["crops"]["jowar"]["kharif"]["All India"]["sowing"]["basis"] == "default"


# ==========================================================================
# Crops (logic only; the DES CSV is downloaded by hand)
# ==========================================================================
def test_main_crops_stop_at_80_percent():
    from src.build_crops import main_crops

    area = pd.Series({"soybean": 50.0, "cotton": 25.0, "tur": 15.0, "maize": 10.0})
    got = [c["crop"] for c in main_crops(area)]
    assert got == ["soybean", "cotton", "tur"]      # 50 + 25 = 75 < 80, so tur too


def test_seasons_group_into_kharif_and_rabi():
    from src.build_crops import season_group

    assert season_group("kharif") == season_group("autumn") == "kharif"
    assert season_group("rabi") == season_group("winter") == "rabi"
    assert season_group("whole year") is None


@pytest.mark.skipif(not UNIT_RAIN_INDIA_PARQUET.is_dir(), reason="national rain not built")
def test_national_unit_rain_has_every_year_and_unit():
    import pyarrow.dataset as ds

    parts = sorted(p.name for p in UNIT_RAIN_INDIA_PARQUET.glob("year=*"))
    assert parts[0] == "year=1990" and len(parts) >= 36
    table = ds.dataset(UNIT_RAIN_INDIA_PARQUET / "year=2024").to_table()
    assert table.num_rows == 366 * 2347
    assert str(table.schema.field("rain_mm").type) == "float"
