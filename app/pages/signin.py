"""Officer sign in. Public visitors never need an account.

Officer pages are only registered with st.navigation once someone is signed in, so a
successful sign-in cannot switch to Overview in this run - it does not exist yet. It sets
a flag and reruns; app/main.py registers the officer pages and then switches.

The demo note shows DEMO_USERNAME / DEMO_PASSWORD from st.secrets or .env (src/config.py);
neither is in the code, and signing in still checks the bcrypt hash in `users`.
"""

from __future__ import annotations

import html

import streamlit as st

from app.common import login_form
from app.theme import footer, page_header
from src.config import get as setting

page_header("Officer sign in", "For agriculture officers who review and approve "
            "advisories. Accounts are created by an administrator. The public outlook "
            "needs no account.")

demo_user, demo_pass = setting("DEMO_USERNAME"), setting("DEMO_PASSWORD")
if demo_user and demo_pass:
    with st.container(key="mo_demo_note", width=420):
        st.html(
            "<div class='mo-demo-note' role='note'>"
            "<p class='mo-demo-title'>Demo access for evaluators</p>"
            f"<p>Username: <code>{html.escape(demo_user)}</code> · "
            f"Password: <code>{html.escape(demo_pass)}</code></p>"
            "<p>This demo account can review, edit and approve alerts. Messages go to a "
            "demo outbox, not real phones. Demo data resets daily.</p></div>")
        if st.button("Fill demo login", icon=":material/input:", key="mo_fill_demo"):
            st.session_state["login_user"] = demo_user
            st.session_state["login_pass"] = demo_pass

if login_form():
    st.session_state["mo_after_login"] = "overview"
    st.rerun()

footer()
