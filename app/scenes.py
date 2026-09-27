"""Which seasonal scene the header band shows, and what the forecast says it should do.

The drawing itself is in app/components/scene_header.js; everything that decides is here,
from config/scenes.yaml, so it can be tested without a browser.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

import streamlit as st

from src.common import load_config

IST = ZoneInfo("Asia/Kolkata")
LABELS = {"monsoon": "Monsoon", "auto": "Auto (by season)", "harvest": "Harvest",
          "winter": "Himalayan winter", "spring": "Spring", "summer": "Summer",
          "classic": "Classic"}


@lru_cache(maxsize=1)
def cfg() -> dict:
    return load_config("scenes")


def by_date(when: dt.datetime | dt.date | None = None) -> str:
    """The season's scene for a moment, judged in Asia/Kolkata."""
    if when is None:
        when = dt.datetime.now(dt.timezone.utc)
    if isinstance(when, dt.datetime):
        month = (when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)).astimezone(IST).month
    else:
        month = when.month
    for scene, months in cfg()["months"].items():
        if month in months:
            return scene
    return cfg()["default"]


def resolve(choice: str | None, when=None) -> str:
    """The scene actually drawn for a menu choice ("auto" -> by date)."""
    choice = choice or cfg()["default"]
    if choice not in cfg()["choices"]:
        choice = cfg()["default"]
    return by_date(when) if choice == "auto" else choice


def forecast_mode(rain_next_7d: float | None, p_dry_week1: float | None) -> str | None:
    """none / light / heavy rain, or clear (a high dry-spell chance). None = unknown."""
    if p_dry_week1 is not None and p_dry_week1 >= cfg()["dry_clear_from"]:
        return "clear"
    if rain_next_7d is None:
        return None
    mm = cfg()["rain_mm"]
    if rain_next_7d >= mm["heavy_from"]:
        return "heavy"
    if rain_next_7d >= mm["light_from"]:
        return "light"
    return "none"


def unit_forecast_mode(unit_id: str | None) -> str | None:
    """The forecast mode for one sub-district from the latest live run (public data)."""
    if not unit_id:
        return None
    from app import public_data as pdata

    run = pdata.live_run()
    if run is None:
        return None
    rows = pdata.live_unit_rows(run["id"], unit_id)
    fc, wx = rows["forecasts"], rows["weather"]
    p_dry = None
    if len(fc):
        hit = fc[(fc["hazard"] == "dry") & (fc["horizon"] == 7)]
        if len(hit) and hit.iloc[0]["p_blend"] == hit.iloc[0]["p_blend"]:
            p_dry = float(hit.iloc[0]["p_blend"]) / 1000
    rain = wx.get("rain_next_7d")
    rain = None if rain is None or rain != rain else float(rain)
    return forecast_mode(rain, p_dry)


# --------------------------------------------------------------------------
# Viewer state (top bar)
# --------------------------------------------------------------------------
def choice() -> str:
    if "scene" not in st.session_state:
        st.session_state["scene"] = cfg()["default"]
    return st.session_state["scene"]


def effects_on() -> bool:
    if "effects" not in st.session_state:
        st.session_state["effects"] = True
    return bool(st.session_state["effects"])
