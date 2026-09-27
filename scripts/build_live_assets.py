"""Small, static inputs for the daily live engine -> app/assets/live/ (committed).

Run:  python scripts/build_live_assets.py

GitHub Actions has no access to data/ (gitignored, ~1 GB), so everything the live run
needs that is derived from it is precomputed here, once, and kept small:

  grid_05.npz / grid_15.npz  the 0.5 and 1.5 degree forecast points over India's units,
                             and the sparse unit x point weight matrix for each
                             (src.build_weights.build: EPSG:6933 areas, rows sum to 1)
  weights_imd.npz            the national unit x IMD-cell matrix (real-time IMD -> units)
  units.csv                  unit_id, names, state, tier, seasonal_low, a label point
  climatology.npz            per unit and calendar week, 7-day rainfall mean and 33rd /
                             67th percentiles from the TRAINING years 1990-2018 only
                             (CLAUDE.md §11), for "vs normal" and tercile outlooks
  district_crops.json        main crops per district (data/processed/district_crops.parquet)

Nothing here changes day to day, so committing it does not trigger daily redeploys.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import scipy.sparse as sp  # noqa: E402

from src.build_weights import build  # noqa: E402
from src.common import (DATA_PROCESSED, EQUAL_AREA_CRS, GEO_CRS, UNIT_RAIN_INDIA_PARQUET,  # noqa: E402
                        UNITS_INDIA_GPKG, WEIGHTS_INDIA_NPZ, load_config)
from src.report import DataError, summarize  # noqa: E402

OUT = ROOT / "app" / "assets" / "live"
GRIDS = {"05": 0.5, "15": 1.5}
N_WEEKS = 52


def forecast_grid(units: gpd.GeoDataFrame, step: float, tag: str) -> dict:
    """Cells of `step` degrees that overlap at least one unit, and the unit weights."""
    minx, miny, maxx, maxy = units.total_bounds
    lat = np.arange(np.floor(miny / step) * step + step / 2, maxy + step, step)
    lon = np.arange(np.floor(minx / step) * step + step / 2, maxx + step, step)
    valid = np.ones(lat.size * lon.size, dtype=bool)
    W, cells_per_unit, _, _, _ = build(units, lat, lon, valid, cell_deg=step)
    used = np.unique(W.indices)                      # columns that touch a unit
    W = W[:, used].tocsr()
    pts_lat = lat[used // lon.size]
    pts_lon = lon[used % lon.size]
    if (cells_per_unit == 0).any():
        raise DataError(f"grid {tag}: {int((cells_per_unit == 0).sum())} units touch no cell")
    np.savez_compressed(OUT / f"grid_{tag}.npz", lat=pts_lat.astype(np.float32),
                        lon=pts_lon.astype(np.float32), step=np.float32(step),
                        data=W.data.astype(np.float32), indices=W.indices, indptr=W.indptr,
                        shape=np.array(W.shape), unit_id=units["unit_id"].to_numpy().astype("U32"))
    print(f"  grid {step} deg: {used.size:,} points over India's units, "
          f"W {W.shape[0]:,} x {W.shape[1]:,}, {W.nnz:,} non-zeros")
    return {"points": int(used.size)}


def climatology(unit_ids: np.ndarray) -> dict:
    """7-day totals starting on each calendar week, over the training years only."""
    train = load_config("project")["splits"]["train"]
    years = range(train[0], train[1] + 1)
    starts = 1 + 7 * np.arange(N_WEEKS)              # day-of-year of each week start
    totals = np.full((len(years), N_WEEKS, unit_ids.size), np.nan, dtype=np.float32)
    order = {u: i for i, u in enumerate(unit_ids)}
    for yi, year in enumerate(years):
        part = UNIT_RAIN_INDIA_PARQUET / f"year={year}"
        frame = pd.read_parquet(part, columns=["unit_id", "date", "rain_mm"])
        frame["unit_id"] = frame["unit_id"].astype(str)
        wide = frame.pivot(index="date", columns="unit_id", values="rain_mm")
        wide = wide.reindex(columns=unit_ids)
        values = wide.to_numpy(dtype=np.float32)      # (days, units)
        cum = np.vstack([np.zeros((1, values.shape[1]), np.float32),
                         np.nancumsum(values, axis=0)])
        for wi, doy in enumerate(starts):
            if doy - 1 + 7 <= values.shape[0]:
                totals[yi, wi] = cum[doy - 1 + 7] - cum[doy - 1]
        missing = np.isnan(values).all(axis=0)
        totals[yi, :, missing] = np.nan
    np.savez_compressed(
        OUT / "climatology.npz", unit_id=unit_ids.astype("U32"), week_start_doy=starts,
        years=np.array([train[0], train[1]]),
        mean=np.nanmean(totals, axis=0).astype(np.float32),
        p33=np.nanpercentile(totals, 100 / 3, axis=0).astype(np.float32),
        p67=np.nanpercentile(totals, 200 / 3, axis=0).astype(np.float32))
    print(f"  climatology: {N_WEEKS} weeks x {unit_ids.size:,} units from "
          f"{train[0]}-{train[1]} (training years only)")
    return {"weeks": N_WEEKS}


def main() -> int:
    print("--- build_live_assets ---")
    if not UNITS_INDIA_GPKG.is_file():
        raise DataError("missing units_india.gpkg; run python -m src.build_units --national")
    OUT.mkdir(parents=True, exist_ok=True)
    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units").to_crs(GEO_CRS)

    pts = units.to_crs(EQUAL_AREA_CRS).geometry.representative_point().to_crs(GEO_CRS)
    table = pd.DataFrame({
        "unit_id": units["unit_id"], "unit_name": units["unit_name"],
        "name_ok": ~units["name_is_placeholder"].astype(bool),
        "district": units["district"], "state": units["state"], "tier": units["tier"],
        "seasonal_low": units["seasonal_low"].astype(bool),
        "lat": pts.y.round(4), "lon": pts.x.round(4),
        "area_km2": units["area_km2"].round(1)})
    table.to_csv(OUT / "units.csv", index=False)

    info = {tag: forecast_grid(units, step, tag) for tag, step in GRIDS.items()}
    shutil.copyfile(WEIGHTS_INDIA_NPZ, OUT / "weights_imd.npz")
    climatology(units["unit_id"].to_numpy())

    crops = pd.read_parquet(DATA_PROCESSED / "district_crops.parquet")
    by_district: dict = {}
    for r in crops.itertuples():
        d = by_district.setdefault(f"{r.state}|{r.district}", {"source": r.crop_source})
        d.setdefault(r.season, []).append(r.crop)
    (OUT / "district_crops.json").write_text(json.dumps(by_district, separators=(",", ":")),
                                             encoding="utf-8")

    files = sorted(OUT.glob("*"))
    total = sum(f.stat().st_size for f in files)
    for f in files:
        print(f"  {f.name:<22} {f.stat().st_size / 1e3:8.0f} kB")
    if total > 20e6:
        raise DataError(f"live assets are {total / 1e6:.1f} MB; keep them small")
    summarize("build_live_assets", rows=len(table), files=files,
              extra={"points 0.5": info["05"]["points"], "points 1.5": info["15"]["points"],
                     "total": f"{total / 1e6:.2f} MB"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
