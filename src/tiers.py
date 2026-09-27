"""Confidence tiers: validated / experimental / low, from config/tiers.yaml.

The stored tier (units table) is the date-independent one. The northeast-monsoon rule
depends on the forecast date, so `tier_on()` applies it at forecast time; callers never
re-implement it.
"""

from __future__ import annotations

import datetime as dt

from src.common import load_config

TIERS = ("validated", "experimental", "low")


def base_tier(state: str, elevation_m: float | None = None) -> str:
    cfg = load_config("tiers")
    if state in cfg["validated"]:
        return "validated"
    low = cfg["low"]
    if state in low["states"]:
        return "low"
    alt = low["high_altitude"]
    if (state in alt["states"] and elevation_m is not None
            and elevation_m >= alt["min_elevation_m"]):
        return "low"
    return "experimental"


def seasonal_low(state: str, district: str) -> bool:
    """True if the unit falls under the Oct-Dec northeast-monsoon rule."""
    cfg = load_config("tiers")["seasonal_low"]
    return state in cfg["states"] or district in (cfg.get("districts") or {}).get(state, [])


def tier_on(stored_tier: str, is_seasonal_low: bool, when: dt.date) -> str:
    """The tier to display for a forecast issued on `when`."""
    months = load_config("tiers")["seasonal_low"]["months"]
    if is_seasonal_low and when.month in months:
        return "low"
    return stored_tier
