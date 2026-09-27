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

from src.common import (DATA_RAW, EQUAL_AREA_CRS, GEO_CRS, UNITS_GPKG,  # noqa: E402
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
    covered_keys = {slug(n) for n in covered}
    district_bounds = districts.to_crs(GEO_CRS)
    for row in district_bounds.itertuples():
        if slug(row.state) in covered_keys:
            continue            # covered states are indexed from the GADM units below
        search.append({"n": row.district, "s": row.state, "k": "district",
                       "b": bbox(row.geometry)})

    # ---- covered states: GADM districts + sub-districts -----------------------
    if not UNITS_GPKG.is_file():
        raise DataError(f"missing {UNITS_GPKG}; run python -m src.build_units")
    units_all = gpd.read_file(UNITS_GPKG, layer="units").to_crs(GEO_CRS)
    per_state: dict[str, dict] = {}
    state_key_for = {region: slug(name) for name, region in covered.items()}
    for region in regions():
        key = state_key_for[region]
        units = units_all[units_all["state"] == region].copy()
        if units.empty:
            raise DataError(f"units.gpkg has no units for {region!r}")
        placeholder = units.get("name_is_placeholder",
                                pd.Series(False, index=units.index))
        units["name_ok"] = ~placeholder.fillna(False).astype(bool)
        # `did` joins a unit to its district for the district-level colour.
        units["did"] = key + ":" + units["district"]
        units["state_key"] = key
        units = units[["unit_id", "unit_name", "district", "did", "state",
                       "state_key", "name_ok", "geometry"]]
        units_s = clean(units, TOL_UNITS_M, f"{region} units")
        districts_s = clean(
            units.dissolve(by="district", as_index=False)[["district", "did", "state",
                                                           "state_key", "geometry"]],
            TOL_DISTRICTS_M, f"{region} districts")
        written.append(write_geojson(units_s, STATIC / f"units_{key}.geojson",
                                     SIZE_LIMIT_MB))
        written.append(write_geojson(districts_s, STATIC / f"districts_{key}.geojson",
                                     SIZE_LIMIT_MB))
        per_state[region] = {"key": key, "units": len(units_s),
                             "districts": len(districts_s)}
        for row in districts_s.itertuples():
            search.append({"n": row.district, "s": region, "k": "district",
                           "b": bbox(row.geometry), "st": key})
        for row in units_s.itertuples():
            if row.name_ok:
                search.append({"n": row.unit_name, "d": row.district, "s": region,
                               "k": "subdistrict", "b": bbox(row.geometry),
                               "st": key, "id": row.unit_id})

    search_path = STATIC / "search_index.json"
    search_path.write_text(json.dumps(search, ensure_ascii=False,
                                      separators=(",", ":")), encoding="utf-8")
    written.append({"path": search_path, "rows": len(search),
                    "size_mb": search_path.stat().st_size / 1e6})

    meta = {
        **provenance,
        "built_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "covered": {region: per_state[region] for region in regions()},
        "subdistrict_source": "GADM 4.1 level 3 (not blocks; see CLAUDE.md §5)",
        "tolerances_m": {"states": TOL_STATES_M, "districts": TOL_DISTRICTS_M,
                         "units": TOL_UNITS_M},
    }
    meta_path = STATIC / "boundary_source.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    written.append({"path": meta_path, "rows": 1,
                    "size_mb": meta_path.stat().st_size / 1e6})

    print("  units per covered state (GADM L3 - uneven, see CLAUDE.md §5):")
    for region, info in per_state.items():
        print(f"    {region:<16} {info['districts']:>3} districts  "
              f"{info['units']:>3} sub-districts  -> *_{info['key']}.geojson")
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
