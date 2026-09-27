"""All-India unit rainfall: national weight matrix, then one year at a time.

Run:  python -m src.build_national_rain [--start 1990] [--force]

The 5-state pipeline goes grid -> NetCDF -> weights -> unit rain. At national size the
NetCDF would hold ~930 MB of float32 in memory (129 x 135 cells x ~13,400 days), which
CLAUDE.md §2 forbids, so this module goes straight from each year's raw .grd file to
that year's unit rainfall:

    for each year:  grid[days, 17,415 cells] @ W.T[17,415, 2,347]  ->  one parquet partition

Peak memory is one year of grid (~25 MB) plus one year of unit rain (~3 MB). The weight
matrix is built once with the same code as the 5-state one (src.build_weights.build:
EPSG:6933 areas, empty cells zeroed before rows are renormalised, §10), over the full
IMD grid. The valid-cell mask comes from the data: a cell is valid if it holds an
observation on any day of a sample of years.

Every year is checked with the exact conservation identity from §10:
sum_u rain_u * area_u == sum_c rain_c * coverage_c.
"""

from __future__ import annotations

import argparse
import shutil

import geopandas as gpd
import numpy as np
import pandas as pd
import scipy.sparse as sp

from src.build_rain_grid import _read_grd, load_realtime_days
from src.build_weights import ROW_SUM_TOL, build
from src.common import (IMD_LAT, IMD_LAT_SIZE, IMD_LON, IMD_LON_SIZE,
                        UNIT_RAIN_INDIA_PARQUET, UNITS_INDIA_GPKG, WEIGHTS_INDIA_NPZ)
from src.fetch_imd import archive_path, expected_archive_bytes, is_leap
from src.report import DataError, print_list, require_nonempty, summarize

N_CELLS = IMD_LAT_SIZE * IMD_LON_SIZE
IMPLAUSIBLE_DAILY_MM = 1000.0
CONSERVATION_TOL = 1e-4          # relative


def archive_years() -> list[int]:
    years = []
    for year in range(1990, 2100):
        path = archive_path(year)
        if not path.is_file():
            if year > 2000:
                break
            continue
        if path.stat().st_size != expected_archive_bytes(year):
            raise DataError(f"{path.name} has the wrong size; re-run src.fetch_imd --force")
        years.append(year)
    return years


def read_year(year: int) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """(days, cells) float32 with NaN for no-data, for an archive or real-time year."""
    path = archive_path(year)
    if path.is_file():
        n = 366 if is_leap(year) else 365
        data = _read_grd(str(path), n)
        days = pd.date_range(f"{year}-01-01", periods=n, freq="D")
        return data.reshape(n, -1), days
    ds, missing = load_realtime_days(year, clip=False)
    if ds is None:
        raise DataError(f"no archive or real-time IMD data for {year}")
    days = pd.DatetimeIndex(ds["time"].values)
    print(f"  {year}: real-time files, {len(days)} days "
          f"({days[0]:%d %b} - {days[-1]:%d %b}), {len(missing)} missing")
    return ds["rain"].values.reshape(len(days), -1), days


def valid_mask(years: list[int]) -> np.ndarray:
    """Cells with an observation on any day of every 5th year (sampled for speed)."""
    mask = np.zeros(N_CELLS, dtype=bool)
    for year in years[::5] + [years[-1]]:
        data, _ = read_year(year)
        mask |= np.isfinite(data).any(axis=0)
    return mask


def build_weights(units: gpd.GeoDataFrame, years: list[int]) -> dict:
    valid = valid_mask(years)
    print(f"  full IMD grid {IMD_LAT_SIZE} x {IMD_LON_SIZE} = {N_CELLS:,} cells, "
          f"{int(valid.sum()):,} with observations")
    W, cells_per_unit, small_unit, area_valid, coverage = build(units, IMD_LAT, IMD_LON, valid)
    require_nonempty(W.data, "national weight matrix")
    totals = np.asarray(W.sum(axis=1)).ravel()
    has_data = cells_per_unit > 0
    bad = np.nonzero(has_data & (np.abs(totals - 1.0) > ROW_SUM_TOL))[0]
    if bad.size:
        raise DataError(f"{bad.size} weight rows do not sum to 1")
    no_data = ~has_data
    if no_data.any():
        print_list("units with NO observed grid cell (rain will be NaN)",
                   [f"{r.state}/{r.district}/{r.unit_name}" for r in units[no_data].itertuples()],
                   limit=12)
    np.savez_compressed(
        WEIGHTS_INDIA_NPZ, data=W.data.astype(np.float64), indices=W.indices,
        indptr=W.indptr, shape=np.array(W.shape),
        unit_id=units["unit_id"].to_numpy().astype("U32"), lat=IMD_LAT, lon=IMD_LON,
        valid_cell=valid, small_unit=small_unit, no_valid_cells=no_data,
        unit_area_valid=area_valid, cell_coverage=coverage)
    require_nonempty(WEIGHTS_INDIA_NPZ, "weights_india.npz")
    print(f"  weights: {W.shape[0]:,} x {W.shape[1]:,}, {W.nnz:,} non-zeros "
          f"({100 * W.nnz / (W.shape[0] * W.shape[1]):.2f}% dense)")
    return load_weights()


