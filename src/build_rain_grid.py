"""Clip the raw IMD rainfall grids to the project BBOX and write one float32 NetCDF.

Run:  python -m src.build_rain_grid [--start 1990] [--force]

Memory discipline (CLAUDE.md section 2)
---------------------------------------
imdlib.open_data allocates a float64 array for the whole requested span, so asking it
for 1990-2025 in one call would need ~1.8 GB of RAM before any clipping. This module
therefore opens exactly one year at a time, clips to the BBOX first, casts to float32,
and only then accumulates: ~6 MB per year in, ~215 MB for the full series out.

Realtime days are read one file at a time because imdlib.get_real_data opens every day
in the requested range and raises if a single file is absent - and the current year is
expected to be ragged at the tail.
"""

from __future__ import annotations

import argparse
import array
import datetime as dt

import numpy as np
import pandas as pd
import xarray as xr

from src.common import (
    IMD_LAT,
    IMD_LAT_SIZE,
    IMD_LON,
    IMD_LON_SIZE,
    IMD_RAW,
    RAIN_GRID_NC,
    bbox,
)
from src.fetch_imd import archive_path, expected_archive_bytes, is_leap, realtime_path
from src.report import DataError, require_nonempty, summarize

SENTINEL = -999.0
VAR = "rain"


def _slice_bbox(ds: xr.Dataset) -> xr.Dataset:
    bb = bbox()
    return ds.sel(
        lat=slice(bb["lat_min"], bb["lat_max"]),
        lon=slice(bb["lon_min"], bb["lon_max"]),
    )


def _read_grd(path: str, n_days: int) -> np.ndarray:
    """Read a raw IMD .grd into (days, lat, lon) float32 with NaN for the sentinel.

    The on-disk layout is (day, lat, lon) C-order float32 - same as imdlib.core reads,
    but kept as float32 throughout instead of being promoted to float64.
    """
    buf = array.array("f")
    with open(path, "rb") as fh:
        buf.fromfile(fh, n_days * IMD_LAT_SIZE * IMD_LON_SIZE)
    data = np.frombuffer(buf, dtype=np.float32).reshape(
        n_days, IMD_LAT_SIZE, IMD_LON_SIZE
    )
    data = data.copy()  # frombuffer is read-only
    data[data == SENTINEL] = np.nan
    return data


def _to_dataset(data: np.ndarray, times: pd.DatetimeIndex) -> xr.Dataset:
    ds = xr.Dataset(
        {VAR: (("time", "lat", "lon"), data)},
        coords={"time": times, "lat": IMD_LAT, "lon": IMD_LON},
    )
    return _slice_bbox(ds)


def load_archive_year(year: int) -> xr.Dataset | None:
    path = archive_path(year)
    expected = expected_archive_bytes(year)
    if not path.is_file():
        return None
    if path.stat().st_size != expected:
        raise DataError(
            f"{path.name} is {path.stat().st_size:,} B, expected {expected:,} B; "
            "re-run python -m src.fetch_imd --force"
        )
    n_days = 366 if is_leap(year) else 365
    data = _read_grd(str(path), n_days)
    times = pd.date_range(f"{year}-01-01", periods=n_days, freq="D")
    return _to_dataset(data, times)


def load_realtime_days(year: int) -> tuple[xr.Dataset | None, list[dt.date]]:
    """Read whatever realtime days exist for `year`, skipping absent ones."""
    day = dt.date(year, 1, 1)
    today = dt.date.today()
    chunks: list[np.ndarray] = []
    days: list[dt.date] = []
    missing: list[dt.date] = []
    while day <= today:
        path = realtime_path(day)
        if path.is_file() and path.stat().st_size == IMD_LAT_SIZE * IMD_LON_SIZE * 4:
            chunks.append(_read_grd(str(path), 1))
            days.append(day)
        else:
            missing.append(day)
        day += dt.timedelta(days=1)
    if not chunks:
        return None, missing
    times = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    return _to_dataset(np.concatenate(chunks, axis=0), times), missing


