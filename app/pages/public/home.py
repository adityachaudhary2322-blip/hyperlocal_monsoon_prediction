"""Public Map tab: national live forecast on the globe, side panel for the picked area.

Reads the database only through app/public_data.py (tests/test_public_access.py). The
selected area travels in the URL (?unit=...), so a shared link opens that area.
"""

from __future__ import annotations

import datetime as dt
import html

import pandas as pd
import streamlit as st

from app import public_data as pdata
from app import unit_detail
from app.components.public_map import public_map, selection
from app import scenes
from app.i18n import bundle, lang, span, t
from app.theme import page_header, RAIN_RAMP, TEMP_RAMP, footer, mode, tokens
from src.common import regions
from src.tiers import tier_on

HORIZONS = {1: 7, 2: 14, 3: 21, 4: 28}
DEFAULTS = {"hazard": "dry", "week": 1, "weather": "rain24", "unit": ""}
DISPLAY_NAME = {"NCT of Delhi": "Delhi"}


def base_url() -> str:
    try:
        return str(st.context.url or "")
    except Exception:
        return ""


code = lang()
state = selection()
sel = {**DEFAULTS, **state}
if sel["week"] not in HORIZONS:
    sel["week"] = 1
horizon = HORIZONS[sel["week"]]
url_unit = st.query_params.get("unit", "")
if "unit" not in state and url_unit:
    sel["unit"] = url_unit                 # first load from a shared link

page_header(t("home_title"), t("desc_home"),
            forecast_mode=scenes.unit_forecast_mode(sel.get("unit")))
st.html(f"<div class='mo-strip mo-prose' role='note'>{html.escape(t('coverage'))}</div>")

run = pdata.live_run()
units = pdata.live_units() if run else pd.DataFrame()
hazards = run["hazards"] if run else []
if hazards and sel["hazard"] not in hazards:
    sel["hazard"] = hazards[0]

risk: dict = {}
tiers: dict = {}
if run:
    values = pdata.live_map_values(run["id"], horizon)
    for hazard, per_unit in values.items():
        for unit_id, pair in per_unit.items():
            risk.setdefault(unit_id, {})[hazard] = pair
    tiers = {r.unit_id: tier_on(r.tier, bool(r.seasonal_low), run["run_date"])
             for r in units.itertuples()}

detail = None
if run and sel["unit"]:
    try:
        detail = unit_detail.build(sel["unit"], code, base_url())
    except Exception:                        # the panel must never take the page down
        detail = None
    st.query_params["unit"] = sel["unit"]
elif "unit" in st.query_params and state.get("unit") == "":
    del st.query_params["unit"]

weather = {
    r.state_key: {"t": None if pd.isna(r.temp_c) else round(r.temp_c, 1),
                  "r24": None if pd.isna(r.precip_24h_mm) else round(r.precip_24h_mm, 1),
                  "r7": None if pd.isna(r.precip_7d_mm) else round(r.precip_7d_mm, 1),
                  "n": int(r.n_districts)}
    for r in pdata.weather_states().itertuples()
}
window = ""
if run:
    end = run["run_date"] + dt.timedelta(days=horizon - 1)
    window = f"{run['run_date']:%d %b} – {end:%d %b %Y}".replace(" 0", " ").lstrip("0")

public_map({
    **{k: sel[k] for k in ("hazard", "week", "weather")},
    "selected": sel["unit"] or None,
    "dark": mode() == "dark", "tokens": tokens(), "lang": code, "i18n": bundle(code),
    "ramps": {"rain24": RAIN_RAMP[mode()], "rain7": RAIN_RAMP[mode()], "temp": TEMP_RAMP[mode()]},
    "hazards": hazards, "risk": risk, "tiers": tiers, "detail": detail,
    "weatherStates": weather, "windowLabel": window, "anim": pdata.animation_files(),
})

if not run:
    st.info(t("empty_no_run"), icon=":material/schedule:")
else:
    info = pdata.weather_updated()
    stamp = [t("updated", time=unit_detail.fmt_ist(run["created_utc"]))]
    if run["data_delayed"]:
        st.warning(t("data_delayed"), icon=":material/hourglass_top:")
    st.caption(" · ".join(stamp + ([f"{t('weather_now')}: {unit_detail.fmt_ist(info['fetched_utc'])}"]
                                   if info.get("fetched_utc") else [])))

    # One line per validated state, then the experimental rest.
    st.subheader(t("summary_heading", hazard=t(f"hazard_{sel['hazard']}"), span=span(sel["week"])))
    level = {u: r.get(sel["hazard"], [None, None])[1] for u, r in risk.items()}
    frame = units.assign(level=units["unit_id"].map(level))
    lines = []
    for region in regions():
        part = frame[frame["state"] == region]
        high = int((part["level"] == "red").sum())
        name = DISPLAY_NAME.get(region, region)
        lines.append(t("summary_line", state=name, n=high) if high else t("summary_none", state=name))
    rest = frame[~frame["state"].isin(regions())]
    high_rest = int((rest["level"] == "red").sum())
    lines.append(t("summary_line", state=t("other_states"), n=high_rest) if high_rest
                 else t("summary_none", state=t("other_states")))
    st.html("<ul class='mo-summary mo-prose'>" +
            "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>")
    if "ec46_only" in run["sources"].get("blend_basis", []):
        st.caption(t("ml_coming_soon"))
    st.caption(t("source_line"))

footer("Rainfall: India Meteorological Department gridded data. Forecast: ECMWF EC46 via "
       "<a href='https://open-meteo.com/'>Open-Meteo.com</a> (CC BY 4.0). Boundaries: Survey "
       "of India layers via the india-geodata mirror (CC0); indicative, not official.")
