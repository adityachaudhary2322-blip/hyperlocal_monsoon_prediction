"""Outbox: everything that has been sent, plus a phone preview and the send button."""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from app.common import fmt_dt, log, require_login, visible_states
from src.db.models import Advisory, Alert, Message, Subscriber, Unit
from src.db.session import get_session
from src.runtime import sender_is_mock, sender_mock_reason

user = require_login()
st.title("Outbox")

mock = sender_is_mock()
if mock:
    st.info(f":material/smartphone: Messages are written to the database with "
            f"`channel=mock` and never leave this machine ({sender_mock_reason()}).")
else:
    st.error(":material/warning: **Live sending is on.** Pressing send will deliver "
             "real WhatsApp messages through Twilio.")

session = get_session()
try:
    states = visible_states(user)

    approved_query = (session.query(Advisory, Unit)
                      .join(Unit, Advisory.unit_id == Unit.unit_id)
                      .filter(Advisory.status == "approved"))
    if states is not None:
        approved_query = approved_query.filter(Unit.state.in_(states))
    approved = approved_query.all()

    alert_query = session.query(Alert).filter(Alert.status == "approved")
    if states is not None:
        alert_query = alert_query.filter(
            (Alert.target_kind == "district") | (Alert.target_value.in_(states)))
    approved_alerts = alert_query.all()

    message_query = (session.query(Message, Subscriber)
                     .join(Subscriber, Message.subscriber_id == Subscriber.id)
                     .order_by(Message.sent_utc.desc()))
    if states is not None:
        message_query = message_query.filter(Subscriber.state.in_(states))
    messages = message_query.limit(500).all()
finally:
    session.close()

columns = st.columns(4)
columns[0].metric("Approved advisories waiting", len(approved))
columns[1].metric("Approved alerts waiting", len(approved_alerts))
columns[2].metric("Messages sent", len(messages))
columns[3].metric("Channel", "mock" if mock else "whatsapp")

# --------------------------------------------------------------------- send
send_disabled = not (approved or approved_alerts)
if st.button("Send approved now", type="primary", disabled=send_disabled,
             help=("Nothing is approved and waiting." if send_disabled else
                   "Builds one message per consented subscriber in scope.")):
    from src.db.session import session_scope
    from src.sender import send_approved

    with st.spinner("Sending..."):
        with session_scope() as write_session:
            counts = send_approved(write_session, sent_by=user.username,
                                   states=states)
    log(user.username, "send_approved", "messages", "-",
        f"messages={counts['messages']} advisories={counts['advisories']} "
        f"alerts={counts['alerts']} channel={'mock' if mock else 'whatsapp'}")
    st.success(
        f"{counts['messages']} message(s) sent · "
        f"{counts['advisories']} advisor{'y' if counts['advisories'] == 1 else 'ies'} · "
        f"{counts['alerts']} alert(s)."
    )
    if counts["no_subscribers"]:
        st.caption(f"{counts['no_subscribers']} item(s) had no consented subscriber "
                   "in their area and were left approved.")
    st.rerun()

st.divider()

if not messages:
    st.caption("Nothing sent yet. Approve something, then press **Send approved now**.")
    st.stop()

# ------------------------------------------------------------------- listing
left, right = st.columns([3, 2])

with left:
    st.subheader("Sent messages")
    frame = pd.DataFrame([{
        "id": message.id,
        "When": fmt_dt(message.sent_utc),
        "To": subscriber.name,
        "District": subscriber.district,
        "Lang": message.language,
        "Channel": message.channel,
        "Status": message.delivery_status,
        "Preview": (message.body[:60] + "...") if len(message.body) > 60
                   else message.body,
    } for message, subscriber in messages])
    st.dataframe(frame.drop(columns=["id"]), width="stretch",
                 hide_index=True, height=420)

    options = {f"#{m.id} · {s.name} ({m.language})": m.id for m, s in messages}
    chosen_label = st.selectbox("Preview on a phone", list(options))
    chosen_id = options[chosen_label]

# ------------------------------------------------------------- phone preview
with right:
    st.subheader("Farmer phone")
    message, subscriber = next((m, s) for m, s in messages if m.id == chosen_id)

    # Only the last 4 digits: the preview is a demo surface, and a full number on
    # screen is a number that ends up in a screenshot.
    masked = f"•••••{subscriber.phone[-4:]}"
    body = html.escape(message.body).replace("\n", "<br>")
    stamp = message.sent_utc.strftime("%H:%M")
    tick = "✓✓" if "deliver" in (message.delivery_status or "").lower() else "✓"

    st.markdown(
        f"""
<div style="max-width:330px;margin:0 auto;border:11px solid #14161a;
            border-radius:38px;background:#0b141a;padding:0;
            box-shadow:0 10px 30px rgba(0,0,0,.28);">
  <div style="background:#1f2c34;color:#e9edef;padding:12px 14px;
              border-radius:26px 26px 0 0;display:flex;align-items:center;gap:10px;">
    <div style="width:34px;height:34px;border-radius:50%;background:#2a3942;
                display:flex;align-items:center;justify-content:center;
                font-size:15px;">🌾</div>
    <div style="line-height:1.2;">
      <div style="font-weight:600;font-size:0.92rem;">Monsoon Advisory</div>
      <div style="font-size:0.72rem;color:#8696a0;">{masked}</div>
    </div>
  </div>
  <div style="background:#0b141a;min-height:340px;padding:16px 12px;">
    <div style="background:#005c4b;color:#e9edef;padding:9px 11px;
                border-radius:9px 0 9px 9px;margin-left:auto;max-width:86%;
                font-size:0.88rem;line-height:1.45;word-wrap:break-word;">
      {body}
      <div style="text-align:right;font-size:0.66rem;color:#a9c6bd;margin-top:5px;">
        {stamp} {tick}
      </div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    st.caption(
        f"**{subscriber.name}** · {subscriber.district}, {subscriber.state} · "
        f"language `{message.language}` · {len(message.body)} characters"
    )
    st.caption(f"Channel `{message.channel}` · {message.delivery_status}"
               + (f" · {message.error}" if message.error else ""))
