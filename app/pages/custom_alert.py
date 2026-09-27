"""Custom Alert: an officer writes a message and sends it to the approval queue."""

from __future__ import annotations

import streamlit as st

from app.common import app_chain, log, require_login, visible_states
from src.db.models import Alert, Unit
from src.db.session import get_session
from src.llm.validation import ValidationError, validate_text
from src.runtime import llm_is_mock, llm_mock_reason

user = require_login()
st.title("Custom Alert")
st.caption("Anything written here still goes through the approval queue - including "
           "your own. Nothing on this page sends a message.")

session = get_session()
try:
    states = visible_states(user)
    unit_query = session.query(Unit)
    if states is not None:
        unit_query = unit_query.filter(Unit.state.in_(states))
    units = unit_query.all()
finally:
    session.close()

all_states = sorted({u.state for u in units})
all_districts = sorted({u.district for u in units})

if not all_states:
    st.warning("No units are in your scope.")
    st.stop()

# ------------------------------------------------------------------ targeting
st.subheader("Who gets it")
columns = st.columns([1, 2])
target_kind = columns[0].radio("Send to", ["state", "district"], horizontal=True)
if target_kind == "state":
    target_value = columns[1].selectbox("State", all_states)
else:
    target_value = columns[1].selectbox("District", all_districts)

# ------------------------------------------------------------------- the text
st.subheader("What it says")
title = st.text_input("Internal title", placeholder="e.g. Cyclone warning, Latur")

text_en = st.text_area(
    "English", height=110,
    placeholder="Heavy rain is expected in your area over the next two days...",
)

live = not llm_is_mock()
translate_columns = st.columns([1, 3])
translate_clicked = translate_columns[0].button(
    "Translate",
    disabled=not live or not text_en.strip(),
    help=("Translates the English text into Hindi and Marathi through the provider "
          "chain." if live else
          f"Needs an API key - currently {llm_mock_reason()}."),
    width="stretch",
)
if live:
    translate_columns[1].caption(
        "Translated text is labelled `source=llm` and still needs approval."
    )
else:
    translate_columns[1].caption(
        f"Offline: {llm_mock_reason()}. Type the Hindi and Marathi yourself."
    )

if translate_clicked:
    chain, _ = app_chain()
    with st.spinner("Translating..."):
        for language, key in (("hi", "alert_hi"), ("mr", "alert_mr")):
            try:
                result = chain.translate(text_en.strip(), language,
                                         source_language="en")
                st.session_state[key] = result.text
                st.session_state.setdefault("alert_source", "llm")
                st.toast(f"{language}: {result.provider} "
                         f"({result.latency_ms or 0:.0f} ms)")
            except Exception as exc:  # noqa: BLE001 - surfaced to the officer
                st.error(f"{language}: {exc}")
    log(user.username, "translate_alert", "alert", "-",
        f"target={target_kind}:{target_value}")

text_hi = st.text_area("हिन्दी (Hindi)", height=110,
                       key="alert_hi", placeholder="...")
text_mr = st.text_area("मराठी (Marathi)", height=110,
                       key="alert_mr", placeholder="...")

for label, body, language in (("English", text_en, "en"),
                              ("Hindi", text_hi, "hi"),
                              ("Marathi", text_mr, "mr")):
    if body and body.strip():
        try:
            validate_text(body.strip(), language=language, trusted=True)
            st.caption(f":material/check: {label}: {len(body.strip())}/300 characters")
        except ValidationError as exc:
            st.warning(f"{label}: {exc}")

# ------------------------------------------------------------------- submit
st.divider()
if st.button("Submit for approval", type="primary",
             disabled=not (title.strip() and text_en.strip())):
    session = get_session()
    try:
        alert = Alert(
            title=title.strip(), target_kind=target_kind, target_value=target_value,
            text_en=text_en.strip() or None,
            text_hi=(text_hi or "").strip() or None,
            text_mr=(text_mr or "").strip() or None,
            source=st.session_state.get("alert_source", "officer"),
            status="pending_approval",
            status_reason="Officer-written alert - all custom alerts are reviewed",
            created_by=user.username,
        )
        session.add(alert)
        session.commit()
        alert_id = alert.id
    finally:
        session.close()

    log(user.username, "create_alert", "alert", alert_id,
        f"{target_kind}={target_value} title={title.strip()}")
    st.success(f"Alert #{alert_id} sent to the Approval Queue.")
    for key in ("alert_hi", "alert_mr", "alert_source"):
        st.session_state.pop(key, None)