def load_weights() -> dict:
    npz = np.load(WEIGHTS_INDIA_NPZ, allow_pickle=False)
    W = sp.csr_matrix((npz["data"], npz["indices"], npz["indptr"]), shape=tuple(npz["shape"]))
    return {"W": W, **{k: npz[k] for k in npz.files if k not in ("data", "indices", "indptr")}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=int, default=1990)
    parser.add_argument("--force", action="store_true", help="rebuild weights and all years")
    args = parser.parse_args()
    print("--- build_national_rain ---")

    if not UNITS_INDIA_GPKG.is_file():
        raise DataError("missing units_india.gpkg; run python -m src.build_units --national")
    units = gpd.read_file(UNITS_INDIA_GPKG, layer="units")
    years = [y for y in archive_years() if y >= args.start]
    if not years:
        raise DataError("no IMD archive years found")
    realtime_year = years[-1] + 1
    print(f"  {len(units):,} units; archive {years[0]}-{years[-1]}, real-time {realtime_year}")

    weights = (build_weights(units, years) if args.force or not WEIGHTS_INDIA_NPZ.is_file()
               else load_weights())
    if list(weights["unit_id"]) != units["unit_id"].tolist():
        raise DataError("weights_india.npz is for a different unit list; rerun with --force")
    Wt = weights["W"].T.tocsr()
    area = weights["unit_area_valid"]
    coverage = weights["cell_coverage"]
    unit_ids = weights["unit_id"]
    no_data = weights["no_valid_cells"]

    if args.force and UNIT_RAIN_INDIA_PARQUET.exists():
        shutil.rmtree(UNIT_RAIN_INDIA_PARQUET)
    UNIT_RAIN_INDIA_PARQUET.mkdir(parents=True, exist_ok=True)

    rows = 0
    worst_err = 0.0
    written = []
    first_day = last_day = None
    for year in years + [realtime_year]:
        part = UNIT_RAIN_INDIA_PARQUET / f"year={year}"
        # The real-time year grows daily, so it is always rebuilt; archive years are
        # idempotent (§16).
        if part.exists() and year != realtime_year and not args.force:
            continue
        try:
            grid, days = read_year(year)
        except DataError as error:
            if year == realtime_year:
                print(f"  {year}: {error} - skipped")
                continue
            raise
        flat = np.nan_to_num(grid, nan=0.0)
        unit_rain = (flat @ Wt).astype(np.float32)               # (days, units)

        # Exact conservation (§10): unit volume == cell volume, per day.
        lhs = unit_rain.astype(np.float64) @ area
        rhs = flat.astype(np.float64) @ coverage
        rel = np.abs(lhs - rhs).max() / max(np.abs(rhs).max(), 1e-9)
        worst_err = max(worst_err, rel)
        if rel > CONSERVATION_TOL:
            raise DataError(f"{year}: conservation broken, relative error {rel:.2e}")

        unit_rain[:, no_data] = np.nan
        if np.nanmax(unit_rain) > IMPLAUSIBLE_DAILY_MM:
            raise DataError(f"{year}: implausible unit rainfall {np.nanmax(unit_rain):.0f} mm")
        table = pd.DataFrame({
            "unit_id": pd.Categorical(np.repeat(unit_ids, len(days))),
            "date": np.tile(days.values, len(unit_ids)),
            "rain_mm": unit_rain.T.reshape(-1),
        })
        require_nonempty(table, f"unit rainfall {year}")
        if part.exists():
            shutil.rmtree(part)
        part.mkdir(parents=True)
        table.to_parquet(part / "part-0.parquet", index=False)
        rows += len(table)
        written.append(part)
        first_day = first_day or days[0]
        last_day = days[-1]
        jjas = float(np.nanmean(unit_rain[np.isin(days.month, [6, 7, 8, 9])].sum(axis=0)))
        print(f"  {year}: {len(days):>3} days, {len(table):>9,} rows, "
              f"mean JJAS {jjas:6.0f} mm, conservation {rel:.1e}")

    if not written and not UNIT_RAIN_INDIA_PARQUET.exists():
        raise DataError("no unit rainfall written")
    summarize("build_national_rain", rows=rows,
              files=[WEIGHTS_INDIA_NPZ, UNIT_RAIN_INDIA_PARQUET],
              date_range=(first_day, last_day) if first_day is not None else None,
              extra={"units": len(unit_ids), "years written": len(written),
                     "worst conservation error": f"{worst_err:.1e}",
                     "units without data": int(no_data.sum())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
