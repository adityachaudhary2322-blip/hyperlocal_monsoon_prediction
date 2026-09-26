"""Build the standardised unit layer from GADM level 3.

Run:  python -m src.build_units

Everything downstream reads data/processed/units.gpkg and never GADM directly. That
indirection is what lets block polygons be swapped in later (CLAUDE.md §5): only this
module knows about GADM column names.
"""

from __future__ import annotations

import geopandas as gpd

from src.common import (
    BOUNDARIES_RAW,
    EQUAL_AREA_CRS,
    GEO_CRS,
    UNITS_GPKG,
    load_config,
    pilot_districts,
    regions,
)
from src.report import DataError, print_list, require_nonempty, summarize

UNIT_TYPE = "subdistrict"

# GADM writes unnamed level-3 polygons as "n.a. ( 1681 )". They are real polygons
# (mostly tiny coastal islands) but the string must never reach an advisory.
PLACEHOLDER_PREFIX = "n.a."

SCHEMA = ["unit_id", "unit_name", "district", "state", "unit_type",
          "name_is_placeholder", "area_km2", "geometry"]


def load_gadm_level3() -> gpd.GeoDataFrame:
    path = BOUNDARIES_RAW / load_config("project")["boundaries"]["subdistrict"].split("/")[-1]
    if not path.is_file():
        raise DataError(f"missing boundary file: {path}")
    gdf = gpd.read_file(path)
    print(f"  read {len(gdf):,} level-3 features from {path.name}")
    if gdf.crs is None:
        raise DataError(f"{path.name} has no CRS")
    return gdf.to_crs(GEO_CRS)


def filter_regions(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Keep the 5 target regions, printing the exact NAME_1 values found.

    Never guesses a spelling: a requested region absent from the file is an error,
    not something to fuzzy-match past (CLAUDE.md §5).
    """
    found = sorted(gdf["NAME_1"].unique())
    wanted = regions()
    print(f"  distinct NAME_1 in file: {len(found)}")
    missing = [r for r in wanted if r not in found]
    if missing:
        raise DataError(
            f"requested regions absent from the shapefile: {missing}. "
            f"Do not guess spellings - the file contains: {found}"
        )
    print("  matched NAME_1 values:")
    for region in wanted:
        print(f"      - {region!r}")
    return gdf[gdf["NAME_1"].isin(wanted)].copy()


def standardise(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Map GADM columns onto the project schema and compute derived columns."""
    if gdf["GID_3"].duplicated().any():
        dupes = gdf.loc[gdf["GID_3"].duplicated(), "GID_3"].tolist()
        raise DataError(f"GID_3 is not unique, cannot serve as unit_id: {dupes[:5]}")

    units = gpd.GeoDataFrame(
        {
            "unit_id": gdf["GID_3"].astype(str),
            "unit_name": gdf["NAME_3"].astype(str),
            "district": gdf["NAME_2"].astype(str),
            "state": gdf["NAME_1"].astype(str),
            "unit_type": UNIT_TYPE,
        },
        geometry=gdf.geometry.values,
        crs=GEO_CRS,
    )

    units["name_is_placeholder"] = units["unit_name"].str.startswith(PLACEHOLDER_PREFIX)
    # Area in an equal-area CRS; degrees would understate area away from the equator.
    units["area_km2"] = units.to_crs(EQUAL_AREA_CRS).area.to_numpy() / 1e6

    invalid = ~units.geometry.is_valid
    if invalid.any():
        print(f"  repairing {int(invalid.sum())} invalid geometries with buffer(0)")
        units.loc[invalid, "geometry"] = units.loc[invalid].geometry.buffer(0)

    return units[SCHEMA].sort_values(["state", "district", "unit_name"]).reset_index(drop=True)


def report_counts(units: gpd.GeoDataFrame) -> None:
    print("\n--- units per state ---")
    per_state = units.groupby("state").size().sort_values(ascending=False)
    for state, count in per_state.items():
        print(f"  {state:<16}: {count:>4}")
    print(f"  {'TOTAL':<16}: {len(units):>4}")

    print("\n--- units per pilot district ---")
    total = 0
    for state, districts in pilot_districts().items():
        for district in districts:
            subset = units[(units["state"] == state) & (units["district"] == district)]
            if subset.empty:
                raise DataError(
                    f"pilot district {state}/{district} matched no units - check the "
                    f"verified gadm_name_2 table in config/project.yaml"
                )
            total += len(subset)
            names = ", ".join(sorted(subset["unit_name"]))
            print(f"  {state:<16} {district:<10}: {len(subset):>2}  [{names}]")
    print(f"  {'TOTAL pilots':<27}: {total:>2}")

    placeholders = units[units["name_is_placeholder"]]
    print()
    print_list(
        "units with placeholder names (never show these to a user)",
        [f"{r.state}/{r.district}/{r.unit_name} ({r.area_km2:.2f} km2)"
         for r in placeholders.itertuples()],
        limit=8,
    )


def main() -> int:
    print("--- build_units ---")
    gdf = load_gadm_level3()
    gdf = filter_regions(gdf)
    units = standardise(gdf)
    require_nonempty(units, "standardised units")
    report_counts(units)

    UNITS_GPKG.parent.mkdir(parents=True, exist_ok=True)
    units.to_file(UNITS_GPKG, layer="units", driver="GPKG")
    require_nonempty(UNITS_GPKG, "units.gpkg")

    summarize(
        "build_units",
        rows=len(units),
        files=[UNITS_GPKG],
        extra={
            "unit_type": UNIT_TYPE,
            "crs": str(units.crs),
            "states": units["state"].nunique(),
            # (state, district) pairs, not bare names: one district name is shared
            # across two of the 5 states, so nunique() on district alone undercounts.
            "districts": len(units.groupby(["state", "district"])),
            "placeholder names": int(units["name_is_placeholder"].sum()),
            "smallest unit": f"{units['area_km2'].min():.3f} km2",
            "median unit": f"{units['area_km2'].median():.1f} km2",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
