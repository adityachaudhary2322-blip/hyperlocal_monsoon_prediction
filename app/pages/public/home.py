"""Public home: the map, a one-line summary per covered state, and model skill.

Reads the database only through app/public_data.py (tests/test_public_access.py).
"""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app import public_data as pdata
from app.components.public_map import public_map, selection
from app.theme import COVERAGE_TEXT, RAIN_RAMP, TEMP_RAMP, footer, mode, tokens
from src.common import load_config, regions

STATIC = Path(__file__).resolve().parents[2] / "static"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HORIZONS = {1: 7, 2: 14, 3: 21, 4: 28}
HAZARD_PHRASE = {"dry": "at high risk of a dry spell",
                 "heavy": "at high risk of heavy rain",
                 "onset": "with a high chance of monsoon onset"}
HAZARD_NAME = {"onset": "Monsoon onset", "dry": "Dry spell", "heavy": "Heavy rain"}
DISPLAY_NAME = {"NCT of Delhi": "Delhi"}
DEFAULTS = {"hazard": "dry", "week": 1, "weather": "rain24"}


@st.cache_data(show_spinner=False)
def boundary_meta() -> dict:
    return json.loads((STATIC / "boundary_source.json").read_text(encoding="utf-8"))


def fmt_ist(value: dt.datetime | None, with_year: bool = False) -> str:
    if value is None:
        return "not yet available"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    local = value.astimezone(IST)
    return local.strftime(f"%d %b{' %Y' if with_year else ''}, %H:%M IST").lstrip("0")


def window_label(as_of: dt.date, horizon: int) -> str:
    end = as_of + dt.timedelta(days=horizon - 1)
    return f"{as_of:%d %b} – {end:%d %b %Y}".replace(" 0", " ").lstrip("0")


def within(week: int) -> str:
    return "in the next week" if week == 1 else f"within the next {week} weeks"


# --------------------------------------------------------------------------
# Selection (owned by the map's panel; read from Session State)
# --------------------------------------------------------------------------
sel = {**DEFAULTS, **selection()}
if sel["week"] not in HORIZONS:
    sel["week"] = DEFAULTS["week"]
horizon = HORIZONS[sel["week"]]

meta = boundary_meta()
key_of = {region: info["key"] for region, info in meta["covered"].items()}
languages = load_config("project").get("languages", {})
run = pdata.latest_run()
weather_info = pdata.weather_updated()

# --------------------------------------------------------------------------
# Coverage strip - visible near the top, as the brief asks
# --------------------------------------------------------------------------
st.html(f"<div class='mo-strip mo-prose' role='note'>{html.escape(COVERAGE_TEXT)}</div>")

# --------------------------------------------------------------------------
# Map
# --------------------------------------------------------------------------
risk: dict = {}
frame = pd.DataFrame()
if run:
    frame = pdata.forecasts(run["id"], horizon)
    for row in frame.itertuples():
        p = None if pd.isna(row.probability) else round(float(row.probability), 3)
        risk.setdefault(row.unit_id, {})[row.hazard] = [p, row.risk_level]

weather = {
    row.state_key: {"t": None if pd.isna(row.temp_c) else round(row.temp_c, 1),
                    "r24": None if pd.isna(row.precip_24h_mm) else round(row.precip_24h_mm, 1),
                    "r7": None if pd.isna(row.precip_7d_mm) else round(row.precip_7d_mm, 1),
                    "n": int(row.n_districts)}
    for row in pdata.weather_states().itertuples()
}

data = {
    **sel,
    "dark": mode() == "dark",
    "tokens": tokens(),
    "ramps": {"rain24": RAIN_RAMP[mode()], "rain7": RAIN_RAMP[mode()],
              "temp": TEMP_RAMP[mode()]},
    "risk": risk,
    "adv": pdata.approved_advisories(run["id"]) if run else {},
    "weatherStates": weather,
    "langs": {key_of[r]: languages.get(r, ["hi", "en"]) for r in key_of},
    "stateNames": {key_of[r]: DISPLAY_NAME.get(r, r) for r in key_of},
    "updated": fmt_ist(run["created_utc"], with_year=True) if run else "never",
    "issuedFor": f"{run['as_of']:%d %b %Y}".lstrip("0") if run else "—",
    "windowLabel": window_label(run["as_of"], horizon) if run else "No forecast yet",
    "weatherUpdated": fmt_ist(weather_info["fetched_utc"]),
    "anim": pdata.animation_files(),
}
public_map(data)

