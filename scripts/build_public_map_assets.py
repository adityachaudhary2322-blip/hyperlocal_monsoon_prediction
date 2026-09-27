"""Build the public dashboard's map files in app/static/ (served by Streamlit).

Run:  python scripts/build_public_map_assets.py

Two boundary sources, used in this order:

1. **`data/raw/official_boundary/`** - the Survey of India "Digital Vector Data" shapefiles
   (OVSF/1M/7, "entire country up to district level with HQ") downloaded by hand from
   onlinemaps.surveyofindia.gov.in. The portal needs a signed-in account, so this
   script cannot fetch it. When it is present the map is labelled official.
2. **`data/raw/boundary_mirror/SOI_{States,Districts}.parquet`** - the same Survey of India
   layers as republished by the india-geodata project (github.com/yashveeeeeeer/
   india-geodata, release tags `admin/states` and `admin/districts`). They carry SoI's
   own spellings, the `DISPUTED (...)` inter-state tracts and "Not Verified on Ground"
   remarks, but they are a third-party copy, so the map says "Boundaries indicative, not
   official" until source 1 exists.

Forecast polygons are NOT taken from either: they stay GADM level 3 from
`data/processed/units.gpkg`, because every forecast, weight row and advisory is keyed on
those unit ids (CLAUDE.md §5, §10).

Everything is simplified in EPSG:6933 so tolerances are metres, not degrees (§10, §17),
and each file is checked against the brief's 5 MB ceiling.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import pandas as pd  # noqa: E402
from rapidfuzz import fuzz, process  # noqa: E402

import numpy as np  # noqa: E402

from src.common import (DATA_RAW, EQUAL_AREA_CRS, GEO_CRS, UNITS_INDIA_GPKG,  # noqa: E402
                        regions)
from src.report import DataError, summarize  # noqa: E402

STATIC = ROOT / "app" / "static"
OFFICIAL_DIR = DATA_RAW / "official_boundary"
MIRROR_DIR = DATA_RAW / "boundary_mirror"

SIZE_LIMIT_MB = 5.0          # the brief: national layer and each state file < 5 MB
NATIONAL_TARGET_MB = 3.0     # the plan's tighter target for the first download

# Metres, in EPSG:6933. Coarse nationally, finer where people zoom in.
TOL_STATES_M = 800.0
TOL_DISTRICTS_M = 400.0
TOL_UNITS_M = 250.0
COORD_DECIMALS = 4           # ~11 m; far below any tolerance above

# A match below this is reported and refused, never silently accepted (CLAUDE.md §5).
MATCH_FLOOR = 90

# Field names seen in Survey of India releases, most specific first. The portal
# download has not been inspected yet, so these are candidates, not assumptions: the
# chosen field is printed on every run.
STATE_FIELDS = ("STATE_C", "STATE", "STATE_NAME", "ST_NM", "State_Name", "NAME_1")
DISTRICT_FIELDS = ("District_C", "District", "DISTRICT", "DIST_NAME", "dtname",
                   "NAME_2")

SMALL_WORDS = {"and", "of", "the"}


def title(raw: str) -> str:
    """'JAMMU AND KASHMIR' -> 'Jammu and Kashmir'; collapses double spaces."""
    words = re.sub(r"\s+", " ", str(raw)).strip().lower().split(" ")
    out = [w if (i and w in SMALL_WORDS) else w[:1].upper() + w[1:]
           for i, w in enumerate(words)]
    return " ".join(out)


def slug(state: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", state.lower()).strip("_")


def pick(frame: pd.DataFrame, candidates: tuple[str, ...], what: str) -> str:
    for name in candidates:
        if name in frame.columns:
            return name
    raise DataError(f"no {what} column among {list(frame.columns)}; "
                    f"expected one of {candidates}")


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def _read(path: Path) -> gpd.GeoDataFrame:
    frame = (gpd.read_parquet(path) if path.suffix == ".parquet"
             else gpd.read_file(path))
    if frame.crs is None:
        raise DataError(f"{path} has no CRS; refusing to guess one")
    return frame.to_crs(GEO_CRS)


def load_boundaries() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    """(states, districts, provenance). Official folder first, then the mirror."""
    shapefiles = sorted(OFFICIAL_DIR.rglob("*.shp")) if OFFICIAL_DIR.is_dir() else []
    if shapefiles:
        print(f"  official_boundary/: {len(shapefiles)} shapefile(s)")
        for shp in shapefiles:
            print(f"    - {shp.relative_to(DATA_RAW).as_posix()}")
        # The district layer is the one with a district-name field; states are its
        # dissolve, which keeps both layers topologically identical.
        districts = None
        for shp in shapefiles:
            frame = _read(shp)
            if any(f in frame.columns for f in DISTRICT_FIELDS):
                districts = frame
                source_file = shp
                break
        if districts is None:
            raise DataError("official_boundary/ has no layer with a district field "
                            f"{DISTRICT_FIELDS}; which file is OVSF/1M/7?")
        state_field = pick(districts, STATE_FIELDS, "state")
        states = districts.dissolve(by=state_field, as_index=False)
        provenance = {
            "official": True,
            "source": "Survey of India, Digital Vector Data 1:1M (OVSF/1M/7)",
            "file": source_file.relative_to(ROOT).as_posix(),
        }
        return states, districts, provenance

    states_file = MIRROR_DIR / "SOI_States.parquet"
    districts_file = MIRROR_DIR / "SOI_Districts.parquet"
    for path in (states_file, districts_file):
        if not path.is_file():
            raise DataError(
                f"no boundary source: {OFFICIAL_DIR} is empty and {path} is missing. "
                "Download OVSF/1M/7 from onlinemaps.surveyofindia.gov.in into "
                "official_boundary/, or fetch the mirror (see this module's docstring).")
    print("  official_boundary/ is empty - using the Survey of India mirror; the map "
          "will say 'Boundaries indicative, not official'")
    provenance = {
        "official": False,
        "source": ("Survey of India layers via the india-geodata mirror "
                   "(github.com/yashveeeeeeer/india-geodata)"),
        "file": f"{states_file.relative_to(ROOT).as_posix()}, "
                f"{districts_file.relative_to(ROOT).as_posix()}",
    }
    return _read(states_file), _read(districts_file), provenance


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------
def clean(frame: gpd.GeoDataFrame, tolerance_m: float, label: str) -> gpd.GeoDataFrame:
    """make_valid -> simplify in metres -> back to WGS84; empty results are errors."""
    projected = frame.to_crs(EQUAL_AREA_CRS)
    projected["geometry"] = projected.geometry.make_valid()
    projected["geometry"] = projected.geometry.simplify(tolerance_m,
                                                        preserve_topology=True)
    # make_valid can return a GeometryCollection (a polygon plus a stray line where two
    # rings touched). Browsers and MapLibre want plain (Multi)Polygons, so keep only the
    # polygonal parts.
    projected["geometry"] = projected.geometry.map(polygonal)
    out = projected.to_crs(GEO_CRS)
    empty = out.geometry.is_empty | out.geometry.isna()
    if empty.any():
        raise DataError(f"{label}: simplifying at {tolerance_m:.0f} m emptied "
                        f"{int(empty.sum())} geometries; lower the tolerance")
    return out


def polygonal(geom):
    from shapely.geometry import MultiPolygon, Polygon

    if geom is None or geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = []
    for part in getattr(geom, "geoms", []):
        if isinstance(part, Polygon):
            parts.append(part)
        elif isinstance(part, MultiPolygon):
            parts.extend(part.geoms)
    return MultiPolygon(parts) if parts else Polygon()


def write_geojson(frame: gpd.GeoDataFrame, path: Path, limit_mb: float) -> dict:
    if frame.empty:
        raise DataError(f"refusing to write an empty {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    # Round coordinates here, with shapely, and re-check validity. Letting GDAL do it
    # (COORDINATE_PRECISION together with RFC7946=YES) "repairs" rounded rings into
    # GeometryCollections of polygons, lines and points, which break the map.
    if frame.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        import shapely

        frame = frame.copy()
        frame["geometry"] = shapely.set_precision(frame.geometry.values,
                                                  10 ** -COORD_DECIMALS)
        frame["geometry"] = frame.geometry.map(polygonal)
        bad = ~frame.geom_type.isin(["Polygon", "MultiPolygon"]) | frame.geometry.is_empty
        if bad.any():
            raise DataError(f"{path.name}: {int(bad.sum())} geometries not polygonal "
                            "after rounding")
    frame.to_file(path, driver="GeoJSON", COORDINATE_PRECISION=COORD_DECIMALS,
                  WRITE_NAME="NO")
    size_mb = path.stat().st_size / 1e6
    if size_mb > limit_mb:
        raise DataError(f"{path.name} is {size_mb:.2f} MB, over {limit_mb} MB; "
                        "raise the tolerance")
    return {"path": path, "rows": len(frame), "size_mb": size_mb}


def bbox(geometry) -> list[float]:
    minx, miny, maxx, maxy = geometry.bounds
    return [round(float(v), 4) for v in (minx, miny, maxx, maxy)]


# --------------------------------------------------------------------------
# Name matching (CLAUDE.md §5: print what is in the file; never guess a spelling)
# --------------------------------------------------------------------------
def match_covered(state_names: list[str]) -> dict[str, str]:
    """boundary state name -> GADM NAME_1, for the 5 covered regions only."""
    candidates = [n for n in state_names if not n.lower().startswith("disputed")]
    matches: dict[str, str] = {}
    print("  covered-state matches (boundary name -> GADM NAME_1, score):")
    for region in regions():
        found = process.extractOne(region, candidates, scorer=fuzz.WRatio,
                                   processor=lambda s: s.lower())
        if found is None or found[1] < MATCH_FLOOR:
            raise DataError(f"no boundary state matches {region!r} "
                            f"(best: {found}); check the source's state names")
        name, score, _ = found
        print(f"    {name!r:>22} -> {region!r} ({score:.0f})")
        matches[name] = region
    if len(set(matches)) != len(regions()):
        raise DataError(f"two regions matched the same boundary state: {matches}")
    return matches


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print("--- build_public_map_assets ---")

    states_raw, districts_raw, provenance = load_boundaries()
    s_field = pick(states_raw, STATE_FIELDS, "state")
    d_state_field = pick(districts_raw, STATE_FIELDS, "district-state")
    d_field = pick(districts_raw, DISTRICT_FIELDS, "district")
    print(f"  fields: states.{s_field}, districts.{d_field} / districts.{d_state_field}")

    states_raw = states_raw.assign(state=states_raw[s_field].map(title))
    distinct = sorted(states_raw["state"].unique())
    print(f"  {len(distinct)} state values found in the file:")
    print("    " + ", ".join(distinct))
    covered = match_covered(distinct)

    # ---- national states layer ------------------------------------------------
    states = states_raw.dissolve(by="state", as_index=False)[["state", "geometry"]]
    states["disputed"] = states["state"].str.lower().str.startswith("disputed")
    states["gadm_state"] = states["state"].map(covered)
    states["covered"] = states["gadm_state"].notna()
    states["state_key"] = states["state"].map(slug)
    states = clean(states, TOL_STATES_M, "states")
    written = [write_geojson(states, STATIC / "india_states.geojson",
                             NATIONAL_TARGET_MB)]

    # The national outline is the dissolve of the same state polygons, so the India
    # border drawn on the map is exactly the source's - never the basemap's.
    outline = gpd.GeoDataFrame(
        {"name": ["India"]},
        geometry=[polygonal(states.to_crs(EQUAL_AREA_CRS).geometry.buffer(0).union_all())],
        crs=EQUAL_AREA_CRS).to_crs(GEO_CRS)
    written.append(write_geojson(outline, STATIC / "india_outline.geojson",
                                 NATIONAL_TARGET_MB))

    # Label anchors inside each polygon (a centroid can fall outside a U-shape).
    labels = states[~states["disputed"]][["state", "state_key", "covered"]].copy()
    labels["geometry"] = (states[~states["disputed"]].to_crs(EQUAL_AREA_CRS)
                          .geometry.representative_point().to_crs(GEO_CRS))
    labels = gpd.GeoDataFrame(labels, geometry="geometry", crs=GEO_CRS)
    written.append(write_geojson(labels, STATIC / "india_state_labels.geojson",
                                 NATIONAL_TARGET_MB))

    # ---- national district centroids (weather) + search -----------------------
    districts = districts_raw.assign(
        district=districts_raw[d_field].map(title),
        state=districts_raw[d_state_field].map(title),
    )[["district", "state", "geometry"]]
    districts = districts[~districts["state"].str.lower().str.startswith("disputed")]
    projected = districts.to_crs(EQUAL_AREA_CRS)
    projected["geometry"] = projected.geometry.make_valid()
    # representative_point is guaranteed inside the polygon; a centroid of a
    # crescent-shaped district can fall in its neighbour.
    points = projected.geometry.representative_point().to_crs(GEO_CRS)
    centroids = pd.DataFrame({
        "district": districts["district"].to_numpy(),
        "state": districts["state"].to_numpy(),
        "state_key": districts["state"].map(slug).to_numpy(),
        "lat": points.y.round(4).to_numpy(),
        "lon": points.x.round(4).to_numpy(),
    }).sort_values(["state", "district"]).reset_index(drop=True)
    if centroids.empty:
        raise DataError("no district centroids")
    centroids_path = STATIC / "district_centroids.csv"
    centroids.to_csv(centroids_path, index=False, encoding="utf-8")
    written.append({"path": centroids_path, "rows": len(centroids),
                    "size_mb": centroids_path.stat().st_size / 1e6})

    search: list[dict] = []

    # ---- every state: SoI districts + SoI sub-districts (CC0), coloured by GADM ----
    # The public map draws Survey of India sub-districts; the forecasts are made on
    # GADM level-3 units (unit ids, weights). Each SoI sub-district takes the GADM unit
    # that covers most of its area, so colours come from real forecasts while the
    # polygons shown are openly licensed.
    if not UNITS_INDIA_GPKG.is_file():
        raise DataError("missing units_india.gpkg; run python -m src.build_units --national")
    gadm = gpd.read_file(UNITS_INDIA_GPKG, layer="units")[["unit_id", "geometry"]]
    gadm = gadm.to_crs(EQUAL_AREA_CRS)
    gadm["geometry"] = gadm.geometry.make_valid()
    subs_path = MIRROR_DIR / "SOI_Subdistricts.parquet"
    if not subs_path.is_file():
        raise DataError(f"missing {subs_path}; download admin/subdistricts from india-geodata")
    subs = _read(subs_path)
    subs["geometry"] = subs.geometry.make_valid().map(polygonal)   # some rings self-touch
    subs = subs.assign(name=subs["TEHSIL_C"].map(title), district=subs["District_C"].map(title),
                       state=subs["STATE_C"].map(title))
    subs = subs[~subs["state"].str.contains("disputed", case=False)].reset_index(drop=True)
    # The sub-district layer spells some states differently from the state layer
    # ("Chhatisgarh"); map each to the state layer's name, and print every change.
    canonical = [n for n in distinct if not n.lower().startswith("disputed")]
    fix = {}
    squash = {re.sub(r"[^a-z]", "", n.lower()): n for n in canonical}
    for raw in sorted(subs["state"].unique()):
        exact = squash.get(re.sub(r"[^a-z]", "", raw.lower()))
        hit = (exact, 100.0, None) if exact else             process.extractOne(raw, canonical, scorer=fuzz.WRatio)
        if not hit or hit[1] < MATCH_FLOOR:
            raise DataError(f"sub-district state {raw!r} matches no state (best {hit})")
        fix[raw] = hit[0]
        if hit[0] != raw:
            print(f"    sub-district state {raw!r} -> {hit[0]!r} ({hit[1]:.0f})")
    subs["state"] = subs["state"].map(fix)
    subs["sid"] = np.arange(len(subs))
    subs["state_key"] = subs["state"].map(slug)
    subs["did"] = subs["state_key"] + ":" + subs["district"]
    ea = subs[["sid", "geometry"]].to_crs(EQUAL_AREA_CRS)
    ea["geometry"] = ea.geometry.make_valid()
    ea["area"] = ea.area
    inter = gpd.overlay(ea, gadm, how="intersection", keep_geom_type=True)
    inter["share"] = inter.area / inter["area"]
    best = inter.sort_values("share", ascending=False).drop_duplicates("sid").set_index("sid")
    subs["unit_id"] = subs["sid"].map(best["unit_id"])
    subs["unit_share"] = subs["sid"].map(best["share"]).round(2)
    print(f"  SoI sub-districts: {len(subs):,}; matched to a GADM unit: "
          f"{subs['unit_id'].notna().sum():,}; median share of the match: "
          f"{subs['unit_share'].median():.2f}")

    # Districts are the dissolve of the sub-districts, so the two layers always agree on
    # names and edges (the separate SoI district layer spells some districts differently).
    soi_d = subs[["state", "state_key", "district", "did", "geometry"]].dissolve(
        by=["state_key", "did"], as_index=False, aggfunc="first")
    per_state: dict[str, dict] = {}
    for key, group in subs.groupby("state_key"):
        units_s = clean(group[["unit_id", "name", "district", "did", "state_key", "geometry"]],
                        TOL_UNITS_M, f"{key} sub-districts")
        dist_s = clean(soi_d[soi_d["state_key"] == key][["district", "did", "state_key", "geometry"]],
                       TOL_DISTRICTS_M, f"{key} districts")
        written.append(write_geojson(units_s, STATIC / f"units_{key}.geojson", SIZE_LIMIT_MB))
        written.append(write_geojson(dist_s, STATIC / f"districts_{key}.geojson", SIZE_LIMIT_MB))
        state_name = group["state"].iloc[0]
        per_state[key] = {"state": state_name, "units": len(units_s), "districts": len(dist_s)}
        for row in dist_s.itertuples():
            search.append({"n": row.district, "s": state_name, "k": "district",
                           "b": bbox(row.geometry), "st": key})
        for row in units_s.itertuples():
            b = bbox(row.geometry)
            entry = {"n": row.name, "d": row.district, "s": state_name, "k": "subdistrict",
                     "b": b, "st": key, "c": [round((b[0] + b[2]) / 2, 3), round((b[1] + b[3]) / 2, 3)]}
            if isinstance(row.unit_id, str):
                entry["id"] = row.unit_id
            search.append(entry)

    # ---- LGD blocks for "Find your block": names and extents only ------------------
    blocks_path = MIRROR_DIR / "LGD_Blocks.parquet"
    if blocks_path.is_file():
        blocks = _read(blocks_path)
        names = {n: slug(n) for n in subs["state"].unique()}      # corrected names

        def squash_name(text: str) -> str:
            return re.sub(r"[^a-z]", "", text.lower().replace("&", "and"))
        by_squash = {squash_name(n): k for n, k in names.items()}
        # LGD writes the merged UT without the joining "and"s.
        by_squash["dadranagarhavelidamananddiu"] = slug("Dadra & Nagar Haveli & Daman & Diu")
        state_map = {}
        for raw in blocks["state"].astype(str).unique():
            key = by_squash.get(squash_name(raw))
            if key is None:
                hit = process.extractOne(title(raw).replace("&", "and"), list(names),
                                         scorer=fuzz.WRatio)
                key = names[hit[0]] if hit and hit[1] >= MATCH_FLOOR else None
            state_map[raw] = key
        unmatched = sorted(k for k, v in state_map.items() if v is None)
        n_blocks = 0
        for row in blocks.itertuples():
            key = state_map.get(str(row.state))
            if not key or row.geometry is None or row.geometry.is_empty:
                continue
            b = bbox(row.geometry)
            search.append({"n": title(row.block_name), "d": title(str(row.district)).replace(" District", ""),
                           "s": per_state.get(key, {}).get("state", title(row.state)),
                           "k": "block", "b": b, "st": key,
                           "c": [round((b[0] + b[2]) / 2, 3), round((b[1] + b[3]) / 2, 3)]})
            n_blocks += 1
        print(f"  LGD blocks indexed: {n_blocks:,}" +
              (f"; state names not matched: {unmatched}" if unmatched else ""))

    search_path = STATIC / "search_index.json"
    search_path.write_text(json.dumps(search, ensure_ascii=False,
                                      separators=(",", ":")), encoding="utf-8")
    written.append({"path": search_path, "rows": len(search),
                    "size_mb": search_path.stat().st_size / 1e6})

    validated = {region: {"key": slug(name), **per_state.get(slug(name), {})}
                 for name, region in covered.items()}
    meta = {
        **provenance,
        "built_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "covered": validated,
        "states": per_state,
        "subdistrict_source": "Survey of India sub-districts (india-geodata mirror, CC0); "
                              "forecast values from the GADM 4.1 level-3 unit covering most "
                              "of each",
        "tolerances_m": {"states": TOL_STATES_M, "districts": TOL_DISTRICTS_M,
                         "units": TOL_UNITS_M},
    }
    meta_path = STATIC / "boundary_source.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    written.append({"path": meta_path, "rows": 1,
                    "size_mb": meta_path.stat().st_size / 1e6})

    print("  districts / sub-districts per state (Survey of India layers):")
    for key, info in sorted(per_state.items(), key=lambda kv: -kv[1]["units"]):
        print(f"    {info['state']:<30} {info['districts']:>3} districts  "
              f"{info['units']:>4} sub-districts")
    for w in written:
        print(f"  {w['path'].relative_to(ROOT).as_posix():<48} "
              f"{w['rows']:>5} rows  {w['size_mb']:.2f} MB")

    summarize(
        "build_public_map_assets",
        rows=sum(w["rows"] for w in written),
        files=[w["path"] for w in written],
        extra={
            "boundary source": provenance["source"],
            "official": provenance["official"],
            "national districts (weather)": len(centroids),
            "search entries": len(search),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
