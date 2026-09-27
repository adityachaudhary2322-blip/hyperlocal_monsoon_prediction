"""Officer sign in. Public visitors never need an account.

Officer pages are only registered with st.navigation once someone is signed in, so a
successful sign-in cannot switch to Overview in this run - it does not exist yet. It sets
a flag and reruns; app/main.py registers the officer pages and then switches.
"""

from __future__ import annotations

import streamlit as st

from app.common import login_form
from app.theme import footer

st.title("Officer sign in")
st.html("<p class='mo-prose mo-muted'>For agriculture officers who review and approve "
        "advisories. Accounts are created by an administrator. The public outlook needs "
        "no account.</p>")
if login_form():
    st.session_state["mo_after_login"] = "overview"
    st.rerun()

footer()
