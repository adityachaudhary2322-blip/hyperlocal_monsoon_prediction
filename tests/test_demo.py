"""The evaluator demo account: it works on demo copies only, and the nightly reset touches
demo rows only.

Real rows are planted beside demo rows (a real run and advisory, a real subscriber with an
old message, a real custom alert, a real admin) and every check runs both ways: demo
data never reaches the real sender or the public site, and the reset never changes a real
row.
"""

from __future__ import annotations

import datetime as dt

import pytest

REAL_TEXT = "REAL-ADVISORY-MARKER-3e1f"
REAL_PHONE = "+919812345678"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import streamlit as st

    from src import demo
    from src.db import session as session_module
    from src.db.models import (Advisory, Alert, ForecastRun, Message, Subscriber, Unit,
                               User)

    session_module.use_url(f"sqlite:///{tmp_path / 'demo.db'}")
    session_module.create_all()
    cfg = demo.cfg()
    unit_ids = sorted({a["unit_id"] for a in cfg["advisories"]}
                      | {s["unit_id"] for s in cfg["subscribers"]} | {"IND.REAL.1_1"})
    old = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    with session_module.session_scope() as s:
        for u in unit_ids:
            s.add(Unit(unit_id=u, unit_name=u, district="D", state="Maharashtra",
                       unit_type="subdistrict", is_pilot=True))
        s.add(User(username="boss", name="Admin", role="admin", password_hash="x"))
        s.add(ForecastRun(id=1, as_of=dt.date(2026, 9, 26), requested_as_of=dt.date(2026, 9, 26),
                          n_units=1, created_by="pipeline"))
        s.flush()
        s.add(Advisory(id=1, run_id=1, unit_id="IND.REAL.1_1", as_of=dt.date(2026, 9, 26),
                       action="MONITOR", confidence="high", status="approved",
                       text_en=REAL_TEXT, hazard="heavy", horizon=7, probability=0.7,
                       risk_level="red"))
        real = Subscriber(name="Real farmer", phone=REAL_PHONE, district="D",
                          state="Maharashtra", unit_id="IND.REAL.1_1", consent=True)
        s.add(real)
        s.add(Alert(id=1, title="real alert", target_kind="state", target_value="Maharashtra",
                    text_en="real", source="officer", status="approved", created_utc=old))
        s.flush()
        s.add(Message(subscriber_id=real.id, advisory_id=1, language="en", body="old real",
                      sent_utc=old))
    st.cache_data.clear()
    yield session_module
    session_module.use_url(None)
    st.cache_data.clear()


def _reset(db, **kw):
    from src.jobs.demo_reset import reset

    with db.session_scope() as s:
        return reset(s, username="vrrtanta", password="246824", **kw)


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------
def test_reset_restores_twenty_pending_and_is_repeatable(db):
    from src import demo
    from src.db.models import Advisory, User

    first = _reset(db)
    assert first["advisories"] == 20
    with db.session_scope() as s:
        rows = demo.advisory_scope(s.query(Advisory), s, User(role="demo")).all()
        assert len(rows) == 20 and {r.status for r in rows} == {"pending_approval"}
        assert {r.hazard for r in rows} == {"heavy", "dry", "onset"}
        assert all(r.text_en and r.text_hi and r.text_mr for r in rows)
        assert sum(r.urgent for r in rows) >= 3
        for r in rows[:5]:
            r.status = "approved"
    _reset(db)
    with db.session_scope() as s:
        rows = demo.advisory_scope(s.query(Advisory), s, User(role="demo")).all()
        assert len(rows) == 20 and {r.status for r in rows} == {"pending_approval"}


def test_reset_never_touches_real_rows(db):
    from src.db.models import Advisory, Alert, AuditLog, Message, Subscriber, User

    _reset(db, now=dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))
    with db.session_scope() as s:
        real = s.get(Advisory, 1)
        assert real.status == "approved" and real.text_en == REAL_TEXT
        assert s.get(Alert, 1) is not None                       # old, but not a demo alert
        assert s.query(Message).filter(Message.body == "old real").count() == 1
        assert s.query(Subscriber).filter(Subscriber.phone == REAL_PHONE).count() == 1
        assert s.get(User, "boss").role == "admin"
        assert s.get(User, "vrrtanta").role == "demo"
        entry = s.query(AuditLog).filter(AuditLog.action == "demo_reset").one()
        assert entry.detail.startswith("[demo]")


