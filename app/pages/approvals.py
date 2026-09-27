"""Approval queue: every advisory and custom alert waiting for a human decision.

One card per item, urgent first. Approve is the primary action, Edit opens the text for
changes before approving, Reject asks for a reason (it is the record of why advice was
not sent). The evaluator demo account sees and decides demo copies only (src/demo.py).
"""

from __future__ import annotations

import datetime as dt
import html

import streamlit as st

from app.common import audit, require_login, visible_states
from app.theme import page_header, risk_label
from src import demo
from src.db.models import Advisory, Alert, Unit
from src.db.session import get_session

HAZARD_TEXT = {"heavy": "Heavy rain", "dry": "Dry spell", "onset": "Monsoon onset",
               "withdrawal": "Monsoon withdrawal"}
LANGS = (("en", "English"), ("hi", "हिन्दी"), ("mr", "मराठी"))

user = require_login()
page_header("Approval queue", "Advice the system held back for a person to check. Urgent "
            "items come first. Approving moves an item to the Outbox; nothing is sent from "
            "this page.")


def _decide(item, status: str, reason: str) -> None:
    item.status = status
    item.status_reason = reason
    item.decided_utc = dt.datetime.now(dt.timezone.utc)
    item.decided_by = user.username


def _texts(bodies: dict) -> None:
    tabs = st.tabs([name for _, name in LANGS])
    for tab, (code, _) in zip(tabs, LANGS):
        with tab:
            body = bodies.get(code)
            if body:
                st.html(f"<p class='mo-prose mo-adv-text' lang='{code}'>{html.escape(body)}</p>")
                st.caption(f"{len(body)} / 300 characters")
            else:
                st.caption("Not written in this language yet.")


