"""Outbox: everything that has been sent, plus a phone preview and the send button."""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from app.common import audit, require_login, visible_states
from app.theme import page_header
from src import demo
from src.db.models import Advisory, Alert, Message, Subscriber, Unit
from src.db.session import get_session
from src.runtime import sender_is_mock, sender_mock_reason

user = require_login()
page_header("Outbox", "Approved advice waiting to go out, everything already sent, and how "
            "each message looks on a farmer's phone.")

is_demo = demo.is_demo(user)
mock = True if is_demo else sender_is_mock()
if is_demo:
    st.info(":material/smartphone: This is the demo outbox: sending writes demo messages "
            "to invented demo subscribers. Nothing reaches a real phone.")
elif mock:
    st.info(f":material/smartphone: Messages are written to the database with "
            f"`channel=mock` and never leave this machine ({sender_mock_reason()}).")
else:
    st.error(":material/warning: **Live sending is on.** Pressing send will deliver "
             "real WhatsApp messages through Twilio.")



def mask_phone(phone: str | None) -> str:
    """+91 ••••••4321 - the full number never reaches the screen (or a screenshot)."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"+91 ••••••{digits[-4:]}" if len(digits) >= 4 else "••••"


session = get_session()
try:
    states = visible_states(user)

    approved_query = demo.advisory_scope(
        session.query(Advisory, Unit).join(Unit, Advisory.unit_id == Unit.unit_id)
        .filter(Advisory.status == "approved"), session, user)
    if states is not None:
        approved_query = approved_query.filter(Unit.state.in_(states))
    approved = approved_query.all()

    alert_query = demo.alert_scope(session.query(Alert).filter(Alert.status == "approved"), user)
    if states is not None:
        alert_query = alert_query.filter(
            (Alert.target_kind == "district") | (Alert.target_value.in_(states)))
    approved_alerts = alert_query.all()

    message_query = demo.subscriber_scope(
        session.query(Message, Subscriber).join(Subscriber, Message.subscriber_id == Subscriber.id)
        .order_by(Message.sent_utc.desc()), user)
    if states is not None:
        message_query = message_query.filter(Subscriber.state.in_(states))
    messages = message_query.limit(500).all()
finally:
    session.close()

columns = st.columns(4)
columns[0].metric("Approved advisories waiting", len(approved))
columns[1].metric("Approved alerts waiting", len(approved_alerts))
columns[2].metric("Messages sent", len(messages))
columns[3].metric("Channel", "demo outbox" if is_demo else ("mock" if mock else "whatsapp"))

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
                                   states=states, demo=is_demo)
    audit(user, "send_approved", "messages", "-",
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
    st.html("<div class='mo-empty'><p class='mo-empty-title'>Nothing sent yet.</p>"
            "<p>Approve advice in the Approval queue, then press <b>Send approved now</b>.</p>"
            "</div>")
    st.stop()

# ------------------------------------------------------------------- listing
left, right = st.columns([3, 2])

with left:
    st.subheader("Sent messages")
    frame = pd.DataFrame([{
        "When": message.sent_utc,
        "To": subscriber.name,
        "Phone": mask_phone(subscriber.phone),
        "District": subscriber.district,
        "Language": {"en": "English", "hi": "हिन्दी", "mr": "मराठी"}.get(message.language,
                                                                     message.language),
        "Channel": "demo outbox" if is_demo else message.channel,
        "Status": ("✕ failed" if message.error else "✓ " + (message.delivery_status or "sent")),
        "Message": message.body,
    } for message, subscriber in messages])
    st.dataframe(frame, width="stretch", hide_index=True, height=420, column_config={
        "When": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm", timezone="UTC"),
        "Phone": st.column_config.TextColumn(help="Only the last 4 digits are shown"),
        "Status": st.column_config.TextColumn(width="small"),
        "Message": st.column_config.TextColumn(width="large"),
    })

    options = {f"#{m.id} · {s.name} ({m.language})": m.id for m, s in messages}
    chosen_label = st.selectbox("Preview on a phone", list(options))
    chosen_id = options[chosen_label]

# ------------------------------------------------------------- phone preview
with right:
    st.subheader("Farmer phone")
    message, subscriber = next((m, s) for m, s in messages if m.id == chosen_id)

    # Only the last 4 digits: the preview is a demo surface, and a full number on
    # screen is a number that ends up in a screenshot.
    masked = mask_phone(subscriber.phone)
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
