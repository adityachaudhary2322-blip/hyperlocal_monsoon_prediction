"""About: how the outlook is made, in plain language, and who built it."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from app.theme import COVERAGE_TEXT, brand_mark, footer

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
st.html(f"<p class='mo-prose mo-lede'>A 1 to 4 week outlook for the monsoon at "
        f"sub-district level: when it is likely to arrive, and the chance of a dry spell "
        f"or heavy rain. {COVERAGE_TEXT}</p>")

st.header("How it works")
st.html("""
<div class="mo-prose mo-steps">
  <h3>1. Rainfall records</h3>
  <p>We start from the India Meteorological Department's daily rainfall maps, which go
  back to 1990. For every sub-district we work out how much rain fell each day.</p>

  <h3>2. Learning from past monsoons</h3>
  <p>A computer model studies 29 past monsoon seasons, from 1990 to 2018. It learns
  what the weeks before a late onset, a dry spell or a heavy downpour tended to look
  like: recent rain, the time of year, and large ocean patterns such as El Niño. Recent
  years are held back so we can check the model honestly on seasons it has never seen.</p>

  <h3>3. A chance, not a promise</h3>
  <p>Each forecast is a probability, for example "a 64% chance of a dry spell in the
  next two weeks". The map colours areas low (under 30%), medium (30 to 60%) or high
  (over 60%). The model is better than guessing the usual season for some hazards and
  some weeks ahead, not all of them. The Home page shows where.</p>

  <h3>4. A person approves every high-risk message</h3>
  <p>Advice for farmers is written from agricultural contingency plans for each
  district. Before any high-risk advisory is published or sent to farmers, a trained
  agriculture officer reads it, can change it, and must approve it. Only approved
  advice appears on this site.</p>

  <h3>5. Today's weather</h3>
  <p>The national weather layer comes from Open-Meteo. It is refreshed every three hours
  for every district in India. Wind and the 7-day rain animation are refreshed every
  six hours.</p>
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
    boundary_text = ("State and district boundaries are Survey of India layers taken from "
                     "a public mirror (india-geodata). Until the files are replaced with the "
                     "Survey of India's own download, the map says \"Boundaries indicative, "
                     "not official\".")
st.html(f"<p class='mo-prose'>{boundary_text} Sub-districts inside the covered states "
        "come from GADM 4.1 level 3. In Bihar and Delhi these are close to district "
        "size. They will be replaced with block boundaries when those are available.</p>")

st.header("Who built this")
members = "".join(f"<li><strong>{name}</strong>, {role}</li>" for name, role in TEAM["members"])
st.html(f"<div class='mo-prose mo-team'><p class='mo-built'>{brand_mark(24)}"
        f"<strong>{TEAM['name']}</strong></p><ul>{members}</ul>"
        "<p class='mo-muted'>Built for the Smart India Hackathon, on a problem statement "
        "from the Ministry of Earth Sciences, Government of India. This is an independent "
        "prototype, not an official government service.</p></div>")

footer()