stamp = [f"Weather updated {fmt_ist(weather_info['fetched_utc'])}"]
if weather_info["latest_failed"] and weather_info["fetched_utc"]:
    stamp.append("the latest update failed, so the last stored values are shown")
if run:
    stamp.append(f"monsoon forecast issued for {run['as_of']:%d %b %Y}".replace(" 0", " "))
st.caption(" · ".join(stamp))

# --------------------------------------------------------------------------
# One line per covered state
# --------------------------------------------------------------------------
st.subheader(f"{HAZARD_NAME[sel['hazard']]}, {within(sel['week'])}")
lines = []
for region in regions():
    name = DISPLAY_NAME.get(region, region)
    total = meta["covered"][region]["units"]
    part = frame[(frame["state"] == region) & (frame["hazard"] == sel["hazard"])] \
        if not frame.empty else frame
    n_fc = len(part)
    if not n_fc:
        lines.append(f"<li><strong>{name}</strong>: no forecast yet.</li>")
        continue
    high = int((part["risk_level"] == "red").sum())
    noun = "sub-district" if high == 1 else "sub-districts"
    lines.append(
        f"<li><strong>{name}</strong>: {high} {noun} {HAZARD_PHRASE[sel['hazard']]} "
        f"{within(sel['week'])} <span class='mo-muted'>({n_fc} of {total} sub-districts "
        f"have forecasts)</span>.</li>")
st.html(f"<ul class='mo-summary mo-prose'>{''.join(lines)}</ul>")
if run:
    st.caption(f"Forecast window {window_label(run['as_of'], horizon)}. Forecasts start with "
               "pilot districts in each state and widen as they are checked.")

# --------------------------------------------------------------------------
# Model skill, in plain words
# --------------------------------------------------------------------------
skill = pdata.model_skill()
if not skill.empty:
    st.subheader("How good are these forecasts?")
    rows = []
    for r in skill.itertuples():
        if r.bss > 0.005:
            verdict = ("✓", "Better", "accent-ink")
        elif r.bss >= -0.005:
            verdict = ("≈", "About the same", "muted")
        else:
            verdict = ("✕", "Not yet better", "risk-high-ink")
        rows.append(
            f"<tr><td>{HAZARD_NAME[r.target]}</td><td>{r.horizon // 7} "
            f"week{'s' if r.horizon > 7 else ''}</td>"
            f"<td><span class='mo-label' style='color:var(--mo-{verdict[2]})'>"
            f"<span aria-hidden='true'>{verdict[0]}</span> {verdict[1]}</span></td>"
            f"<td class='mo-num'>{round(r.auc * 100)} in 100</td>"
            f"<td class='mo-num'>{int(r.n):,} ({int(r.n_pos):,} events)</td></tr>")
    st.html(
        "<div class='mo-table-wrap'><table class='mo-table'><caption class='mo-sr'>"
        "Forecast skill on test years</caption><thead><tr><th>Hazard</th>"
        "<th>Looking ahead</th><th>Compared with the usual-season guess</th>"
        "<th>Ranks a risky week above a calm one</th><th>Cases tested</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>")
    st.caption("Tested on the 2022–2026 monsoons in the pilot districts, which the model "
               "never saw while learning. \"Usual-season guess\" means always forecasting "
               "the long-term average for that time of year.")

footer("Rainfall history: India Meteorological Department gridded data. Weather: "
       "<a href='https://open-meteo.com/'>Open-Meteo.com</a> (CC BY 4.0). Boundaries: "
       "Survey of India layers" + ("" if meta.get("official") else
                                   ", via the india-geodata mirror; indicative, not official")
       + ".")
