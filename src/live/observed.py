"""Observed daily rainfall per unit, season to date: IMD first, Open-Meteo as fallback.

IMD real-time gridded rainfall (0.25 deg, the same grid as the training archive) is read
with the project's own downloader (src.fetch_imd: byte-exact validation, retries) - not
imdlib, whose get_real_data opens every day in a range and fails if one is absent. IMD
publishes a day's rain the next afternoon IST, so the latest day or two are often not
there yet at 04:30 UTC.

Those days are filled from Open-Meteo's past-days precipitation (0.5 deg), bias-adjusted
to IMD per unit: ratio = (IMD total + 1) / (Open-Meteo total + 1) over the recent days
where both exist, clipped (config/live.yaml). Each value records its source.
"""

from __future__ import annotations

import array
import concurrent.futures as cf
import datetime as dt

import numpy as np

from src.common import IMD_BYTES_PER_DAY, IMD_LAT_SIZE, IMD_LON_SIZE
from src.fetch_imd import _fetch_one_realtime_day, realtime_path
from src.live import assets

SENTINEL = -999.0


def read_imd_day(path) -> np.ndarray:
    """One IMD real-time .grd -> flat (17,415,) float32, NaN for no-data."""
    buf = array.array("f")
    with open(path, "rb") as fh:
        buf.fromfile(fh, IMD_LAT_SIZE * IMD_LON_SIZE)
    grid = np.frombuffer(buf, dtype=np.float32).copy()
    grid[grid == SENTINEL] = np.nan
    return grid


def imd_units(days: list[dt.date], workers: int = 6) -> dict[dt.date, np.ndarray]:
    """{day: (units,) rain} for the days IMD has published; missing days are left out."""
    def fetch(day):
        path = realtime_path(day)
        if not (path.is_file() and path.stat().st_size == IMD_BYTES_PER_DAY):
            _fetch_one_realtime_day(day, force=False)
        return day, path

    w = assets.imd_weights()
    Wt = w["W"].T.tocsr()
    out: dict[dt.date, np.ndarray] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for day, path in pool.map(fetch, days):
            if path.is_file() and path.stat().st_size == IMD_BYTES_PER_DAY:
                grid = read_imd_day(path)
                values = (np.nan_to_num(grid, nan=0.0) @ Wt).astype(np.float32)
                values[w["no_data"]] = np.nan
                out[day] = values
    return out


def bias_ratio(imd: dict[dt.date, np.ndarray], om: dict[dt.date, np.ndarray],
               clip: tuple[float, float]) -> np.ndarray:
    """Per-unit IMD / Open-Meteo ratio over the days both exist (1.0 where no overlap)."""
    common = sorted(set(imd) & set(om))
    n_units = len(next(iter(om.values())))
    if not common:
        return np.ones(n_units, dtype=np.float32)
    i_tot = np.nansum([imd[d] for d in common], axis=0)
    o_tot = np.nansum([om[d] for d in common], axis=0)
    return np.clip((i_tot + 1.0) / (o_tot + 1.0), *clip).astype(np.float32)


def assemble(dates: list[dt.date], stored: dict[dt.date, tuple[np.ndarray, str]],
             imd: dict[dt.date, np.ndarray], om: dict[dt.date, np.ndarray],
             ratio: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """(units, days) observed matrix and a source label per day.

    Precedence per day: fresh IMD > stored value > bias-adjusted Open-Meteo > NaN.
    """
    n_units = len(assets.units())
    obs = np.full((n_units, len(dates)), np.nan, dtype=np.float32)
    sources = []
    for j, day in enumerate(dates):
        if day in imd:
            obs[:, j], src = imd[day], "imd"
        elif day in stored:
            obs[:, j], src = stored[day]
        elif day in om:
            obs[:, j], src = om[day] * ratio, "om_adj"
        else:
            src = "missing"
        sources.append(src)
    return obs, sources
