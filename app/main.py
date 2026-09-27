"""VRRTANTA Monsoon outlook: public dashboard plus the officer portal.

Start with:  .venv\\Scripts\\streamlit.exe run app/main.py

Access:
* Public, no login: Home (the map) and About. They read the database only through
  app/public_data.py - forecasts, approved advisories, national weather, model skill -
  and tests/test_public_access.py checks that by recording every SQL statement.
* Officer portal: Overview, Risk map, Approval queue, Custom alert, Outbox, Models &
  settings. These pages are registered with st.navigation only after sign-in, so a
  signed-out visitor has no route to them, and each page still starts with
  require_login() as a second guard.

Runs fully offline. With no API keys and no Twilio credentials it uses template text and
writes mock messages; officer pages say so on every page.
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

from app.common import boot, current_user, mode_banner  # noqa: E402
from app.theme import apply_theme, footer, officer_subnav, top_bar  # noqa: E402

st.set_page_config(
    page_title="VRRTANTA Monsoon outlook",
    page_icon=str(ROOT / "app" / "static" / "brand" / "favicon-32.png"),
    layout="wide",
    initial_sidebar_state="collapsed",
)

boot()
user = current_user()

pages = {
    "home": st.Page("pages/public/home.py", title="Monsoon outlook", default=True),
    "outlook": st.Page("pages/public/outlook.py", title="Next 30 days", url_path="next-30-days"),
    "accuracy": st.Page("pages/public/accuracy.py", title="Accuracy", url_path="accuracy"),
    "about": st.Page("pages/public/about.py", title="About", url_path="about"),
    "signin": st.Page("pages/signin.py", title="Officer sign in", url_path="sign-in"),
    "overview": st.Page("pages/overview.py", title="Overview",
                        icon=":material/dashboard:", url_path="overview"),
    "risk_map": st.Page("pages/risk_map.py", title="Risk map", icon=":material/map:",
                        url_path="risk-map"),
    "approvals": st.Page("pages/approvals.py", title="Approval queue",
                         icon=":material/fact_check:", url_path="approvals"),
    "custom_alert": st.Page("pages/custom_alert.py", title="Custom alert",
                            icon=":material/campaign:", url_path="custom-alert"),
    "outbox": st.Page("pages/outbox.py", title="Outbox", icon=":material/outbox:",
                      url_path="outbox"),
    "models": st.Page("pages/models.py", title="Models & settings",
                      icon=":material/tune:", url_path="models"),
}
PUBLIC = ("home", "outlook", "accuracy", "about")
OFFICER = ("overview", "risk_map", "approvals", "custom_alert", "outbox", "models")

registered = [pages[k] for k in PUBLIC]
registered += [pages["signin"]] if user is None else [pages[k] for k in OFFICER]
current = st.navigation(registered, position="hidden")

if user is not None and st.session_state.pop("mo_after_login", None):
    st.switch_page(pages["overview"])

is_public = current.url_path in {pages[k].url_path for k in (*PUBLIC, "signin")}
apply_theme(public=is_public)
top_bar(pages, user)
if not is_public and user is not None:
    officer_subnav(pages, user)
    mode_banner()
current.run()
if not is_public:
    footer()      # public pages place their own, with page-specific credits
