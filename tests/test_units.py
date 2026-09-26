"""The unit layer is the contract every later phase reads, so guard its shape."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest

from src.common import CONFIG_DIR, UNITS_GPKG, pilot_districts, regions

pytestmark = pytest.mark.skipif(
    not UNITS_GPKG.is_file(), reason="units.gpkg not built yet"
)

EXPECTED_PER_STATE = {
    "Uttar Pradesh": 214,
    "Madhya Pradesh": 166,
    "Maharashtra": 305,
    "Bihar": 53,
    "NCT of Delhi": 1,
}
EXPECTED_PILOT_UNITS = {
    ("Uttar Pradesh", "Jhansi"): 4,
    ("Uttar Pradesh", "Gorakhpur"): 2,
    ("Bihar", "Gaya"): 1,
    ("Bihar", "Purnia"): 1,
    ("Madhya Pradesh", "Sehore"): 5,
    ("Madhya Pradesh", "Indore"): 4,
    ("Maharashtra", "Latur"): 5,
    ("Maharashtra", "Bid"): 8,
}


@pytest.fixture(scope="module")
def units():
    return gpd.read_file(UNITS_GPKG, layer="units")


def test_schema_is_the_standardised_one(units):
    required = {"unit_id", "unit_name", "district", "state", "unit_type", "geometry"}
    assert required <= set(units.columns)
    assert units["unit_type"].eq("subdistrict").all()
    assert units.crs.to_string() == "EPSG:4326"


def test_unit_id_is_unique_and_non_null(units):
    assert units["unit_id"].notna().all()
    assert not units["unit_id"].duplicated().any()


def test_geometries_are_valid_and_non_empty(units):
    assert units.geometry.is_valid.all()
    assert not units.geometry.is_empty.any()


def test_only_the_five_target_regions_are_present(units):
    assert set(units["state"]) == set(regions())


def test_unit_counts_per_state(units):
    counts = units.groupby("state").size().to_dict()
    assert counts == EXPECTED_PER_STATE


def test_pilot_districts_resolve_to_the_expected_units(units):
    for (state, district), expected in EXPECTED_PILOT_UNITS.items():
        found = len(units[(units["state"] == state) & (units["district"] == district)])
        assert found == expected, f"{state}/{district}: {found} units, want {expected}"


def test_beed_is_present_under_its_gadm_spelling(units):
    """GADM writes Beed as "Bid"; fuzzy matching sends "Beed" to Nanded."""
    maharashtra = units[units["state"] == "Maharashtra"]["district"].unique()
    assert "Bid" in maharashtra
    assert "Beed" not in maharashtra
    assert pilot_districts()["Maharashtra"] == ["Latur", "Bid"]


def test_placeholder_names_are_flagged_rather_than_dropped(units):
    flagged = units[units["name_is_placeholder"]]
    assert len(flagged) == 25
    assert flagged["unit_name"].str.startswith("n.a.").all()
    # They are real polygons and must keep an area.
    assert (flagged["area_km2"] > 0).all()


def test_areas_are_positive_and_in_equal_area_units(units):
    assert (units["area_km2"] > 0).all()
    # Sanity: total area of the 5 regions is about 1.05 million km2.
    total = units["area_km2"].sum()
    assert 0.9e6 < total < 1.2e6, f"total area {total:,.0f} km2 looks wrong"


def test_zone_column_is_present_and_pilots_are_all_zoned(units):
    assert "zone_id" in units.columns
    for state, districts in pilot_districts().items():
        for district in districts:
            subset = units[(units["state"] == state) & (units["district"] == district)]
            assert subset["zone_id"].notna().all(), (
                f"pilot {state}/{district} has no zone; Phase 2 onset rules depend on it"
            )


def test_every_zone_row_cites_a_source():
    table = pd.read_csv(CONFIG_DIR / "zones_narp.csv", dtype=str).fillna("")
    assert not table.empty
    assert not table.duplicated(["state", "district"]).any()
    # Delhi is a documented project decision with no external source; every other
    # row must carry a URL and the verbatim wording used to verify it.
    external = table[table["zone_id"] != "NCT"]
    assert external["source_url"].str.startswith("http").all()
    assert external["zone_name_in_source"].str.len().gt(0).all()