session = get_session()
try:
    states = visible_states(user)
    query = demo.advisory_scope(
        session.query(Advisory, Unit).join(Unit, Advisory.unit_id == Unit.unit_id)
        .filter(Advisory.status == "pending_approval"), session, user)
    if states is not None:
        query = query.filter(Unit.state.in_(states))
    pending = query.order_by(Advisory.urgent.desc(), Advisory.probability.desc()).all()

    alert_query = demo.alert_scope(
        session.query(Alert).filter(Alert.status == "pending_approval"), user)
    if states is not None:
        alert_query = alert_query.filter(
            (Alert.target_kind == "district") | (Alert.target_value.in_(states)))
    pending_alerts = alert_query.order_by(Alert.created_utc.desc()).all()

    total = len(pending) + len(pending_alerts)
    if total == 0:
        st.html("<div class='mo-empty'><p class='mo-empty-title'>No alerts waiting for review."
                "</p><p>New forecasts arrive every morning.</p></div>")
    else:
        urgent = sum(1 for a, _ in pending if a.urgent)
        st.html(f"<p class='mo-queue-count'><b>{total}</b> waiting"
                + (f" · <span class='mo-urgent-text'>{urgent} urgent</span>" if urgent else "")
                + "</p>")

    # ------------------------------------------------------------- advisories
    for advisory, unit in pending:
        aid = advisory.id
        key = f"card-urgent-{aid}" if advisory.urgent else f"card-{aid}"
        with st.container(border=True, key=key):
            week = max(1, (advisory.horizon or 7) // 7)
            hazard = HAZARD_TEXT.get(advisory.hazard, advisory.hazard or "Advisory")
            place = (unit.unit_name if unit.unit_name == unit.district
                     else f"{unit.unit_name} · {unit.district}")
            chance = "" if advisory.probability is None else f"{advisory.probability:.0%} chance"
            st.html(
                "<div class='mo-card-head'>"
                + ("<span class='mo-urgent-flag'>◆ Urgent</span>" if advisory.urgent else "")
                + f"<h3 class='mo-card-title'>{html.escape(place)}</h3>"
                f"<p class='mo-card-meta'>{html.escape(hazard)}, week {week} · "
                f"{risk_label(advisory.risk_level)} {chance} · "
                f"lead time {advisory.horizon or 7} days · {html.escape(unit.state)}</p>"
                f"<p class='mo-card-why'><b>Why it needs review:</b> "
                f"{html.escape(advisory.status_reason or 'Held by the alert policy')}</p>"
                "</div>")
            _texts({"en": advisory.text_en, "hi": advisory.text_hi, "mr": advisory.text_mr})

            editing, rejecting = f"editing_{aid}", f"rejecting_{aid}"
            if st.session_state.get(editing):
                edited = st.text_area("Edited English text", key=f"edit_{aid}", height=110,
                                      value=advisory.edited_text or advisory.text_en or "")
                with st.container(horizontal=True, gap="small"):
                    if st.button("Save and approve", key=f"save_{aid}", type="primary"):
                        if not edited.strip():
                            st.error("The edited text is empty.")
                        else:
                            advisory.edited_text = edited.strip()
                            advisory.source = "officer"
                            _decide(advisory, "approved", "Approved with an officer edit")
                            session.commit()
                            audit(user, "edit_approve_advisory", "advisory", aid,
                                  f"chars={len(edited.strip())}")
                            st.session_state.pop(editing, None)
                            st.rerun()
                    if st.button("Cancel", key=f"cancel_edit_{aid}", type="tertiary"):
                        st.session_state.pop(editing, None)
                        st.rerun()
            elif st.session_state.get(rejecting):
                reason = st.text_input("Why are you rejecting this?", key=f"reason_{aid}",
                                       placeholder="e.g. rain already fell; advice is out of date")
                with st.container(horizontal=True, gap="small", key=f"reject-row-{aid}"):
                    if st.button("Confirm rejection", key=f"confirm_no_{aid}"):
                        if not reason.strip():
                            st.error("A rejection needs a reason - it is the record of why "
                                     "this advice was not sent.")
                        else:
                            _decide(advisory, "rejected", reason.strip())
                            session.commit()
                            audit(user, "reject_advisory", "advisory", aid, reason.strip())
                            st.session_state.pop(rejecting, None)
                            st.rerun()
                    if st.button("Cancel", key=f"cancel_no_{aid}", type="tertiary"):
                        st.session_state.pop(rejecting, None)
                        st.rerun()
            else:
                with st.container(horizontal=True, gap="small", key=f"actions-{aid}"):
                    if st.button("Approve", key=f"ok_{aid}", type="primary",
                                 icon=":material/check:"):
                        _decide(advisory, "approved", "Approved without changes")
                        session.commit()
                        audit(user, "approve_advisory", "advisory", aid,
                              f"action={advisory.action} unit={advisory.unit_id}")
                        st.rerun()
                    if st.button("Edit", key=f"edit_btn_{aid}", icon=":material/edit:"):
                        st.session_state[editing] = True
                        st.rerun()
                    if st.button("Reject", key=f"no_{aid}", type="tertiary",
                                 icon=":material/close:"):
                        st.session_state[rejecting] = True
                        st.rerun()

    # ----------------------------------------------------------------- alerts
    if pending_alerts:
        st.subheader("Custom alerts")
    for alert in pending_alerts:
        with st.container(border=True, key=f"card-alert-{alert.id}"):
            st.html(
                "<div class='mo-card-head'>"
                f"<h3 class='mo-card-title'>{html.escape(alert.title)}</h3>"
                f"<p class='mo-card-meta'>Custom alert to {html.escape(alert.target_kind)} "
                f"{html.escape(alert.target_value)} · written by "
                f"{html.escape(alert.created_by or '-')}</p>"
                "<p class='mo-card-why'><b>Why it needs review:</b> every custom alert is "
                "checked by a second person.</p></div>")
            _texts({"en": alert.text_en, "hi": alert.text_hi, "mr": alert.text_mr})
            reason = st.text_input("Rejection reason", key=f"alert_reason_{alert.id}",
                                   placeholder="Needed only to reject")
            with st.container(horizontal=True, gap="small", key=f"actions-alert-{alert.id}"):
                if st.button("Approve", key=f"alert_ok_{alert.id}", type="primary",
                             icon=":material/check:"):
                    _decide(alert, "approved", "Approved")
                    session.commit()
                    audit(user, "approve_alert", "alert", alert.id, alert.title)
                    st.rerun()
                if st.button("Reject", key=f"alert_no_{alert.id}", type="tertiary",
                             icon=":material/close:"):
                    if not reason.strip():
                        st.error("A rejection needs a reason.")
                    else:
                        _decide(alert, "rejected", reason.strip())
                        session.commit()
                        audit(user, "reject_alert", "alert", alert.id, reason.strip())
                        st.rerun()
finally:
    session.close()
