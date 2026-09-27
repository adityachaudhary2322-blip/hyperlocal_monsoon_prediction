"""Monsoon advisory control panel.

Start with:  .venv\\Scripts\\streamlit.exe run app/main.py

Runs fully offline. With no API keys and no Twilio credentials it uses template text
and writes mock messages; the banner says so on every page.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit runs this file as a script, not as a package module, so the project root
# has to be importable before anything from src/ is loaded.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.common import boot, mode_banner, require_login, sign_out_button  # noqa: E402

st.set_page_config(
    page_title="Monsoon advisory control panel",
    page_icon=":material/rainy:",
    layout="wide",
)

boot()
user = require_login()
sign_out_button(user)

PAGES = [
    st.Page("pages/overview.py", title="Overview", icon=":material/dashboard:",
            default=True),
    st.Page("pages/risk_map.py", title="Risk Map", icon=":material/map:"),
    st.Page("pages/approvals.py", title="Approval Queue",
            icon=":material/fact_check:"),
    st.Page("pages/custom_alert.py", title="Custom Alert",
            icon=":material/campaign:"),
    st.Page("pages/outbox.py", title="Outbox", icon=":material/outbox:"),
    st.Page("pages/models.py", title="Models", icon=":material/settings:"),
]

navigation = st.navigation(PAGES)
mode_banner()
navigation.run()
