"""VRRTANTA Monsoon outlook: public dashboard plus the officer portal.

Start with:  .venv\\Scripts\\streamlit.exe run app/main.py

One navigation for the whole app (st.navigation, position="top"):

* Public - Home (the map), Next 30 days, Accuracy, About. Always registered. They read
  the database only through app/public_data.py (tests/test_public_access.py).
* Officer portal - Overview, Risk changes, Approval queue, Custom alert, Outbox, Models,
  and Settings for admins. Registered only while someone is signed in, so a signed-out
  visitor has no route to them - typing the URL lands on Home - and each page still
  starts with require_login() as a second guard.

Sign in lives on a hidden page reached from the top bar. Roles: config/roles.yaml;
the evaluator demo account: config/demo.yaml.
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
from app.i18n import t  # noqa: E402
from app.permissions import can  # noqa: E402
from app.theme import apply_theme, footer, logo, top_bar  # noqa: E402

st.set_page_config(
    page_title="VRRTANTA Monsoon outlook",
    page_icon=str(ROOT / "app" / "static" / "brand" / "favicon-32.png"),
    layout="wide",
    initial_sidebar_state="collapsed",
)

boot()
user = current_user()

pages = {
    "home": st.Page("pages/public/home.py", title=t("nav_home"), icon=":material/home:",
                    default=True),
    "outlook": st.Page("pages/public/outlook.py", title=t("tab_30"),
                       icon=":material/date_range:", url_path="next-30-days"),
    "accuracy": st.Page("pages/public/accuracy.py", title=t("tab_accuracy"),
                        icon=":material/fact_check:", url_path="accuracy"),
    "about": st.Page("pages/public/about.py", title=t("tab_about"),
                     icon=":material/info:", url_path="about"),
    "signin": st.Page("pages/signin.py", title="Officer sign in", icon=":material/login:",
                      url_path="sign-in", visibility="hidden"),
    "overview": st.Page("pages/overview.py", title="Overview",
                        icon=":material/dashboard:", url_path="overview"),
    "risk_map": st.Page("pages/risk_map.py", title="Risk changes",
                        icon=":material/trending_up:", url_path="risk-changes"),
    "approvals": st.Page("pages/approvals.py", title="Approval queue",
                         icon=":material/approval:", url_path="approvals"),
    "custom_alert": st.Page("pages/custom_alert.py", title="Custom alert",
                            icon=":material/campaign:", url_path="custom-alert"),
    "outbox": st.Page("pages/outbox.py", title="Outbox", icon=":material/outbox:",
                      url_path="outbox"),
    "models": st.Page("pages/models.py", title="Models", icon=":material/model_training:",
                      url_path="models"),
    "settings": st.Page("pages/settings.py", title="Settings", icon=":material/settings:",
                        url_path="settings"),
}
PUBLIC = ("home", "outlook", "accuracy", "about")
OFFICER = ("overview", "risk_map", "approvals", "custom_alert", "outbox", "models")

public_pages = [pages[k] for k in PUBLIC]
if user is None:
    nav = public_pages + [pages["signin"]]
else:
    officer_pages = [pages[k] for k in OFFICER]
    if can(user, "settings"):
        officer_pages.append(pages["settings"])
    # "" = the ungrouped Public section: its pages stay one click away in the top bar,
    # and the officer pages sit in an "Officer portal" menu beside them. Sign in stays
    # registered (hidden): right after signing in the browser is still on /sign-in, and
    # an unregistered URL shows Streamlit's "Page not found" before the switch.
    nav = {"": public_pages + [pages["signin"]], t("nav_officer"): officer_pages}
current = st.navigation(nav, position="top")

if user is not None and (st.session_state.pop("mo_after_login", None)
                         or current.url_path == "sign-in"):
    st.switch_page(pages["overview"])

is_public = current.url_path in {pages[k].url_path for k in (*PUBLIC, "signin")}
apply_theme(public=is_public)
logo()
top_bar(pages, user)
if not is_public and user is not None:
    mode_banner(user)
current.run()
if not is_public:
    footer()      # public pages place their own, with page-specific credits