def check_time_axis(ds: xr.Dataset) -> list[str]:
    """Assert the time axis is strictly increasing daily; report any gaps."""
    times = pd.DatetimeIndex(ds["time"].values)
    if not times.is_monotonic_increasing:
        raise DataError("time axis is not strictly increasing")
    if times.has_duplicates:
        dupes = times[times.duplicated()].strftime("%Y-%m-%d").tolist()
        raise DataError(f"time axis has duplicate days: {dupes[:5]}")
    steps = np.diff(times.values).astype("timedelta64[D]").astype(int)
    gap_at = np.nonzero(steps != 1)[0]
    return [
        f"{times[i]:%Y-%m-%d} -> {times[i + 1]:%Y-%m-%d} ({steps[i]} days)"
        for i in gap_at
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1990)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-realtime", action="store_true",
                        help="archive years only; skip the current-year realtime days "
                             "(use while the realtime backfill is still running, so "
                             "the record does not appear to stop mid-January)")
    args = parser.parse_args()

    if RAIN_GRID_NC.is_file() and not args.force:
        print(f"{RAIN_GRID_NC} exists; pass --force to rebuild")
        return 0

    print("--- build_rain_grid ---")
    bb = bbox()
    print(f"  clipping to lat {bb['lat_min']}-{bb['lat_max']}, "
          f"lon {bb['lon_min']}-{bb['lon_max']} before anything else")

    parts: list[xr.Dataset] = []
    years: list[int] = []
    this_year = dt.date.today().year
    for year in range(args.start, this_year + 1):
        ds = load_archive_year(year)
        if ds is None:
            continue
        parts.append(ds)
        years.append(year)
        print(f"  {year}: {ds.sizes['time']:>3} days, "
              f"{ds.sizes['lat']}x{ds.sizes['lon']} cells, {ds[VAR].dtype}")

    if not parts:
        raise DataError(
            f"no archive .grd files found under {IMD_RAW / 'rain'}; "
            "run python -m src.fetch_imd first"
        )

    rt_missing: list[dt.date] = []
    rt_days = 0
    if args.no_realtime:
        print("  skipping realtime days (--no-realtime)")
    elif years and years[-1] < this_year:
        rt, rt_missing = load_realtime_days(years[-1] + 1)
        if rt is not None:
            rt_days = rt.sizes["time"]
            parts.append(rt)
            print(f"  {years[-1] + 1}: {rt_days} realtime days "
                  f"({len(rt_missing)} absent)")
        else:
            print(f"  {years[-1] + 1}: no realtime days on disk")

    print("  concatenating...")
    grid = xr.concat(parts, dim="time")
    del parts
    grid[VAR] = grid[VAR].astype("float32")
    require_nonempty(grid[VAR], "clipped rainfall grid")

    gaps = check_time_axis(grid)
    if gaps:
        print(f"  time-axis gaps ({len(gaps)}):")
        for gap in gaps[:10]:
            print(f"      - {gap}")

    grid[VAR].attrs = {
        "units": "mm",
        "long_name": "daily accumulated rainfall",
        "source": "India Meteorological Department 0.25 degree gridded rainfall",
    }
    grid.attrs = {
        "title": "IMD daily rainfall clipped to the monsoon-ai project BBOX",
        "bbox": f"lat {bb['lat_min']}-{bb['lat_max']}, lon {bb['lon_min']}-{bb['lon_max']}",
        "archive_years": f"{years[0]}-{years[-1]}",
        "realtime_days": rt_days,
        "created": dt.date.today().isoformat(),
        "Conventions": "CF-1.7",
    }

    RAIN_GRID_NC.parent.mkdir(parents=True, exist_ok=True)
    encoding = {VAR: {"zlib": True, "complevel": 4, "dtype": "float32",
                      "_FillValue": np.float32(np.nan)}}
    grid.to_netcdf(RAIN_GRID_NC, encoding=encoding)
    require_nonempty(RAIN_GRID_NC, "imd_rain.nc")

    times = pd.DatetimeIndex(grid["time"].values)
    valid = np.isfinite(grid[VAR].isel(time=0).values)
    summarize(
        "build_rain_grid",
        rows=int(grid.sizes["time"]),
        files=[RAIN_GRID_NC],
        date_range=(f"{times[0]:%Y-%m-%d}", f"{times[-1]:%Y-%m-%d}"),
        extra={
            "grid": f"{grid.sizes['lat']} lat x {grid.sizes['lon']} lon "
                    f"= {grid.sizes['lat'] * grid.sizes['lon']} cells",
            "dtype": str(grid[VAR].dtype),
            "archive years": f"{len(years)} ({years[0]}-{years[-1]})",
            "realtime days": rt_days,
            "missing rt days": len(rt_missing),
            "time gaps": len(gaps),
            "cells with data": f"{int(valid.sum())}/{valid.size} "
                               f"({100 * valid.mean():.1f}% land)",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
