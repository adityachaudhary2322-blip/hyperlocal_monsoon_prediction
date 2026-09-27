"""Main crops per district from DES district-wise, season-wise crop statistics.

Run:  python -m src.build_crops [--years 5]

Source (you download it; data.gov.in puts a captcha in front of the file):
  "District-wise, season-wise crop production statistics from 1997", Directorate of
  Economics & Statistics, Ministry of Agriculture and Farmers Welfare, Open Government
  Data Platform India -
  https://www.data.gov.in/resource/district-wise-season-wise-crop-production-statistics-1997
  Put the CSV in data/raw/crops/.

Output data/processed/district_crops.parquet: per GADM district and season (kharif,
rabi), the main crops = the largest crops by average area over the latest `--years`
years, taken in order until they cover >= 80% of that season's area. Sub-districts
inherit their district's crops at use time.

District names are matched to GADM NAME_2 within the same state with rapidfuzz, plus
explicit overrides in config/crop_district_overrides.yaml. A match below 90 is never
accepted silently (CLAUDE.md §5: "Bid" fuzzy-matches "Nanded" at 60): unmatched
districts get the state's main crops with crop_source = "state default", and are listed.
"""

from __future__ import annotations

import argparse
import re

import geopandas as gpd
import pandas as pd
from rapidfuzz import fuzz, process

from src.common import CONFIG_DIR, DATA_PROCESSED, DATA_RAW, UNITS_INDIA_GPKG, load_config
from src.report import DataError, print_list, summarize

CROPS_DIR = DATA_RAW / "crops"
OUT = DATA_PROCESSED / "district_crops.parquet"
COVERAGE = 0.80
MATCH_FLOOR = 90

# DES crop names -> the keys used by crop_calendar.yaml / crop_vulnerability.yaml.
# Crops outside this map are kept under their DES name (they still count toward 80%).
CROP_KEYS = {
    "rice": "paddy", "paddy": "paddy", "wheat": "wheat", "maize": "maize",
    "bajra": "bajra", "jowar": "jowar", "arhar/tur": "tur", "arhar": "tur", "tur": "tur",
    "moong(green gram)": "moong", "moong": "moong", "urad": "urad",
    "soyabean": "soybean", "soybean": "soybean", "cotton(lint)": "cotton", "cotton": "cotton",
    "groundnut": "groundnut", "sugarcane": "sugarcane", "gram": "gram",
    "rapeseed &mustard": "mustard", "rapeseed & mustard": "mustard", "mustard": "mustard",
}
KHARIF = {"kharif", "autumn"}
RABI = {"rabi", "winter"}
COLUMN_CANDIDATES = {
    "state": ("State_Name", "State", "state_name", "state"),
    "district": ("District_Name", "District", "district_name", "district"),
    "year": ("Crop_Year", "Year", "crop_year", "year"),
    "season": ("Season", "season"),
    "crop": ("Crop", "crop"),
    "area": ("Area", "area", "Area (Hectare)", "Area_Hectare"),
}
STATE_FIX = {"orissa": "Odisha", "chhattisgarh": "Chhattisgarh", "uttaranchal": "Uttarakhand",
             "jammu and kashmir ": "Jammu and Kashmir", "andaman and nicobar islands":
             "Andaman and Nicobar", "delhi": "NCT of Delhi", "telangana ": "Telangana"}


def _pick(frame: pd.DataFrame, key: str) -> str:
    for name in COLUMN_CANDIDATES[key]:
        if name in frame.columns:
            return name
    raise DataError(f"no {key} column among {list(frame.columns)}")


def load_statistics() -> pd.DataFrame:
    files = sorted(CROPS_DIR.glob("*.csv"))
    if not files:
        raise DataError(f"no crop statistics CSV in {CROPS_DIR}. Download 'District-wise, "
                        "season-wise crop production statistics from 1997' from data.gov.in")
    raw = pd.read_csv(files[0], low_memory=False)
    cols = {k: _pick(raw, k) for k in COLUMN_CANDIDATES}
    frame = raw[list(cols.values())].rename(columns={v: k for k, v in cols.items()})
    frame["season"] = frame["season"].astype(str).str.strip().str.lower()
    frame["crop"] = frame["crop"].astype(str).str.strip()
    frame["year"] = pd.to_numeric(frame["year"].astype(str).str[:4], errors="coerce")
    frame["area"] = pd.to_numeric(frame["area"], errors="coerce")
    frame = frame.dropna(subset=["year", "area"])
    print(f"  {files[0].name}: {len(frame):,} rows, years {int(frame['year'].min())}-"
          f"{int(frame['year'].max())}, {frame['crop'].nunique()} crops, "
          f"seasons {sorted(frame['season'].unique())}")
    return frame


def season_group(season: str) -> str | None:
    return "kharif" if season in KHARIF else "rabi" if season in RABI else None


def main_crops(area: pd.Series) -> list[dict]:
    area = area.sort_values(ascending=False)
    total, out, covered = area.sum(), [], 0.0
    for crop, value in area.items():
        if covered >= COVERAGE or total <= 0:
            break
        covered += value / total
        out.append({"crop": crop, "share": round(float(value / total), 3)})
    return out


