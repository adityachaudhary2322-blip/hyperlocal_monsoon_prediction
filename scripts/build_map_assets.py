"""Write simplified unit polygons to app/assets/ so the Space needs no GDAL data.

Run:  python scripts/build_map_assets.py [--tolerance 300]

`data/processed/units.gpkg` is 5.0 MB, gitignored, and built by the Phase 1 pipeline -
none of which travels to a Hugging Face Space. The Risk Map needs geometry, so it gets a
committed, simplified GeoJSON instead.

Two reasons it is GeoJSON rather than a copied GeoPackage:

* **No geopandas at runtime.** The app reads these files with the standard library and
  hands them straight to folium, which keeps geopandas, pyogrio and GDAL out of the
  Space image entirely - they are the heaviest part of the dependency tree and are only
  ever needed to *build* the pipeline's own data.
* **Simplification is measurable.** Geometry is simplified in EPSG:6933, where the
  tolerance is metres rather than degrees, so "300 m" means the same thing in Bihar and
  in coastal Maharashtra. Simplifying in degrees would distort by latitude, the same
  trap the area-weight matrix avoids (CLAUDE.md §10).

Centroids and a bounding box are precomputed into the file, so nothing at runtime has to
reproject to find where to centre the map.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402

from src.common import EQUAL_AREA_CRS, GEO_CRS, UNITS_GPKG, pilot_districts  # noqa: E402
from src.report import DataError, summarize  # noqa: E402

ASSETS = ROOT / "app" / "assets"
PILOT_FILE = ASSETS / "pilot_units.geojson"
ALL_FILE = ASSETS / "all_units.geojson"
# The layer the Risk Map reads, one state at a time. A GeoPackage rather than one big
# GeoJSON because it can be queried with a WHERE clause without parsing the whole
# file - which is the difference between loading one state's shapes and all of them
# on a 2.7 GB tier.
GPKG_FILE = ASSETS / "map_layers_simplified.gpkg"
GPKG_LAYER = "units"

# Hugging Face keeps git-tracked files small; the brief caps the layers at 10 MB.
SIZE_LIMIT_MB = 10.0

KEEP = ["unit_id", "unit_name", "district", "state", "zone_id", "zone_name"]


def simplify(units: gpd.GeoDataFrame, tolerance_m: float) -> gpd.GeoDataFrame:
    """Simplify in metres, then return to WGS84 for the browser."""
    projected = units.to_crs(EQUAL_AREA_CRS)
    projected["geometry"] = projected.geometry.simplify(
        tolerance_m, preserve_topology=True)
    out = projected.to_crs(GEO_CRS)

    # A tolerance large enough to collapse a tiny island would silently drop it from
    # the map, so an empty result is an error rather than a smaller file.
    broken = out[out.geometry.is_empty | out.geometry.isna()]
    if not broken.empty:
        raise DataError(
            f"simplifying at {tolerance_m} m emptied {len(broken)} geometries "
            f"({', '.join(broken['unit_name'].head(5))}); lower --tolerance"
        )
    return out


def write(units: gpd.GeoDataFrame, path: Path, tolerance_m: float) -> dict:
    centroid = units.to_crs(EQUAL_AREA_CRS).geometry.centroid.to_crs(GEO_CRS)
    units = units.copy()
    units["centroid_lat"] = centroid.y.to_numpy().round(5)
    units["centroid_lon"] = centroid.x.to_numpy().round(5)

    columns = [c for c in KEEP if c in units.columns] + \
              ["centroid_lat", "centroid_lon", "geometry"]
    slim = units[columns]

    payload = json.loads(slim.to_json(drop_id=True))
    minx, miny, maxx, maxy = slim.total_bounds
    payload["bbox"] = [round(float(v), 5) for v in (minx, miny, maxx, maxy)]
    payload["properties"] = {
        "source": "GADM 4.1 level 3, simplified",
        "simplify_tolerance_m": tolerance_m,
        "crs_note": f"simplified in {EQUAL_AREA_CRS}, stored in {GEO_CRS}",
        "units": len(slim),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    # Trim coordinate precision: 5 decimals is ~1 m, far finer than a 300 m simplify,
    # and full float repr triples the file size for no visible difference.
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_mb = path.stat().st_size / 1e6
    return {"path": path, "units": len(slim), "size_mb": size_mb}


def write_gpkg(units: gpd.GeoDataFrame, tolerance_m: float) -> dict:
    """One GeoPackage layer, indexed on `state` so a per-state read is cheap."""
    centroid = units.to_crs(EQUAL_AREA_CRS).geometry.centroid.to_crs(GEO_CRS)
    out = units.copy()
    out["centroid_lat"] = centroid.y.to_numpy().round(5)
    out["centroid_lon"] = centroid.x.to_numpy().round(5)
    out["is_pilot"] = out["is_pilot"] if "is_pilot" in out.columns else False

    columns = [c for c in KEEP if c in out.columns] +               ["is_pilot", "centroid_lat", "centroid_lon", "geometry"]
    slim = out[columns]

    if GPKG_FILE.exists():
        # GeoPandas appends to an existing layer by default, which would double the
        # rows on a second run.
        GPKG_FILE.unlink()
    slim.to_file(GPKG_FILE, layer=GPKG_LAYER, driver="GPKG")

    import sqlite3

    with sqlite3.connect(GPKG_FILE) as connection:
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{GPKG_LAYER}_state "
            f"ON {GPKG_LAYER}(state)")
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{GPKG_LAYER}_unit "
            f"ON {GPKG_LAYER}(unit_id)")
    return {"path": GPKG_FILE, "units": len(slim),
            "size_mb": GPKG_FILE.stat().st_size / 1e6}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tolerance", type=float, default=300.0,
                        help="simplification tolerance in metres (default 300)")
    parser.add_argument("--all-units", action="store_true",
                        help="also write all 739 units, not just the 30 pilots")
    args = parser.parse_args()

    print("--- build_map_assets ---")
    if not UNITS_GPKG.is_file():
        raise DataError(f"missing {UNITS_GPKG}; run python -m src.build_units first")

    units = gpd.read_file(UNITS_GPKG, layer="units")
    print(f"  read {len(units)} units from {UNITS_GPKG.name} "
          f"({UNITS_GPKG.stat().st_size / 1e6:.1f} MB)")

    mask = units["state"].isin([])
    for state, districts in pilot_districts().items():
        mask |= (units["state"] == state) & units["district"].isin(districts)
    pilots = units[mask].copy()
    if pilots.empty:
        raise DataError("no pilot units matched config/project.yaml")

    simplified_pilots = simplify(pilots, args.tolerance)
    pilot_ids = set(pilots["unit_id"])

    # The seed in src/db/init.py reads this one: small, and GDAL-free.
    written = [write(simplified_pilots, PILOT_FILE, args.tolerance)]

    # The map layer covers every unit so a full run has geometry, with is_pilot to
    # mark the 30 the pipeline actually forecasts.
    all_simplified = simplify(units, args.tolerance)
    all_simplified["is_pilot"] = all_simplified["unit_id"].isin(pilot_ids)
    written.append(write_gpkg(all_simplified, args.tolerance))

    if args.all_units:
        written.append(write(all_simplified, ALL_FILE, args.tolerance))

    total_mb = sum(w["size_mb"] for w in written)
    for w in written:
        print(f"  {w['path'].relative_to(ROOT).as_posix()}: "
              f"{w['units']} units, {w['size_mb']:.2f} MB")
    if total_mb > SIZE_LIMIT_MB:
        raise DataError(
            f"assets total {total_mb:.1f} MB, over the {SIZE_LIMIT_MB} MB limit; "
            "raise --tolerance or drop --all-units"
        )

    summarize(
        "build_map_assets",
        rows=sum(w["units"] for w in written),
        files=[w["path"] for w in written],
        extra={
            "tolerance": f"{args.tolerance:.0f} m (in {EQUAL_AREA_CRS})",
            "total size": f"{total_mb:.2f} MB of {SIZE_LIMIT_MB:.0f} MB allowed",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
