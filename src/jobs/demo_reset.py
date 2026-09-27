"""Nightly reset of the evaluator demo data (config/demo.yaml). Touches demo rows only.

    python -m src.jobs.demo_reset [--db-url-env DATABASE_URL_CLOUD]

1. The demo user exists with role `demo`, the 5 regions, and the password from
   DEMO_PASSWORD (bcrypt-hashed). Refuses if DEMO_USERNAME names a non-demo account.
2. The invented demo subscribers exist (reserved +91 00000... numbers, consented).
3. The demo forecast run exists (created_by = run_mark) and holds exactly the fixed set
   of advisories, every one back to pending with its original text.
4. Demo custom alerts and demo outbox messages older than `message_retention_days` go.

Every delete and update below is filtered on a demo marker; tests/test_demo.py plants
real rows beside demo ones and checks they come through untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt

from src import demo
from src.advisory import crop_phrase, risk_level
from src.common import load_config
from src.report import DataError


def _texts(spec: dict, rules: dict, marathi: dict) -> dict[str, str | None]:
    action = rules["actions"][spec["action"]]
    out = {}
    for lang in ("en", "hi", "mr"):
        template = marathi.get(spec["action"]) if lang == "mr" else action.get(lang)
        out[lang] = (template.replace("{crops}", crop_phrase(spec.get("crops") or [], lang, rules))
                     if template else None)
    return out


def ensure_user(session, cfg: dict, username: str, password: str):
    import bcrypt

    from src.db.models import User

    user = session.get(User, username)
    if user is not None and user.role != cfg["role"]:
        raise DataError(f"DEMO_USERNAME names an existing {user.role} account; "
                        "pick another name - the reset never modifies a real account")
    if user is None:
        user = User(username=username, name="Demo evaluator", role=cfg["role"])
        session.add(user)
    if not user.password_hash or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        user.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user.role = cfg["role"]
    user.assigned_states = "|".join(cfg["states"])
    user.active = True
    return user


def reset(session, *, username: str, password: str, today: dt.date | None = None,
          now: dt.datetime | None = None) -> dict:
    from src.db.models import Advisory, Alert, AuditLog, ForecastRun, Message, Subscriber, Unit

    cfg = demo.cfg()
    rules = load_config("rules")
    policy = {r["id"]: r["reason"] for r in load_config("alert_policy")["hold_for_approval"]}
    today = today or dt.date.today()
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=cfg["message_retention_days"])
    prefix = cfg["subscriber_phone_prefix"]
    counts = {}

    ensure_user(session, cfg, username, password)

    # subscribers -------------------------------------------------------------
    for sub in cfg["subscribers"]:
        if not sub["phone"].startswith(prefix):
            raise DataError(f"demo subscriber {sub['phone']} lacks the demo prefix")
        row = session.query(Subscriber).filter(Subscriber.phone == sub["phone"]).one_or_none()
        if row is None:
            row = Subscriber(phone=sub["phone"])
            session.add(row)
        row.name, row.language = sub["name"], sub["language"]
        row.unit_id, row.district, row.state = sub["unit_id"], sub["district"], sub["state"]
        row.consent, row.do_not_send = True, False
    counts["subscribers"] = len(cfg["subscribers"])

    # the demo run ------------------------------------------------------------
    run = demo.demo_run(session)
    if run is None:
        as_of = dt.date.fromisoformat(cfg["run_as_of"])
        run = ForecastRun(as_of=as_of, requested_as_of=as_of, created_by=cfg["run_mark"],
                          model_choice="demo", pilot_only=True)
        session.add(run)
        session.flush()
    run.n_units = len(cfg["advisories"])

    # outbox: demo messages older than the retention window --------------------
    old = (session.query(Message.id).join(Subscriber, Subscriber.id == Message.subscriber_id)
           .filter(demo.old_demo_messages_clause(cutoff)).all())
    old_ids = [i for (i,) in old]
    if old_ids:
        session.query(Message).filter(Message.id.in_(old_ids)).delete(synchronize_session=False)
    counts["messages_cleared"] = len(old_ids)

    # demo custom alerts older than the window (with their demo messages) --------
    stale_alerts = [a for (a,) in session.query(Alert.id).filter(
        Alert.source == cfg["alert_source"], Alert.created_utc < cutoff).all()]
    if stale_alerts:
        reached_real = (session.query(Message.id)
                        .join(Subscriber, Subscriber.id == Message.subscriber_id)
                        .filter(Message.alert_id.in_(stale_alerts),
                                demo.real_subscriber_clause()).count())
        if reached_real:
            raise DataError("a demo alert reached a real subscriber - refusing to delete")
        session.query(Message).filter(Message.alert_id.in_(stale_alerts)) \
            .delete(synchronize_session=False)
        session.query(Alert).filter(Alert.id.in_(stale_alerts)).delete(synchronize_session=False)
    counts["alerts_cleared"] = len(stale_alerts)

    # advisories: exactly the fixed set, all pending ----------------------------
    wanted = {a["unit_id"]: a for a in cfg["advisories"]}
    missing = sorted(set(wanted) - {u for (u,) in session.query(Unit.unit_id)
                                    .filter(Unit.unit_id.in_(list(wanted))).all()})
    if missing:
        raise DataError(f"demo advisory units not in the units table: {missing}")
    existing = {a.unit_id: a for a in session.query(Advisory).filter(Advisory.run_id == run.id)}
    extra = [a.id for u, a in existing.items() if u not in wanted]
    if extra:
        session.query(Message).filter(Message.advisory_id.in_(extra)).delete(synchronize_session=False)
        session.query(Advisory).filter(Advisory.id.in_(extra)).delete(synchronize_session=False)
    for unit_id, spec in wanted.items():
        row = existing.get(unit_id)
        if row is None:
            row = Advisory(run_id=run.id, unit_id=unit_id)
            session.add(row)
        texts = _texts(spec, rules, cfg.get("marathi") or {})
        action = rules["actions"][spec["action"]]
        row.as_of = today
        row.action, row.stage = spec["action"], None
        row.crops = ", ".join(spec.get("crops") or [])
        row.confidence = spec.get("confidence") or action.get("confidence", "high")
        row.hazard, row.horizon = spec["hazard"], spec["horizon"]
        row.probability = float(spec["probability"])
        row.risk_level = risk_level(row.probability, rules)
        row.status, row.status_reason = "pending_approval", policy[spec["reason"]]
        row.urgent = bool(spec.get("urgent"))
        row.text_en, row.text_hi, row.text_mr = texts["en"], texts["hi"], texts["mr"]
        row.edited_text, row.source = None, "template"
        row.created_utc, row.decided_utc, row.decided_by = now, None, None
    counts["advisories"] = len(wanted)
    if not wanted:
        raise DataError("config/demo.yaml lists no advisories")

    session.add(AuditLog(username="system", action="demo_reset", entity="demo", entity_id="-",
                         detail=demo.tag(", ".join(f"{k}={v}" for k, v in counts.items()))))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url-env", metavar="NAME", default=None)
    args = parser.parse_args(argv)

    from src.config import get as setting
    from src.db.session import create_all, session_scope, use_url

    if args.db_url_env:
        value = setting(args.db_url_env) or ""
        if not value.strip():
            raise DataError(f"{args.db_url_env} is not set")
        use_url(value)
    username, password = setting("DEMO_USERNAME"), setting("DEMO_PASSWORD")
    if not username or not password:
        raise DataError("DEMO_USERNAME and DEMO_PASSWORD must be set")

    create_all()
    with session_scope() as session:
        counts = reset(session, username=username.strip(), password=password)
    print("--- demo reset ---")
    for key, value in counts.items():
        print(f"  {key:18} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
