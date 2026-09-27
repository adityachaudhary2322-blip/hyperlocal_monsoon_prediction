"""Send approved advisories and alerts to consented subscribers.

Run:  python -m src.sender [--dry-run] [--sent-by NAME]

Mock mode (no Twilio credentials, or MOCK_MODE=true) writes rows to `messages` with
channel="mock" and delivery_status="delivered (mock)" and never touches the network.
Adding the credentials and setting MOCK_MODE=false switches the same code to Twilio.

Three gates before any message is built, in this order:

1. The advisory must be `approved` - never `pending_approval`, and never `rejected`.
2. The subscriber must have `consent` and not `do_not_send`.
3. The body must pass the same validation the language layer applies, so an
   over-length or off-script message cannot reach a handset.
"""

from __future__ import annotations

import argparse

from src.common import load_config
from src.db.models import Advisory, Alert, AuditLog, Message, Subscriber, Unit
from src.db.session import create_all, session_scope
from src.llm.validation import ValidationError, validate_text
from src.report import summarize
from src.runtime import env, sender_is_mock, sender_mock_reason

MOCK_CHANNEL = "mock"
MOCK_STATUS = "delivered (mock)"


def body_for(subscriber: Subscriber, advisory: Advisory) -> tuple[str | None, str]:
    """The text to send, in the subscriber's language, and which language it is.

    An officer's edit wins over the generated text: that is the point of editing.
    """
    if advisory.edited_text:
        return advisory.edited_text, subscriber.language
    column = {"en": advisory.text_en, "hi": advisory.text_hi,
              "mr": advisory.text_mr}
    text = column.get(subscriber.language)
    if text:
        return text, subscriber.language
    # Fall back to English rather than send nothing, and say which language it is
    # so the Outbox does not claim it is Marathi.
    return advisory.text_en, "en"


def alert_body_for(subscriber: Subscriber, alert: Alert) -> tuple[str | None, str]:
    column = {"en": alert.text_en, "hi": alert.text_hi, "mr": alert.text_mr}
    text = column.get(subscriber.language)
    if text:
        return text, subscriber.language
    return alert.text_en, "en"


def send_twilio(to_phone: str, body: str) -> tuple[str, str | None]:
    """Real send. Only reached when credentials exist and MOCK_MODE is off."""
    from twilio.rest import Client

    client = Client(env("TWILIO_ACCOUNT_SID"), env("TWILIO_AUTH_TOKEN"))
    message = client.messages.create(
        from_=env("TWILIO_WHATSAPP_FROM"),
        to=f"whatsapp:{to_phone}",
        body=body,
    )
    return message.status or "queued", message.sid


def deliver(session, *, subscriber: Subscriber, body: str, language: str,
            advisory_id: int | None = None, alert_id: int | None = None,
            sent_by: str = "system", mock: bool = True) -> Message:
    if mock:
        channel, status, provider_id, error = MOCK_CHANNEL, MOCK_STATUS, None, None
    else:
        try:
            status, provider_id = send_twilio(subscriber.phone, body)
            channel, error = "whatsapp", None
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
            channel, status, provider_id, error = "whatsapp", "failed", None, str(exc)

    message = Message(
        subscriber_id=subscriber.id, advisory_id=advisory_id, alert_id=alert_id,
        language=language, body=body, channel=channel, delivery_status=status,
        provider_message_id=provider_id, error=error, sent_by=sent_by,
    )
    session.add(message)
    return message


def eligible_subscribers(session, unit_id: str | None = None,
                         state: str | None = None, district: str | None = None):
    query = session.query(Subscriber).filter(
        Subscriber.consent.is_(True), Subscriber.do_not_send.is_(False)
    )
    if unit_id:
        query = query.filter(Subscriber.unit_id == unit_id)
    if state:
        query = query.filter(Subscriber.state == state)
    if district:
        query = query.filter(Subscriber.district == district)
    return query.all()


def send_approved(session, *, sent_by: str = "system", states: list[str] | None = None,
                  dry_run: bool = False) -> dict:
    """Send every approved advisory and alert. Returns counts."""
    mock = sender_is_mock()
    rules_cfg = load_config("rules")
    counts = {"advisories": 0, "alerts": 0, "messages": 0, "skipped_no_text": 0,
              "skipped_invalid": 0, "no_subscribers": 0}

    advisories = session.query(Advisory).filter(Advisory.status == "approved").all()
    for advisory in advisories:
        unit = session.get(Unit, advisory.unit_id)
        if unit is None or (states and unit.state not in states):
            continue
        people = eligible_subscribers(session, unit_id=advisory.unit_id)
        if not people:
            counts["no_subscribers"] += 1
            continue

        sent_any = False
        for subscriber in people:
            body, language = body_for(subscriber, advisory)
            if not body:
                counts["skipped_no_text"] += 1
                continue
            keyword = (rules_cfg["actions"].get(advisory.action, {})
                       .get("keywords") or {}).get(language)
            try:
                validate_text(body, language=language, action_keyword=keyword,
                              trusted=(advisory.source != "llm"))
            except ValidationError:
                counts["skipped_invalid"] += 1
                continue
            if not dry_run:
                deliver(session, subscriber=subscriber, body=body, language=language,
                        advisory_id=advisory.id, sent_by=sent_by, mock=mock)
            counts["messages"] += 1
            sent_any = True

        if sent_any and not dry_run:
            advisory.status = "sent"
            counts["advisories"] += 1

    alerts = session.query(Alert).filter(Alert.status == "approved").all()
    for alert in alerts:
        kwargs = ({"state": alert.target_value} if alert.target_kind == "state"
                  else {"district": alert.target_value})
        if states and alert.target_kind == "state" and alert.target_value not in states:
            continue
        people = eligible_subscribers(session, **kwargs)
        if not people:
            counts["no_subscribers"] += 1
            continue
        for subscriber in people:
            body, language = alert_body_for(subscriber, alert)
            if not body:
                counts["skipped_no_text"] += 1
                continue
            if not dry_run:
                deliver(session, subscriber=subscriber, body=body, language=language,
                        alert_id=alert.id, sent_by=sent_by, mock=mock)
            counts["messages"] += 1
        if not dry_run:
            alert.status = "sent"
            counts["alerts"] += 1

    if not dry_run:
        session.add(AuditLog(
            username=sent_by, action="send_approved", entity="messages",
            entity_id="-",
            detail=f"channel={'mock' if mock else 'whatsapp'} "
                   f"messages={counts['messages']} advisories={counts['advisories']} "
                   f"alerts={counts['alerts']}",
        ))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sent-by", default="cli")
    args = parser.parse_args()

    create_all()
    mock = sender_is_mock()
    print("--- sender ---")
    print(f"  mode: {'MOCK - ' + sender_mock_reason() if mock else 'LIVE (Twilio)'}")

    with session_scope() as session:
        counts = send_approved(session, sent_by=args.sent_by, dry_run=args.dry_run)

    summarize(
        "sender",
        rows=counts["messages"],
        files=[],
        extra={
            "channel": MOCK_CHANNEL if mock else "whatsapp",
            "advisories sent": counts["advisories"],
            "alerts sent": counts["alerts"],
            "units with no subscriber": counts["no_subscribers"],
            "skipped (no text)": counts["skipped_no_text"],
            "skipped (failed validation)": counts["skipped_invalid"],
            "dry run": args.dry_run,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
