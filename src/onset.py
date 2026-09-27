"""Local monsoon onset detection, driven entirely by config/onset.yaml.

No threshold is hardcoded here (CLAUDE.md section 9): every number comes from the
config, resolved per zone with a per-state and then a global default fallback.

Onset is the first day D inside the search window where

  1. rain[D : D + accum_days] sums to at least accum_mm, and
  2. no dry spell begins in the confirmation window that follows the trigger, where a
     dry spell is `dry_spell_days` consecutive days each below `dry_day_mm`.

Criterion 2 is the false-start filter: a burst of rain followed by a long dry break is
not an onset.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.common import load_config


@dataclass(frozen=True)
class OnsetConfig:
    accum_mm: float
    accum_days: int
    dry_spell_days: int
    dry_day_mm: float
    confirm_window_days: int
    search_start: str  # "MM-DD"
    search_end: str    # "MM-DD"

    def search_bounds(self, year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
        return (
            pd.Timestamp(f"{year}-{self.search_start}"),
            pd.Timestamp(f"{year}-{self.search_end}"),
        )


def onset_config(zone_id: str | None = None, state: str | None = None,
                 region: str | None = None) -> OnsetConfig:
    """Resolve the onset rule: zone, then agro-climatic region, then state, then defaults.

    Layers are applied least-specific first, so a zone override beats a region default,
    and an unzoned district still gets a documented rule rather than a guess.
    """
    cfg = load_config("onset")
    resolved = dict(cfg["defaults"])
    for key, value in (("states", state), ("regions", region), ("zones", zone_id)):
        if value and value in (cfg.get(key) or {}):
            resolved.update(cfg[key][value] or {})
    return OnsetConfig(**resolved)


def _dry_spell_starts(rain: np.ndarray, dry_day_mm: float,
                      dry_spell_days: int) -> np.ndarray:
    """Boolean array: True where a run of `dry_spell_days` dry days starts at i.

    A day with missing rainfall is not evidence of dryness, so NaN breaks a run.
    """
    dry = np.isfinite(rain) & (rain < dry_day_mm)
    n = rain.size
    starts = np.zeros(n, dtype=bool)
    if dry_spell_days <= 0 or n < dry_spell_days:
        return starts
    # Rolling all() over a fixed window via a cumulative count of dry days.
    counts = np.concatenate(([0], np.cumsum(dry)))
    window = counts[dry_spell_days:] - counts[:-dry_spell_days]
    starts[: window.size] = window == dry_spell_days
    return starts


def detect_onset(rain: np.ndarray, dates: pd.DatetimeIndex,
                 cfg: OnsetConfig) -> pd.Timestamp | None:
    """First onset date in `dates`, or None if the criteria are never met.

    `rain` and `dates` must cover the search window *plus* accum_days +
    confirm_window_days beyond `search_end`, otherwise a late candidate cannot be
    confirmed and is reported as no onset.
    """
    if rain.size != len(dates):
        raise ValueError(f"rain has {rain.size} values but dates has {len(dates)}")
    if rain.size == 0:
        return None

    year = int(dates[0].year)
    lo, hi = cfg.search_bounds(year)

    # Trigger: rolling accum_days sum, aligned to the window's first day.
    finite = np.nan_to_num(rain, nan=0.0)
    counts = np.concatenate(([0.0], np.cumsum(finite, dtype=np.float64)))
    k = cfg.accum_days
    if rain.size < k:
        return None
    rolling = counts[k:] - counts[:-k]  # rolling[i] = sum(rain[i : i + k])
    triggered = rolling >= cfg.accum_mm

    dry_starts = _dry_spell_starts(rain, cfg.dry_day_mm, cfg.dry_spell_days)

    in_window = (dates >= lo) & (dates <= hi)
    for i in np.nonzero(triggered)[0]:
        if not in_window[i]:
            continue
        # Confirmation window begins the day after the trigger window closes.
        start = i + k
        stop = start + cfg.confirm_window_days
        if stop > rain.size:
            # Not enough data to rule out a false start: refuse to declare onset
            # rather than declare one that cannot be confirmed.
            continue
        if dry_starts[start:stop].any():
            continue
        return dates[i]
    return None


def onset_dates(rain_by_unit_year: pd.DataFrame, cfg_for_unit) -> pd.DataFrame:
    """Onset date per (unit_id, year).

    `rain_by_unit_year` needs columns unit_id, date, rain_mm covering whole years.
    `cfg_for_unit(unit_id)` returns the OnsetConfig for that unit.
    """
    rows = []
    for (unit_id, year), group in rain_by_unit_year.groupby(
        ["unit_id", rain_by_unit_year["date"].dt.year], observed=True
    ):
        group = group.sort_values("date")
        onset = detect_onset(
            group["rain_mm"].to_numpy(dtype="float64"),
            pd.DatetimeIndex(group["date"].values),
            cfg_for_unit(unit_id),
        )
        rows.append({
            "unit_id": unit_id,
            "year": int(year),
            "onset_date": onset,
            "onset_doy": np.nan if onset is None else float(onset.dayofyear),
        })
    return pd.DataFrame(rows)


def first_dry_spell_start(rain: np.ndarray, dates: pd.DatetimeIndex,
                          *, dry_day_mm: float, dry_spell_days: int,
                          window: tuple[pd.Timestamp, pd.Timestamp]
                          ) -> pd.Timestamp | None:
    """First day inside `window` on which a qualifying dry spell begins."""
    starts = _dry_spell_starts(rain, dry_day_mm, dry_spell_days)
    lo, hi = window
    mask = starts & (dates >= lo) & (dates <= hi)
    hits = np.nonzero(mask)[0]
    return dates[hits[0]] if hits.size else None


def forecast_start_dates(year: int, *, first: str = "05-15", last: str = "09-30",
                         step_days: int = 5) -> pd.DatetimeIndex:
    """Forecast issue dates: every `step_days` from `first` to `last` inclusive.

    Anchored on calendar month-day rather than day-of-year, so the same dates recur
    every year and a leap year does not shift the series by one.
    """
    start = dt.date.fromisoformat(f"{year}-{first}")
    end = dt.date.fromisoformat(f"{year}-{last}")
    return pd.date_range(start, end, freq=f"{step_days}D")