def calendar_defaults() -> pd.DataFrame:
    """Fallback when no district statistics exist: every GADM district gets the crops the
    DES crop calendar lists for its state (or All India), with no area shares.

    This says which crops are grown in the state, not which dominate a district, so it is
    labelled crop_source = "state default (DES crop calendar)" and shown that way.
    """
    import yaml

    cal = yaml.safe_load((CONFIG_DIR / "crop_calendar.yaml").read_text(encoding="utf-8"))
    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units", ignore_geometry=True)
    rows = []
    for (state, district) in units[["state", "district"]].drop_duplicates().itertuples(index=False):
        for season in ("kharif", "rabi"):
            crops = sorted(crop for crop, seasons in cal["crops"].items()
                           if season in seasons and state in seasons[season])
            source = "state default (DES crop calendar)"
            if not crops:
                crops = sorted(crop for crop, seasons in cal["crops"].items()
                               if season in seasons and "All India" in seasons[season])
                source = "national default (DES crop calendar)"
            rows += [{"state": state, "district": district, "season": season, "rank": None,
                      "crop": crop, "share": None, "crop_source": source, "years": None}
                     for crop in crops]
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--from-calendar", action="store_true",
                        help="no DES statistics yet: state crops from the DES crop calendar")
    args = parser.parse_args()
    print("--- build_crops ---")
    if args.from_calendar:
        table = calendar_defaults()
        if table.empty:
            raise DataError("no crops from the calendar")
        table.to_parquet(OUT, index=False)
        print("  crop_source:", table.drop_duplicates(["state", "district", "crop_source"])
              ["crop_source"].value_counts().to_dict())
        summarize("build_crops --from-calendar", rows=len(table), files=[OUT],
                  extra={"districts": len(table[["state", "district"]].drop_duplicates()),
                         "note": "state-level crop lists; district statistics pending"})
        return 0
    stats = load_statistics()
    last = int(stats["year"].max())
    stats = stats[stats["year"] > last - args.years]
    stats["season"] = stats["season"].map(season_group)
    stats = stats.dropna(subset=["season"])
    stats["crop"] = stats["crop"].map(lambda c: CROP_KEYS.get(c.lower(), c))

    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units", ignore_geometry=True)
    gadm = units[["state", "district"]].drop_duplicates()
    overrides = (load_config("crop_district_overrides") or {}).get("districts", {}) \
        if (CONFIG_DIR / "crop_district_overrides.yaml").is_file() else {}
    gadm_states = sorted(gadm["state"].unique())

    def fix_state(name: str) -> str | None:
        name = re.sub(r"\s+", " ", str(name)).strip()
        name = STATE_FIX.get(name.lower() + " ", STATE_FIX.get(name.lower(), name))
        hit = process.extractOne(name, gadm_states, scorer=fuzz.WRatio)
        return hit[0] if hit and hit[1] >= MATCH_FLOOR else None

    stats["gstate"] = stats["state"].map(fix_state)
    mapping, weak = {}, []
    for (state, district) in stats[["gstate", "district"]].drop_duplicates().itertuples(index=False):
        if state is None:
            continue
        key = f"{state}|{str(district).strip()}"
        if key in overrides:
            mapping[(state, district)] = overrides[key]
            continue
        options = gadm.loc[gadm["state"] == state, "district"].tolist()
        hit = process.extractOne(str(district).strip().title(), options, scorer=fuzz.WRatio)
        if hit and hit[1] >= MATCH_FLOOR:
            mapping[(state, district)] = hit[0]
        else:
            weak.append(f"{state} / {district} -> best {hit[0] if hit else None!r} "
                        f"({hit[1]:.0f})" if hit else f"{state} / {district} -> none")
    stats["gdistrict"] = [mapping.get((s, d)) for s, d in zip(stats["gstate"], stats["district"])]

    rows = []
    per_year = stats.groupby(["gstate", "gdistrict", "season", "crop"])["area"].sum() / args.years
    state_level = stats.groupby(["gstate", "season", "crop"])["area"].sum() / args.years
    for (state, district) in gadm.itertuples(index=False):
        for season in ("kharif", "rabi"):
            try:
                area = per_year.loc[(state, district, season)]
                source = "district statistics"
            except KeyError:
                try:
                    area = state_level.loc[(state, season)]
                    source = "state default"
                except KeyError:
                    continue
            for rank, item in enumerate(main_crops(area), start=1):
                rows.append({"state": state, "district": district, "season": season,
                             "rank": rank, "crop": item["crop"], "share": item["share"],
                             "crop_source": source, "years": f"{last - args.years + 1}-{last}"})
    table = pd.DataFrame(rows)
    if table.empty:
        raise DataError("no main crops produced")
    table.to_parquet(OUT, index=False)
    cover = table.groupby("crop_source")[["state", "district"]].apply(
        lambda f: len(f.drop_duplicates()))
    print("  districts by crop source:", cover.to_dict())
    print_list("DES districts not matched to GADM (their GADM district uses the state "
               "default)", weak, limit=25)
    top = table[table["rank"] == 1].groupby(["season", "crop"]).size()
    print("  top kharif crops by #districts:",
          top.loc["kharif"].sort_values(ascending=False).head(8).to_dict())
    print("  top rabi crops by #districts:",
          top.loc["rabi"].sort_values(ascending=False).head(8).to_dict())
    summarize("build_crops", rows=len(table), files=[OUT],
              extra={"years averaged": f"{last - args.years + 1}-{last}",
                     "unmatched DES districts": len(weak)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
