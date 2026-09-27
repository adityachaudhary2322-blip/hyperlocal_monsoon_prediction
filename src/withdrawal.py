"""Local monsoon withdrawal, driven by config/withdrawal.yaml (rainfall-only IMD proxy).

Withdrawal is the first day W in the search window, after onset, where W .. W+dry_days-1
are all below dry_day_mm and the following confirm_window_days hold less than
confirm_max_mm. A candidate the record cannot confirm is not declared (as for onset).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.common import load_config


@dataclass(frozen=True)
class WithdrawalConfig:
    dry_days: int
    dry_day_mm: float
    confirm_window_days: int
    confirm_max_mm: float
    search_start: str
    search_end: str


def withdrawal_config(region: str | None = None, state: str | None = None) -> WithdrawalConfig:
    cfg = load_config("withdrawal")
    resolved = dict(cfg["defaults"])
    for key, value in (("states", state), ("regions", region)):
        if value and value in (cfg.get(key) or {}):
            resolved.update(cfg[key][value] or {})
    return WithdrawalConfig(**resolved)


def detect_withdrawal(rain: np.ndarray, dates: pd.DatetimeIndex, cfg: WithdrawalConfig,
                      onset: pd.Timestamp | None = None) -> pd.Timestamp | None:
    if rain.size != len(dates):
        raise ValueError(f"rain has {rain.size} values but dates has {len(dates)}")
    if rain.size == 0:
        return None
    year = int(dates[0].year)
    lo = pd.Timestamp(f"{year}-{cfg.search_start}")
    hi = pd.Timestamp(f"{year}-{cfg.search_end}")
    if onset is not None and onset > lo:
        lo = onset + pd.Timedelta(days=1)
    dry = np.isfinite(rain) & (rain < cfg.dry_day_mm)
    finite = np.nan_to_num(rain, nan=0.0)
    counts = np.concatenate(([0], np.cumsum(dry)))
    sums = np.concatenate(([0.0], np.cumsum(finite, dtype=np.float64)))
    k, w = cfg.dry_days, cfg.confirm_window_days
    for i in np.nonzero((dates >= lo) & (dates <= hi))[0]:
        stop = i + k + w
        if stop > rain.size:
            break                      # cannot confirm: no withdrawal declared
        if counts[i + k] - counts[i] == k and sums[stop] - sums[i + k] < cfg.confirm_max_mm:
            return dates[i]
    return None
