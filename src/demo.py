"""The evaluator demo account: what counts as demo data, and the filters that keep it apart.

Every rule lives in config/demo.yaml. Three markers, one per table the demo can write:

* advisories  -> on the forecast run whose `created_by` is `run_mark`
* alerts      -> `source == alert_source`
* subscribers -> phone starts with `subscriber_phone_prefix`

Real officer views, the public site and the real sender call the `real_*` filters; the
demo account's views call the `demo_*` ones. A test plants real rows next to demo rows and
checks both directions (tests/test_demo.py).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import and_, not_, or_

from src.common import load_config
from src.db.models import Advisory, Alert, ForecastRun, Message, Subscriber


@lru_cache(maxsize=1)
def cfg() -> dict:
    return load_config("demo")


def is_demo(user) -> bool:
    return user is not None and getattr(user, "role", None) == cfg()["role"]


def tag(text: str | None) -> str:
    """Audit detail for a demo action: always starts with the demo tag."""
    t = cfg()["audit_tag"]
    return t if not text else f"{t} {text}"


# --------------------------------------------------------------------------
# Forecast runs and advisories
# --------------------------------------------------------------------------
def real_run_clause():
    mark = cfg()["run_mark"]
    return or_(ForecastRun.created_by.is_(None), ForecastRun.created_by != mark)


def latest_real_run(session) -> ForecastRun | None:
    return (session.query(ForecastRun).filter(real_run_clause())
            .order_by(ForecastRun.as_of.desc(), ForecastRun.id.desc()).first())


def demo_run(session) -> ForecastRun | None:
    return (session.query(ForecastRun).filter(ForecastRun.created_by == cfg()["run_mark"])
            .order_by(ForecastRun.id.desc()).first())


def demo_run_ids(session) -> list[int]:
    return [r for (r,) in session.query(ForecastRun.id)
            .filter(ForecastRun.created_by == cfg()["run_mark"]).all()]


def advisory_scope(query, session, user):
    """Demo sees only demo-run advisories; everyone else never sees them."""
    ids = demo_run_ids(session)
    if is_demo(user):
        return query.filter(Advisory.run_id.in_(ids or [-1]))
    return query.filter(Advisory.run_id.notin_(ids)) if ids else query


def advisory_run(session, user) -> ForecastRun | None:
    """The run whose advisories a user works on: the demo run, or the latest real one."""
    return demo_run(session) if is_demo(user) else latest_real_run(session)


# --------------------------------------------------------------------------
# Alerts, subscribers, messages
# --------------------------------------------------------------------------
def alert_scope(query, user):
    src = cfg()["alert_source"]
    if is_demo(user):
        return query.filter(Alert.source == src)
    return query.filter(or_(Alert.source.is_(None), Alert.source != src))


def demo_subscriber_clause():
    return Subscriber.phone.like(cfg()["subscriber_phone_prefix"] + "%")


def subscriber_scope(query, user):
    """Apply to any query that already joins Subscriber."""
    clause = demo_subscriber_clause()
    return query.filter(clause if is_demo(user) else not_(clause))


def real_subscriber_clause():
    return not_(demo_subscriber_clause())


def old_demo_messages_clause(cutoff):
    """Demo outbox messages older than `cutoff` (joined to Subscriber)."""
    return and_(demo_subscriber_clause(), Message.sent_utc < cutoff)
