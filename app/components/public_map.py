"""The public MapLibre map as an st.components.v2 component.

v2 rather than components.v1.html because v1 re-creates its iframe whenever the HTML
string changes, which would reload the globe and every tile on each week or hazard
change. A v2 component keeps its DOM across reruns and receives new `data` (checked in
this repo against Streamlit 1.64: the JS default export is re-invoked with the same
parent element and no cleanup in between), so the map just repaints.

Only public data goes in `data`: probabilities, approved advisory text, state weather
means. It is assembled in app/pages/public/home.py from app/public_data.py.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _component():
    return st.components.v2.component(
        "monsoon_public_map",
        js=(HERE / "public_map.js").read_text(encoding="utf-8"),
        css=(HERE / "public_map.css").read_text(encoding="utf-8"),
        # Unisolated so the --mo-* theme variables and Mukta reach the map chrome, and
        # MapLibre's own stylesheet (a <link> in <head>) applies to its controls.
        isolate_styles=False,
    )


STATE_KEYS = ("hazard", "week", "weather")


def _ignore() -> None:
    """State callbacks must exist for setStateValue to reach Python; nothing to do."""


def selection(key: str = "mo_map") -> dict:
    """The map panel's current choices, from Session State (empty before first use)."""
    state = st.session_state.get(key) or {}
    return {k: state.get(k) for k in STATE_KEYS if state.get(k) is not None}


def public_map(data: dict, key: str = "mo_map"):
    """Mount the map. `data` must carry the current hazard / week / weather."""
    return _component()(
        data=data, key=key,
        default={k: data[k] for k in STATE_KEYS},
        on_hazard_change=_ignore, on_week_change=_ignore, on_weather_change=_ignore,
    )
