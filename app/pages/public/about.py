"""About: how the outlook is made, in plain language, and who built it."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

import html

from app.i18n import t
from app.theme import brand_mark, footer

STATIC = Path(__file__).resolve().parents[2] / "static"

# Filled in by the team. Kept as data so the page never shows half-edited markup.
TEAM = {
    "name": "VRRTANTA",
    "members": [
        ("[Name]", "[Role]"),
        ("[Name]", "[Role]"),
        ("[Name]", "[Role]"),
    ],
}

try:
    boundary = json.loads((STATIC / "boundary_source.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    boundary = {"official": False}

st.title("About this outlook")
st.html(f"<p class='mo-prose mo-lede'>A 1 to 4 week outlook for the monsoon for every "
        f"sub-district of India: when it is likely to arrive or withdraw, and the chance of "
        f"a dry spell or heavy rain. {html.escape(t('coverage'))}</p>")

st.header("How it works")
st.html("""
<div class="mo-prose mo-steps">
  <h3>1. Rainfall records</h3>
  <p>We start from the India Meteorological Department's daily rainfall maps, which go
  back to 1990, and work out how much rain fell each day in every sub-district of India.
  Every morning the latest IMD day is added.</p>

  <h3>2. Today's forecast, every day</h3>
  <p>Each morning our system reads the European weather centre's 46-day forecast (ECMWF
  EC46). It is not one forecast but 51 slightly different ones. If 33 of the 51 show a
  dry spell starting next week, we say there is a 65% chance. The same rules decide
  what counts as a dry spell, heavy rain or the monsoon's arrival everywhere.</p>

  <h3>3. Learning from past monsoons</h3>
  <p>For the five validated states, a computer model has also learned from 29 past
  monsoon seasons (1990 to 2018) and was checked on the seasons since. A national
  version of this model is coming soon; when it is ready, the two forecasts will be
  combined.</p>

  <h3>4. How much to trust it</h3>
  <p>Every area carries a label. <b>Validated</b>: checked against past seasons in this
  state. <b>Experimental</b>: the same method, not yet checked here. <b>Low
  confidence</b>: places where the method is known to be weak, such as the hills of the
  northeast, the high Himalaya, and Tamil Nadu and coastal Andhra during the October to
  December rains. The Accuracy tab shows how the forecasts have done.</p>

  <h3>5. What it means for crops</h3>
  <p>For the main crops of each area we work out the likely growth stage from the
  Agriculture Ministry's crop calendar, and check which weather matters at that stage:
  a dry spell at flowering, heavy rain at harvest, waterlogging, heat. This is general
  guidance; outside the validated states it is marked as not yet checked.</p>

  <h3>6. A person approves every high-risk message</h3>
  <p>Before any high-risk advisory is published or sent to farmers, a trained agriculture
  officer reads it, can change it, and must approve it. Only approved advice appears on
  this site.</p>
</div>
""")

st.header("Languages")
st.html("<p class='mo-prose'>Advisories are written in Hindi (हिन्दी) for Uttar Pradesh, "
        "Delhi, Bihar and Madhya Pradesh, in Marathi (मराठी) for Maharashtra, and in "
        "English for every state.</p>")

st.header("Maps and boundaries")
if boundary.get("official"):
    boundary_text = ("State and district boundaries are drawn from the Survey of India's "
                     "digital vector data.")
else:
    boundary_text = ("State, district and sub-district boundaries are Survey of India layers "
                     "taken from a public mirror (india-geodata, CC0). Until they are replaced "
                     "with the Survey of India's own download, the map says \"Boundaries "
                     "indicative, not official\".")
st.html(f"<p class='mo-prose'>{boundary_text} Forecasts are calculated on GADM level-3 "
        "units; each sub-district on the map shows the forecast of the unit that covers most "
        "of it. Search also finds blocks, from the Local Government Directory.</p>")

st.header("Who built this")
members = "".join(f"<li><strong>{name}</strong>, {role}</li>" for name, role in TEAM["members"])
st.html(f"<div class='mo-prose mo-team'><p class='mo-built'>{brand_mark(24)}"
        f"<strong>{TEAM['name']}</strong></p><ul>{members}</ul>"
        "<p class='mo-muted'>Built for the Smart India Hackathon, on a problem statement "
        "from the Ministry of Earth Sciences, Government of India. This is an independent "
        "prototype, not an official government service.</p></div>")

footer()
