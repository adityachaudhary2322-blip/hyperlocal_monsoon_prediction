"""Approval Queue: every advisory and alert waiting for a human decision."""

from __future__ import annotations

import datetime as dt

import streamlit as st

from app.common import log, require_login, risk_chip, visible_states
from src.db.models import Advisory, Alert, Unit
from src.db.session import get_session

user = require_login()
st.title("Approval Queue")
st.caption("Urgent items first. Nothing is sent from this page - approving moves an "
           "item to the Outbox.")

session = get_session()
try:
    states = visible_states(user)

    query = (session.query(Advisory, Unit)
             .join(Unit, Advisory.unit_id == Unit.unit_id)
             .filter(Advisory.status == "pending_approval"))
    if states is not None:
        query = query.filter(Unit.state.in_(states))
    pending = query.order_by(Advisory.urgent.desc(),
                             Advisory.probability.desc()).all()

    alert_query = session.query(Alert).filter(Alert.status == "pending_approval")
    if states is not None:
        alert_query = alert_query.filter(
            (Alert.target_kind == "district") | (Alert.target_value.in_(states))
        )
    pending_alerts = alert_query.order_by(Alert.created_utc.desc()).all()

    total = len(pending) + len(pending_alerts)
    if total == 0:
        st.success("Nothing waiting for approval.")
    else:
        st.write(f"**{total}** item(s) waiting · "
                 f"{sum(1 for a, _ in pending if a.urgent)} urgent")

    # ------------------------------------------------------------- advisories
    for advisory, unit in pending:
        flag = ":material/priority_high: " if advisory.urgent else ""
        header = (f"{flag}{unit.unit_name}, {unit.district} — {advisory.action} "
                  f"({advisory.hazard} {advisory.horizon}d, "
                  f"{advisory.probability:.0%})")
        with st.expander(header, expanded=advisory.urgent):
            top = st.columns([3, 2])
            top[0].markdown(
                f"{risk_chip(advisory.risk_level)} &nbsp; confidence "
                f"**{advisory.confidence}** &nbsp; source `{advisory.source}`",
                unsafe_allow_html=True,
            )
            top[1].caption(f"{unit.state} · as of {advisory.as_of}")
            st.info(f"**Why this needs review:** {advisory.status_reason}")

            tabs = st.tabs(["English", "हिन्दी", "मराठी"])
            bodies = {"en": advisory.text_en, "hi": advisory.text_hi,
                      "mr": advisory.text_mr}
            for tab, code in zip(tabs, ["en", "hi", "mr"]):
                with tab:
                    if bodies[code]:
                        st.write(bodies[code])
                        st.caption(f"{len(bodies[code])} / 300 characters")
                    else:
                        st.caption("Not translated yet.")

            edited = st.text_area(
                "Edit before approving (optional)",
                value=advisory.edited_text or advisory.text_en or "",
                key=f"edit_{advisory.id}", height=110,
            )
            reason = st.text_input("Rejection reason (required to reject)",
                                   key=f"reason_{advisory.id}")

            buttons = st.columns(3)
            if buttons[0].button("Approve", key=f"ok_{advisory.id}",
                                 type="primary", width="stretch"):
                advisory.status = "approved"
                advisory.status_reason = "Approved without changes"
                advisory.decided_utc = dt.datetime.now(dt.timezone.utc)
                advisory.decided_by = user.username
                session.commit()
                log(user.username, "approve_advisory", "advisory", advisory.id,
                    f"action={advisory.action} unit={advisory.unit_id}")
                st.rerun()

            if buttons[1].button("Edit & Approve", key=f"edit_ok_{advisory.id}",
                                 width="stretch"):
                if not edited.strip():
                    st.error("The edited text is empty.")
                else:
                    advisory.edited_text = edited.strip()
                    advisory.status = "approved"
                    advisory.status_reason = "Approved with an officer edit"
                    advisory.source = "officer"
                    advisory.decided_utc = dt.datetime.now(dt.timezone.utc)
                    advisory.decided_by = user.username
                    session.commit()
                    log(user.username, "edit_approve_advisory", "advisory",
                        advisory.id, f"chars={len(edited.strip())}")
                    st.rerun()

            if buttons[2].button("Reject", key=f"no_{advisory.id}",
                                 width="stretch"):
                if not reason.strip():
                    st.error("A rejection needs a reason - it is the record of why "
                             "this advice was not sent.")
                else:
                    advisory.status = "rejected"
                    advisory.status_reason = reason.strip()
                    advisory.decided_utc = dt.datetime.now(dt.timezone.utc)
                    advisory.decided_by = user.username
                    session.commit()
                    log(user.username, "reject_advisory", "advisory", advisory.id,
                        reason.strip())
                    st.rerun()

    # ----------------------------------------------------------------- alerts
    if pending_alerts:
        st.subheader("Custom alerts")
    for alert in pending_alerts:
        with st.expander(f":material/campaign: {alert.title} "
                         f"→ {alert.target_kind}: {alert.target_value}"):
            st.caption(f"Written by {alert.created_by} · source `{alert.source}`")
            tabs = st.tabs(["English", "हिन्दी", "मराठी"])
            for tab, body in zip(tabs, [alert.text_en, alert.text_hi, alert.text_mr]):
                with tab:
                    st.write(body or "_not provided_")
            reason = st.text_input("Rejection reason",
                                   key=f"alert_reason_{alert.id}")
            buttons = st.columns(2)
            if buttons[0].button("Approve", key=f"alert_ok_{alert.id}",
                                 type="primary", width="stretch"):
                alert.status = "approved"
                alert.decided_utc = dt.datetime.now(dt.timezone.utc)
                alert.decided_by = user.username
                session.commit()
                log(user.username, "approve_alert", "alert", alert.id, alert.title)
                st.rerun()
            if buttons[1].button("Reject", key=f"alert_no_{alert.id}",
                                 width="stretch"):
                if not reason.strip():
                    st.error("A rejection needs a reason.")
                else:
                    alert.status = "rejected"
                    alert.status_reason = reason.strip()
                    alert.decided_utc = dt.datetime.now(dt.timezone.utc)
                    alert.decided_by = user.username
                    session.commit()
                    log(user.username, "reject_alert", "alert", alert.id,
                        reason.strip())
                    st.rerun()
finally:
    session.close()
