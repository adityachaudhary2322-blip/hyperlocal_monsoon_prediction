"""Models: provider order, offline switch, and what the language layer has cost."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.common import (
    effective_llm_config,
    fmt_dt,
    get_setting,
    require_login,
    set_setting,
)
from src.common import load_config
from src.db.models import AuditLog
from src.db.session import get_session
from src.llm import cache as llm_cache
from src.runtime import status as runtime_status

user = require_login()
st.title("Models")

cfg = effective_llm_config()
base = load_config("llm")
state = runtime_status()

# ------------------------------------------------------------------- status
st.subheader("Current state")
columns = st.columns(3)
columns[0].metric("Language layer", "template only" if state["llm_mock"] else "live")
columns[0].caption(state["llm_reason"])
columns[1].metric("Sender", "mock" if state["sender_mock"] else "live")
columns[1].caption(state["sender_reason"])
columns[2].metric("Provider order", " → ".join(cfg["fallback_order"]))
columns[2].caption("Offline mode ON" if cfg["offline_mode"]
                   else "Offline mode off")

with st.expander("Configured providers"):
    rows = []
    for name in ("sarvam", "gemini", "template"):
        settings = base["providers"].get(name, {})
        rows.append({
            "Provider": name,
            "Model": settings.get("translate_model") or settings.get("model") or "-",
            "Mode": settings.get("default_mode", "-"),
            "Key variable": settings.get("api_key_env", "-"),
            "Key present": ("n/a" if name == "template" else
                            ("yes" if not state["llm_mock"] else "no")),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# ------------------------------------------------------------------ controls
st.divider()
st.subheader("Controls")

if user.role != "admin":
    st.caption("Changing the provider order or the offline switch is an admin "
               "action. Both changes are written to the audit log.")
else:
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

# ---------------------------------------------------------------- usage/cost
st.divider()
st.subheader("Usage and cost")

try:
    totals = llm_cache.daily_totals(cfg=base)
except Exception as exc:  # noqa: BLE001 - an empty cache file is normal
    totals = []
    st.caption(f"No call log yet ({exc}).")

if not totals:
    st.caption("No provider calls recorded yet. Template renders are logged too, so "
               "this fills in as soon as a forecast runs.")
else:
    frame = pd.DataFrame(totals)
    frame = frame.rename(columns={
        "day": "Day", "provider": "Provider", "calls": "Calls", "ok": "OK",
        "cache_hits": "Cache hits", "chars": "Characters", "tokens": "Tokens",
        "cost_inr": "Est. cost (INR)", "avg_latency_ms": "Avg latency (ms)",
    })
    st.dataframe(frame, width="stretch", hide_index=True)

    today = frame[frame["Day"] == frame["Day"].max()]
    metrics = st.columns(4)
    metrics[0].metric("Calls today", int(today["Calls"].sum()))
    metrics[1].metric("Cache hits today", int(today["Cache hits"].sum()))
    metrics[2].metric("Est. cost today", f"₹{today['Est. cost (INR)'].sum():.2f}")
    metrics[3].metric("Daily budget", base["rate_limit"]["daily_call_budget"])
    st.caption("Cost is an estimate from the per-1k rates in `config/llm.yaml`, "
               "which are placeholders until the published pricing is pinned. "
               "Cache hits cost nothing.")

# -------------------------------------------------------------------- audit
st.divider()
st.subheader("Recent configuration changes")
session = get_session()
try:
    entries = (session.query(AuditLog)
               .filter(AuditLog.action.in_(["change_setting", "run_forecast",
                                            "send_approved"]))
               .order_by(AuditLog.created_utc.desc()).limit(25).all())
finally:
    session.close()

if entries:
    st.dataframe(pd.DataFrame([{
        "When": fmt_dt(entry.created_utc),
        "Who": entry.username,
        "Action": entry.action,
        "Target": entry.entity_id,
        "Detail": entry.detail,
    } for entry in entries]), width="stretch", hide_index=True)
else:
    st.caption("No configuration changes recorded yet.")

# --------------------------------------------------------------- deployment
st.divider()
st.subheader("Deployment")

from app.common import artifact_state  # noqa: E402
from src.db.session import describe as describe_db  # noqa: E402
from src.runtime import (CHRONOS_CPU_BUDGET_SECONDS, CHRONOS_CPU_SECONDS,  # noqa: E402
                         CHRONOS_OFF_REASON, chronos_enabled)

models_ready, models_detail = artifact_state()

from src.config import hosted  # noqa: E402

on_cloud = hosted()

facts = st.columns(4)
facts[0].metric("Mode", "hosted" if on_cloud else "local",
                help="HOSTED_MODE; hosted reads committed model assets and Postgres")
facts[1].metric("Database", describe_db().split(" · ")[0],
                help=describe_db())
facts[2].metric("Forecast models", "loaded" if models_ready else "missing",
                help=models_detail)
facts[3].metric("Chronos-2", "off" if on_cloud or not chronos_enabled() else "on",
                help=f"ENABLE_CHRONOS; {CHRONOS_CPU_SECONDS}s measured on CPU "
                     f"against a {CHRONOS_CPU_BUDGET_SECONDS}s budget")

if chronos_enabled() and not on_cloud:
    st.info(":material/memory: Chronos-2 is enabled for the online forecast path.")
else:
    st.caption(":material/memory: **Chronos-2 runs on the forecast engine.** "
               "This app forecasts with LightGBM on CPU.")
    with st.expander("Why Chronos-2 is off here"):
        st.write(CHRONOS_OFF_REASON)
        st.caption(
            f"Measured {CHRONOS_CPU_SECONDS} s for one forecast date across the 30 "
            f"pilot units on 2 CPU threads, against the "
            f"{CHRONOS_CPU_BUDGET_SECONDS} s budget - so the cost is not the reason."
        )

st.caption(
    "The hosted site forecasts from the committed assets in `app/assets/models/` "
    "(`scripts/export_models.py` writes them, and the isotonic calibrators travel as "
    "plain JSON so no scikit-learn is needed). Forecast state lives in "
    "`DATABASE_URL`, so a restarted container keeps its history. Settings come from "
    "`st.secrets` when hosted and `.env` locally - same variable names in both "
    "(`src/config.py`)."
)
