"""Hazard events, defined once (config/labels.yaml) for the live engine and for training.

Every function takes daily rainfall with days on the LAST axis - shape (..., days), e.g.
(units, days) for one observed series or (members, units, days) for the EC46 ensemble -
plus `s`, the index of the first forecast day (today), and `h`, the horizon in days. The
window is days s .. s+h-1, the same timing convention as training (CLAUDE.md §12).

Return: a float array (...,) of 1.0 / 0.0 per series, or NaN where the event cannot be
judged (e.g. onset that already happened). Averaging over the member axis gives the
EC46 probability; that is all src.live.probabilities does.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from src.common import load_config
from src.onset import OnsetConfig, detect_onset
from src.withdrawal import WithdrawalConfig, detect_withdrawal


def cfg() -> dict:
    return load_config("labels")


def _window_counts(flags: np.ndarray, width: int) -> np.ndarray:
    """count[..., t] = flags[..., t:t+width].sum(); length days-width+1."""
    c = np.concatenate([np.zeros(flags.shape[:-1] + (1,)), np.cumsum(flags, axis=-1)], axis=-1)
    return c[..., width:] - c[..., :-width]


def dry_spell(rain: np.ndarray, s: int, h: int) -> np.ndarray:
    """A run of >= run_days days each < dry_mm that STARTS inside the window."""
    c = cfg()["dry"]
    k = c["run_days"]
    dry = np.isfinite(rain) & (rain < c["dry_mm"])
    counts = _window_counts(dry.astype(np.int32), k)          # run starting at t
    last_start = min(s + h, counts.shape[-1])
    if last_start <= s:
        return np.full(rain.shape[:-1], np.nan)
    return (counts[..., s:last_start] == k).any(axis=-1).astype(float)


def heavy_rain(rain: np.ndarray, s: int, h: int, day_mm: float | None = None) -> np.ndarray:
    threshold = cfg()["heavy"]["day_mm"] if day_mm is None else day_mm
    window = rain[..., s:s + h]
    return (np.nan_to_num(window, nan=0.0) >= threshold).any(axis=-1).astype(float)


def late_heavy_rain(rain: np.ndarray, s: int, h: int, harvest_mask: np.ndarray) -> np.ndarray:
    """Heavy rain on a day that lies in a main crop's harvest window.

    `harvest_mask` is (units, days) or broadcastable: True on harvest days per unit.
    Units with no harvest day in the window get NaN (nothing to damage).
    """
    threshold = cfg()["late_heavy"]["day_mm"]
    window = np.nan_to_num(rain[..., s:s + h], nan=0.0)
    mask = harvest_mask[..., s:s + h]
    out = ((window >= threshold) & mask).any(axis=-1).astype(float)
    return np.where(mask.any(axis=-1), out, np.nan)


def rabi_moisture_short(rain: np.ndarray, s: int, h: int, season_start_idx: int,
                        normal_total: np.ndarray) -> np.ndarray:
    """Season rain so far + window rain < fraction_of_normal x normal (per unit)."""
    frac = cfg()["rabi_moisture"]["fraction_of_normal"]
    total = np.nansum(rain[..., season_start_idx:s + h], axis=-1)
    return np.where(np.isfinite(normal_total), (total < frac * normal_total).astype(float),
                    np.nan)


def onset_in_window(rain: np.ndarray, dates: pd.DatetimeIndex, s: int, h: int,
                    config: OnsetConfig) -> float:
    """1.0 if onset falls in the window, 0.0 if not, NaN if it already happened.

    Onset "already happened" is the KNOWABLE version (§12): its confirmation window has
    closed before day s. For the forecast the false-start check uses the days that exist
    (at least onset.min_confirm_days, config/labels.yaml) - EC46 is 46 days long.
    """
    past = detect_onset(rain[:s], dates[:s], config)
    if past is not None:
        return np.nan
    min_confirm = cfg()["onset"]["min_confirm_days"]
    usable = min(config.confirm_window_days,
                 max(min_confirm, rain.size - (s + h) - config.accum_days))
    relaxed = OnsetConfig(**{**config.__dict__, "confirm_window_days": usable})
    onset = detect_onset(rain, dates, relaxed)
    if onset is None:
        return 0.0
    last = dates[min(s + h, len(dates)) - 1]
    return float(dates[s] <= onset <= last)


def onset_forecast(rain: np.ndarray, dates: pd.DatetimeIndex, s: int,
                   config: OnsetConfig) -> tuple[bool, pd.Timestamp | None]:
    """(already happened by day s, forecast onset date or None) - one detector call.

    Same rule as onset_in_window: "already" is the knowable onset (confirmed before s);
    the forecast relaxes the 30-day confirmation to the days the series has.
    """
    if detect_onset(rain[:s], dates[:s], config) is not None:
        return True, None
    min_confirm = cfg()["onset"]["min_confirm_days"]
    usable = min(config.confirm_window_days,
                 max(min_confirm, rain.size - s - config.accum_days))
    relaxed = OnsetConfig(**{**config.__dict__, "confirm_window_days": usable})
    return False, detect_onset(rain, dates, relaxed)


def withdrawal_in_window(rain: np.ndarray, dates: pd.DatetimeIndex, s: int, h: int,
                         config: WithdrawalConfig, onset: pd.Timestamp | None) -> float:
    """1.0 if withdrawal falls in the window; NaN before onset or after withdrawal."""
    if onset is None:
        return np.nan
    if detect_withdrawal(rain[:s], dates[:s], config, onset) is not None:
        return np.nan
    got = detect_withdrawal(rain, dates, config, onset)
    if got is None:
        return 0.0
    last = dates[min(s + h, len(dates)) - 1]
    return float(dates[s] <= got <= last)


def harvest_days(dates: pd.DatetimeIndex, windows: list[tuple[str, str]]) -> np.ndarray:
    """Boolean (days,) True inside any of the MM-DD windows (wrapping the year end)."""
    mmdd = np.array([f"{d:%m-%d}" for d in dates])
    mask = np.zeros(len(dates), dtype=bool)
    for start, end in windows:
        if start <= end:
            mask |= (mmdd >= start) & (mmdd <= end)
        else:
            mask |= (mmdd >= start) | (mmdd <= end)
    return mask


def season_start_index(dates: pd.DatetimeIndex, today: dt.date) -> int:
    start = pd.Timestamp(f"{today.year}-{cfg()['rabi_moisture']['season_start']}")
    return int(np.searchsorted(dates.values, start.to_datetime64()))
