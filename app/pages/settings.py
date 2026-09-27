"""Settings (admins only): the language-layer provider order and the offline switch.

Both changes are written to the audit log (app.common.set_setting). The page is only
registered for roles with the `settings` permission (config/roles.yaml), and checks it
again here.
"""

from __future__ import annotations

import streamlit as st

from app.common import effective_llm_config, require_login, set_setting
from app.permissions import require
from app.theme import page_header

user = require_login()
require(user, "settings")
page_header("Settings", "Which language provider writes advisory text, and the offline "
            "switch. Every change is recorded in the audit log.")

cfg = effective_llm_config()

control_columns = st.columns([3, 2])

with control_columns[0]:
    current = cfg["fallback_order"]
    options = {
        "sarvam → gemini → template": ["sarvam", "gemini", "template"],
        "gemini → sarvam → template": ["gemini", "sarvam", "template"],
        "sarvam → template": ["sarvam", "template"],
        "gemini → template": ["gemini", "template"],
        "template only": ["template"],
    }
    labels = list(options)
    try:
        index = [options[l] for l in labels].index(current)
    except ValueError:
        index = 0
    chosen = st.selectbox("Provider order", labels, index=index)
    if options[chosen] != current:
        if st.button("Save order", type="primary"):
            set_setting("fallback_order", options[chosen], user.username)
            st.success(f"Provider order set to {' → '.join(options[chosen])}.")
            st.rerun()
    st.caption("`template` must stay last - it is the only provider that works "
               "offline and cannot fail.")

with control_columns[1]:
    offline = st.toggle(
        "Offline mode", value=bool(cfg["offline_mode"]),
        help="Forces template text regardless of the provider order. The safe "
             "setting for a live demo.",
    )
    if offline != bool(cfg["offline_mode"]):
        set_setting("offline_mode", offline, user.username)
        st.rerun()
    if offline:
        st.caption("Forced: no provider will be called.")

