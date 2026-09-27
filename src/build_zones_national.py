"""Agro-climatic region for every district in India -> config/zones_national.csv.

Run:  python -m src.build_zones_national

Source (you download it; data.gov.in puts a captcha in front of the file):
  "Boundaries of Agro-climatic regions", Ministry of Jal Shakti (2022), Open Government
  Data Platform India - https://www.data.gov.in/resource/boundaries-agro-climatic-regions
  Put the shapefile / GeoJSON / GeoPackage in data/raw/zones/.

Each GADM district gets the region covering the largest share of its area (computed in
EPSG:6933, §10). The share is written next to it, so a district split between regions is
visible instead of silently assigned. Districts with a verified NARP zone in
config/zones_narp.csv keep that zone as `zone_id`; the region is added for every district.

The Esri India copy of this layer is NOT used: its terms forbid exporting the data.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

from src.common import CONFIG_DIR, DATA_RAW, EQUAL_AREA_CRS, UNITS_INDIA_GPKG
from src.report import DataError, summarize

ZONES_DIR = DATA_RAW / "zones"
OUT = CONFIG_DIR / "zones_national.csv"
SOURCE_URL = "https://www.data.gov.in/resource/boundaries-agro-climatic-regions"
# The 15 Planning Commission regions, as named in the Ministry of Jal Shakti layer
# (attribute names read from the published service, 2026-09-27).
REGIONS = {
    "Western Himalayan Region", "Eastern Himalayan Region", "Lower Gangetic Plain Region",
    "Middle Gangetic Plains Region", "Upper Gangetic Plain Region", "Trans-Ganga Plains Region",
    "Eastern Plateau and Hills", "Central Plateau and Hills", "Western Plateau and Hills",
    "Southern Plateau and Hills", "Eastern Coastal Plains and Hills",
    "Western Coastal Plains and Ghats", "Gujarat Plains and Hills", "Western Dry Region",
    "Island Region",
}
NAME_FIELDS = ("acr_name", "ACR_NAME", "Acr_Name", "region", "Region", "NAME", "name")


def load_regions() -> gpd.GeoDataFrame:
    files = [p for p in ZONES_DIR.glob("*") if p.suffix.lower() in (".shp", ".geojson",
                                                                     ".json", ".gpkg")]
    if not files:
        raise DataError(f"no agro-climatic region file in {ZONES_DIR}. Download "
                        f"'Boundaries of Agro-climatic regions' from {SOURCE_URL}")
    frame = gpd.read_file(files[0])
    field = next((f for f in NAME_FIELDS if f in frame.columns), None)
    if field is None:
        raise DataError(f"{files[0].name}: no region-name column among {list(frame.columns)}")
    found = sorted(frame[field].astype(str).str.strip().unique())
    print(f"  {files[0].name}: {len(frame)} features, names in '{field}':")
    for name in found:
        print(f"    {name!r}{'' if name in REGIONS else '   <- not one of the 15 known names'}")
    frame = frame.rename(columns={field: "region"})
    frame["region"] = frame["region"].astype(str).str.strip()
    if frame.crs is None:
        raise DataError(f"{files[0].name} has no CRS")
    return frame[["region", "geometry"]]


def main() -> int:
    print("--- build_zones_national ---")
    regions = load_regions().to_crs(EQUAL_AREA_CRS)
    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units")
    districts = units.dissolve(by=["state", "district"], as_index=False)[
        ["state", "district", "geometry"]].to_crs(EQUAL_AREA_CRS)
    districts["area"] = districts.area
    inter = gpd.overlay(districts, regions, how="intersection", keep_geom_type=True)
    inter["share"] = inter.area / inter["area"]
    best = (inter.sort_values("share", ascending=False)
            .drop_duplicates(["state", "district"])[["state", "district", "region", "share"]])
    table = districts[["state", "district"]].merge(best, how="left", on=["state", "district"])

    narp = pd.read_csv(CONFIG_DIR / "zones_narp.csv")
    narp_cols = [c for c in ("state", "district", "zone_id") if c in narp.columns]
    table = table.merge(narp[narp_cols], how="left", on=["state", "district"])
    table["region_share"] = table.pop("share").round(3)
    table["source"] = SOURCE_URL
    missing = table[table["region"].isna()]
    split = table[table["region_share"] < 0.6]
    table.to_csv(OUT, index=False)

    print(f"  districts: {len(table)}, assigned: {table['region'].notna().sum()}, "
          f"with a verified NARP zone: {table['zone_id'].notna().sum()}")
    print("  districts per region:")
    for region, n in table["region"].value_counts().items():
        print(f"    {region:<36} {n}")
    if len(split):
        print(f"  split districts (largest region < 60% of area): {len(split)}")
    if len(missing):
        print(f"  NOT assigned: {missing[['state', 'district']].values.tolist()[:10]}")
    summarize("build_zones_national", rows=len(table), files=[OUT],
              extra={"unassigned": len(missing), "split": len(split)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
