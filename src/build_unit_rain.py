"""Turn the clipped rainfall grid into daily rainfall per unit.

Run:  python -m src.build_unit_rain [--force]

The whole point of the cached weight matrix (CLAUDE.md section 10):

    unit_rain[t, u] = sum_c grid[t, c] * W[u, c]      ->   daily @ W.T

One matrix multiply per year. Polygons are never clipped per day. Cells with no
observations were already dropped from W and its rows renormalised, so replacing NaN
with 0 in the grid contributes nothing to any unit's mean - it is not a fill, it is
arithmetic on weights that are already zero there.
"""

from __future__ import annotations

import argparse
import shutil

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp
import xarray as xr

from src.common import RAIN_GRID_NC, UNITS_GPKG, UNIT_RAIN_PARQUET, WEIGHTS_NPZ
from src.report import DataError, require_nonempty, summarize

# A day above this in a unit mean would be a record for India; used as a tripwire,
# not as a cap - nothing is clipped, the run fails so the cause gets looked at.
IMPLAUSIBLE_DAILY_MM = 1200.0


def load_weights() -> dict:
    if not WEIGHTS_NPZ.is_file():
        raise DataError(f"missing {WEIGHTS_NPZ}; run python -m src.build_weights first")
    npz = np.load(WEIGHTS_NPZ, allow_pickle=False)
    W = sp.csr_matrix(
        (npz["data"], npz["indices"], npz["indptr"]), shape=tuple(npz["shape"])
    )
    return {
        "W": W,
        "unit_id": npz["unit_id"],
        "lat": npz["lat"],
        "lon": npz["lon"],
        "small_unit": npz["small_unit"],
        "no_valid_cells": npz["no_valid_cells"],
        # Raw intersection areas (m^2), for the conservation identity below.
        "unit_area_valid": npz["unit_area_valid"],
        "cell_coverage": npz["cell_coverage"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if UNIT_RAIN_PARQUET.exists() and not args.force:
        print(f"{UNIT_RAIN_PARQUET} exists; pass --force to rebuild")
        return 0

    print("--- build_unit_rain ---")
    if not RAIN_GRID_NC.is_file():
        raise DataError(
            f"missing {RAIN_GRID_NC}; run python -m src.build_rain_grid first"
        )
    weights = load_weights()
    W = weights["W"]
    unit_ids = weights["unit_id"]
    Wt = W.T.tocsr()  # (cells, units) - the orientation the multiply needs

    with xr.open_dataset(RAIN_GRID_NC) as ds:
        if (ds.sizes["lat"], ds.sizes["lon"]) != (weights["lat"].size,
                                                  weights["lon"].size):
            raise DataError(
                f"grid/weights mismatch: grid is {ds.sizes['lat']}x{ds.sizes['lon']}, "
                f"weights were built for {weights['lat'].size}x{weights['lon'].size}; "
                "rebuild with python -m src.build_weights --force"
            )
        times = pd.DatetimeIndex(ds["time"].values)
        years = sorted(set(times.year))
        print(f"  {len(times):,} days over {len(years)} years, "
              f"{len(unit_ids)} units")

        frames: list[pd.DataFrame] = []
        jjas_cell_totals = np.zeros(weights["lat"].size * weights["lon"].size)
        for year in years:
            sel = ds["rain"].sel(time=str(year))
            days = pd.DatetimeIndex(sel["time"].values)
            raw = sel.values.reshape(sel.sizes["time"], -1)
            # (days, cells); NaN -> 0 is safe because W is already 0 on those cells.
            flat = np.nan_to_num(raw, nan=0.0)
            unit_rain = (flat @ Wt).astype(np.float32)  # (days, units)

            # Accumulate the JJAS cell totals here, while the file is already open.
            # Re-opening the NetCDF later deadlocks on Windows HDF5 file locking.
            jjas = np.isin(days.month, [6, 7, 8, 9])
            jjas_cell_totals += flat[jjas].sum(axis=0)

            frames.append(pd.DataFrame({
                "unit_id": np.repeat(unit_ids, len(days)),
                "date": np.tile(days.values, len(unit_ids)),
                "rain_mm": unit_rain.T.reshape(-1),
            }))
            print(f"  {year}: {len(days):>3} days -> "
                  f"{len(days) * len(unit_ids):,} rows")

    table = pd.concat(frames, ignore_index=True)
    del frames

    # Units with no observed cell must be NaN, not a spurious 0.
    no_data = pd.Series(weights["no_valid_cells"], index=unit_ids)
    blank = table["unit_id"].map(no_data).to_numpy()
    table.loc[blank, "rain_mm"] = np.nan
    print(f"  set rain_mm = NaN for {int(no_data.sum())} unit(s) with no observed cell")

    small = pd.Series(weights["small_unit"], index=unit_ids)
    table["small_unit"] = table["unit_id"].map(small).astype(bool)
    table["year"] = table["date"].dt.year.astype("int16")
    table["rain_mm"] = table["rain_mm"].astype("float32")

    require_nonempty(table, "unit rainfall table")
    if not np.isfinite(table["rain_mm"]).any():
        raise DataError("every unit rainfall value is NaN")

    worst = table["rain_mm"].max()
    if worst > IMPLAUSIBLE_DAILY_MM:
        row = table.loc[table["rain_mm"].idxmax()]
        raise DataError(
            f"implausible daily unit rainfall {worst:.1f} mm on {row['date']} for "
            f"{row['unit_id']}; the weight matrix or the grid read is wrong"
        )

    UNIT_RAIN_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    # A partitioned write ADDS files to existing year=* directories rather than
    # replacing them, so a rebuild would silently double every row. Clear first.
    if UNIT_RAIN_PARQUET.is_dir():
        shutil.rmtree(UNIT_RAIN_PARQUET)
    table.to_parquet(UNIT_RAIN_PARQUET, index=False, partition_cols=["year"])
    require_nonempty(UNIT_RAIN_PARQUET, "unit_rain.parquet")

    # Cheap guard against exactly the bug above ever coming back.
    written = pd.read_parquet(UNIT_RAIN_PARQUET, columns=["unit_id", "date"])
    if written.duplicated(["unit_id", "date"]).any():
        raise DataError(
            f"{UNIT_RAIN_PARQUET.name} contains duplicate (unit_id, date) rows; "
            "the partitioned write appended to a previous build"
        )

    # Conservation identity. Rainfall *volume* over the area the units cover must be
    # the same whether it is summed unit-by-unit or cell-by-cell:
    #
    #   sum_u  rain_u * area_u   ==   sum_c  rain_c * coverage_c
    #
    # where area_u is the unit's area over observed cells and coverage_c is how much
    # of cell c any unit covers. Both come from the same raw intersection areas, so
    # this is an exact check on the matrix multiply - not the earlier apples-to-
    # oranges comparison against the whole BBOX, which is dominated by the Thar
    # desert and other states the units do not cover.
    jjas = table[table["date"].dt.month.isin([6, 7, 8, 9])]
    per_unit = jjas.groupby("unit_id", observed=True)["rain_mm"].sum(min_count=1)
    per_unit = per_unit.reindex(unit_ids).to_numpy()
    area_u = weights["unit_area_valid"]
    volume_units = float(np.nansum(per_unit * area_u))
    volume_cells = float(jjas_cell_totals @ weights["cell_coverage"])

    discrepancy = abs(volume_units / volume_cells - 1) if volume_cells else float("inf")
    print(f"\n  conservation check: unit volume {volume_units:.6e} mm*m2 vs "
          f"cell volume {volume_cells:.6e} mm*m2 "
          f"({100 * discrepancy:.4f}% apart)")
    if discrepancy > 1e-6:
        raise DataError(
            f"rainfall volume is not conserved ({100 * discrepancy:.4f}% apart); "
            "the weight matrix and the grid read disagree"
        )

    # Descriptive means, for the report only.
    unit_mean = float(
        jjas.groupby(["unit_id", "year"], observed=True)["rain_mm"].sum()
        .groupby("year").mean().mean()
    )

    units = gpd.read_file(UNITS_GPKG, layer="units", ignore_geometry=True)
    labelled = table.merge(units[["unit_id", "state"]], on="unit_id", how="left")
    print("\n--- mean JJAS total by state (all years) ---")
    state_jjas = (labelled[labelled["date"].dt.month.isin([6, 7, 8, 9])]
                  .groupby(["state", "unit_id", "year"], observed=True)["rain_mm"].sum()
                  .groupby("state").mean().sort_values(ascending=False))
    for state, value in state_jjas.items():
        print(f"  {state:<16}: {value:7.1f} mm")

    summarize(
        "build_unit_rain",
        rows=len(table),
        files=[UNIT_RAIN_PARQUET],
        date_range=(f"{table['date'].min():%Y-%m-%d}", f"{table['date'].max():%Y-%m-%d}"),
        extra={
            "units": len(unit_ids),
            "days": len(times),
            "small_unit rows": int(table["small_unit"].sum()),
            "NaN rows": int(table["rain_mm"].isna().sum()),
            "max daily": f"{worst:.1f} mm",
            "mean JJAS/unit-yr": f"{unit_mean:.1f} mm",
            "volume conserved": f"within {100 * discrepancy:.4f}%",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