def test_reset_clears_old_demo_messages_and_alerts_only(db):
    from src import demo
    from src.db.models import Alert, Message, Subscriber

    _reset(db)
    old = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    with db.session_scope() as s:
        sub = s.query(Subscriber).filter(demo.demo_subscriber_clause()).first()
        s.add(Message(subscriber_id=sub.id, language="hi", body="old demo", sent_utc=old))
        s.add(Message(subscriber_id=sub.id, language="hi", body="new demo",
                      sent_utc=dt.datetime(2026, 9, 27, 23, tzinfo=dt.timezone.utc)))
        s.add(Alert(title="demo alert", target_kind="state", target_value="Maharashtra",
                    source=demo.cfg()["alert_source"], created_utc=old))
    counts = _reset(db, now=dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))
    assert counts["messages_cleared"] == 1 and counts["alerts_cleared"] == 1
    with db.session_scope() as s:
        bodies = {m.body for m in s.query(Message)}
        assert bodies == {"new demo", "old real"}


def test_reset_refuses_to_take_over_a_real_account(db):
    from src.jobs.demo_reset import reset
    from src.report import DataError

    with db.session_scope() as s, pytest.raises(DataError, match="existing admin"):
        reset(s, username="boss", password="x")


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
def test_real_sender_skips_demo_items_and_demo_subscribers(db, monkeypatch):
    from src import demo, sender
    from src.db.models import Advisory, Message, Subscriber, User

    _reset(db)
    with db.session_scope() as s:
        for a in demo.advisory_scope(s.query(Advisory), s, User(role="demo")):
            a.status = "approved"
    monkeypatch.setattr(sender, "sender_is_mock", lambda: True)
    with db.session_scope() as s:
        counts = sender.send_approved(s, sent_by="boss")
    with db.session_scope() as s:
        demo_msgs = (s.query(Message).join(Subscriber, Subscriber.id == Message.subscriber_id)
                     .filter(demo.demo_subscriber_clause()).count())
        assert demo_msgs == 0
        assert counts["advisories"] == 1                      # only the real one
        demo_status = {a.status for a in demo.advisory_scope(s.query(Advisory), s,
                                                             User(role="demo"))}
        assert demo_status == {"approved"}                    # left for the demo account


def test_demo_send_is_always_mock_and_reaches_only_demo_subscribers(db, monkeypatch):
    from src import demo, sender
    from src.db.models import Advisory, Message, Subscriber, User

    _reset(db)
    with db.session_scope() as s:
        for a in demo.advisory_scope(s.query(Advisory), s, User(role="demo")):
            a.status = "approved"
        s.get(Advisory, 1).status = "approved"
    monkeypatch.setattr(sender, "sender_is_mock", lambda: False)   # Twilio "configured"

    def boom(*a, **k):
        raise AssertionError("the demo account must never call Twilio")

    monkeypatch.setattr(sender, "send_twilio", boom)
    with db.session_scope() as s:
        counts = sender.send_approved(s, sent_by="vrrtanta", demo=True)
    assert counts["messages"] > 0
    with db.session_scope() as s:
        msgs = s.query(Message, Subscriber).join(Subscriber, Subscriber.id == Message.subscriber_id) \
            .filter(Message.sent_by == "vrrtanta").all()
        assert msgs and all(m.channel == "mock" for m, _ in msgs)
        assert all(sub.phone.startswith(demo.cfg()["subscriber_phone_prefix"]) for _, sub in msgs)
        assert s.get(Advisory, 1).status == "approved"       # the real one was not touched


# --------------------------------------------------------------------------
# Public site
# --------------------------------------------------------------------------
def test_public_site_never_shows_the_demo_run(db):
    from app import public_data
    from src import demo
    from src.db.models import Advisory, ForecastRun, User

    _reset(db)
    with db.session_scope() as s:
        run = demo.demo_run(s)
        run.as_of = dt.date(2099, 1, 1)                   # newest by date: must still lose
        for a in demo.advisory_scope(s.query(Advisory), s, User(role="demo")):
            a.status = "approved"
        run_id = run.id
    public_data.latest_run.clear()
    public_data.approved_advisories.clear()
    latest = public_data.latest_run()
    assert latest["id"] == 1
    assert public_data.approved_advisories(run_id) == {}


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
def test_demo_role_permissions():
    from app.permissions import can
    from src.db.models import User

    demo_user, officer, admin = User(role="demo"), User(role="officer"), User(role="admin")
    for perm in ("portal", "approve", "custom_alert", "send"):
        assert can(demo_user, perm)
    for perm in ("settings", "alert_policy", "manage_users", "switch_models", "run_forecast",
                 "real_send", "audit_view"):
        assert not can(demo_user, perm), perm
    assert can(admin, "settings") and not can(officer, "settings")
    assert not can(None, "portal")


def test_demo_credentials_are_not_in_the_code():
    from src.common import ROOT

    for path in [*(ROOT / "app").rglob("*.py"), *(ROOT / "src").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert "246824" not in text, path
