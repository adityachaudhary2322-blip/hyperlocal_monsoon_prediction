"""The public pages show forecasts, approved advisories, national weather and model skill
- and nothing else.

Three independent checks, because each alone can be fooled:

1. **Static.** The public modules may import only the allowed ORM models and may not
   run raw SQL. Catches a future `from src.db.models import Subscriber` at review time.
2. **Recorded SQL.** Home and About are run with streamlit.testing against a seeded
   SQLite database. Every statement is captured with a `before_cursor_execute` listener;
   the tables it touches must be a subset of the allowed set, and every `advisories`
   query must filter on status.
3. **Rendered output.** Marker strings are planted in a pending, a rejected and an
   approved advisory, a subscriber's phone, a user and the audit log. Only the approved
   marker may reach the page or the map component's data.

Plus the navigation rule: signed out, only Home, About and Sign in are registered.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from src.common import ROOT

PUBLIC_MODULES = [
    ROOT / "app" / "public_data.py",
    ROOT / "app" / "components" / "public_map.py",
    ROOT / "app" / "unit_detail.py",
    ROOT / "app" / "scenes.py",
    ROOT / "app" / "components" / "scene_header.py",
    *sorted((ROOT / "app" / "pages" / "public").glob("*.py")),
]
ALLOWED_MODELS = {"Unit", "ForecastRun", "Forecast", "Advisory", "WeatherNow",
                  "WeatherState", "WeatherFetch", "WeatherGrid", "LiveRun", "LiveUnit",
                  "LiveForecast", "LiveOutlook", "LiveWeather", "LiveVerification"}
ALLOWED_TABLES = {"units", "forecast_runs", "forecasts", "advisories", "weather_now",
                  "weather_state", "weather_fetch", "weather_grid", "live_runs", "live_units",
                  "live_forecasts", "live_outlook", "live_weather", "live_verification"}
FORBIDDEN_TABLES = {"subscribers", "messages", "users", "audit_log", "alerts", "settings",
                    "forecast_changes", "obs_unit_rain"}

APPROVED = "APPROVED-MARKER-7f3a"
PENDING = "PENDING-MARKER-91c2"
REJECTED = "REJECTED-MARKER-44d8"
PHONE = "+919000012345"
USER_NAME = "USER-MARKER-5b1e"
AUDIT = "AUDIT-MARKER-0c9d"


# ==========================================================================
# 1. Static
# ==========================================================================
@pytest.mark.parametrize("path", PUBLIC_MODULES, ids=lambda p: p.name)
def test_public_modules_import_only_allowed_models(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.db.models":
            names = {a.name for a in node.names}
            assert names <= ALLOWED_MODELS, f"{path.name} imports {names - ALLOWED_MODELS}"
        if isinstance(node, ast.ImportFrom) and node.module == "app.common":
            names = {a.name for a in node.names}
            # app.common reads users/audit/settings; public pages may not touch it.
            assert not names & {"units_frame", "log", "get_setting", "authenticate",
                                "scope_query"}, f"{path.name} imports {names}"


@pytest.mark.parametrize("path", PUBLIC_MODULES, ids=lambda p: p.name)
def test_public_modules_run_no_raw_sql(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\btext\(\s*[\"']", text), f"raw SQL text() in {path.name}"
    assert ".execute(" not in text, f"direct execute() in {path.name}"


def test_only_public_data_touches_the_database():
    """Pages go through app/public_data.py; they never open a session themselves."""
    for path in PUBLIC_MODULES:
        if path.name == "public_data.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "get_session" not in text and "session_scope" not in text, path.name


# ==========================================================================
# 2 + 3. Recorded SQL and rendered output
# ==========================================================================
@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    import streamlit as st

    from src.db import session as session_module
    from src.db.models import (Advisory, AuditLog, Forecast, ForecastRun, LiveForecast,
                               LiveOutlook, LiveRun, LiveUnit, LiveWeather, Subscriber, Unit,
                               User, WeatherFetch, WeatherNow, WeatherState)

    db = tmp_path / "public.db"
    session_module.use_url(f"sqlite:///{db}")
    session_module.create_all()
    now = dt.datetime.now(dt.timezone.utc)
    with session_module.session_scope() as s:
        units = [("IND.20.16.2_1", "Ausa", "Latur"), ("IND.20.6.2_1", "Ashti", "Bid"),
                 ("IND.20.6.3_1", "Jamkhed", "Bid")]
        s.add_all(Unit(unit_id=u, unit_name=n, district=d, state="Maharashtra",
                       unit_type="subdistrict", is_pilot=True) for u, n, d in units)
        s.add(ForecastRun(id=1, as_of=dt.date(2024, 6, 19), requested_as_of=dt.date(2024, 6, 19),
                          n_units=3))
        s.flush()
        for u, _, _ in units:
            for hz in ("onset", "dry", "heavy"):
                s.add(Forecast(run_id=1, unit_id=u, hazard=hz, horizon=7, probability=0.7,
                               risk_level="red", model="lightgbm"))
        for (u, _, _), status, marker in zip(units, ("approved", "pending_approval",
                                                     "rejected"),
                                             (APPROVED, PENDING, REJECTED)):
            s.add(Advisory(run_id=1, unit_id=u, as_of=dt.date(2024, 6, 19), action="X",
                           confidence="low", status=status, status_reason=f"why {marker}",
                           text_en=marker, text_hi=marker, decided_by=f"officer {marker}"))
        s.add(Subscriber(name="Farmer", phone=PHONE, district="Latur", state="Maharashtra",
                         consent=True))
        s.add(User(username="u1", name=USER_NAME, password_hash="x"))
        s.add(AuditLog(username="u1", action="login", detail=AUDIT))
        s.add(WeatherNow(state="Maharashtra", district="Latur", state_key="maharashtra",
                         lat=18.4, lon=76.6, valid_utc=now, fetched_utc=now, temp_c=29.0,
                         precip_24h_mm=3.0, precip_7d_mm=20.0))
        s.add(WeatherState(state_key="maharashtra", state="Maharashtra", n_districts=1,
                           fetched_utc=now, temp_c=29.0, precip_24h_mm=3.0, precip_7d_mm=20.0))
        s.add(WeatherFetch(started_utc=now, ok=True, n_districts=1, n_expected=1))
        live = LiveRun(run_date=dt.date(2024, 6, 20), run_type="live", season="monsoon",
                       hazards="dry,heavy", sources='{"blend_basis": ["ec46_only"]}', n_units=3)
        s.add(live)
        s.flush()
        for u, n, d in units:
            s.add(LiveUnit(unit_id=u, unit_name=n, name_ok=True, district=d, state="Maharashtra",
                           tier="validated", seasonal_low=False, lat=18.4, lon=76.6))
            for hz in ("dry", "heavy"):
                for h in (7, 14, 21, 28):
                    s.add(LiveForecast(run_id=live.id, unit_id=u, hazard=hz, horizon=h,
                                       p_ec46=700, p_blend=700, level="red"))
            for w in (1, 2, 3, 4):
                s.add(LiveOutlook(run_id=live.id, unit_id=u, week=w, p10=1, p50=5, p90=20,
                                  normal=12, p_below=500, p_near=300, p_above=200))
            s.add(LiveWeather(run_id=live.id, unit_id=u, rain_7d=10, rain_14d=30, normal_7d=12,
                              normal_14d=25, temp_c=29, rh_pct=80, obs_source="imd"))

    statements: list[str] = []
    from sqlalchemy import event

    engine = session_module.engine()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    st.cache_data.clear()

    captured: dict = {}
    import app.components.public_map as component

    monkeypatch.setattr(component, "public_map",
                        lambda data, key="mo_map": captured.setdefault("data", data))
    yield statements, captured
    event.remove(engine, "before_cursor_execute", record)
    session_module.use_url(None)
    st.cache_data.clear()


def _tables(sql: str) -> set[str]:
    return {m.lower() for m in re.findall(r'(?:FROM|JOIN|INTO|UPDATE)\s+"?(\w+)"?', sql, re.I)}


def _run_public(page: str | None, unit: str | None = None):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=120)
    if unit:
        at.query_params["unit"] = unit
    at.run()
    if page:
        at.switch_page(page).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _page_text(at) -> str:
    parts = []
    for kind in ("markdown", "caption", "title", "header", "subheader", "text"):
        parts += [str(getattr(e, "value", "")) for e in getattr(at, kind)]
    parts += [str(getattr(e, "proto", "")) for e in at.get("html")]
    return "\n".join(parts)


@pytest.mark.parametrize("page", [None, "pages/public/about.py", "pages/public/outlook.py",
                                  "pages/public/accuracy.py"],
                         ids=["home", "about", "next30", "accuracy"])
def test_public_pages_touch_only_allowed_tables(seeded_db, page):
    statements, _ = seeded_db
    statements.clear()
    _run_public(page)
    # create_all's PRAGMA/DDL checks run at boot; judge only data statements.
    data_sql = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    touched = set().union(*(_tables(s) for s in data_sql)) if data_sql else set()
    if page is None:
        # Not vacuous: Home really reads forecasts and advisories through this path.
        assert {"live_forecasts", "live_runs", "weather_state"} <= touched, touched
    assert touched <= ALLOWED_TABLES, f"public page read {touched - ALLOWED_TABLES}"
    assert not touched & FORBIDDEN_TABLES
    for sql in data_sql:
        if "advisories" in _tables(sql):
            assert re.search(r"advisories\.status\s+IN", sql, re.I), \
                f"advisories read without a status filter:\n{sql}"


def test_home_shows_only_approved_advisory_text(seeded_db):
    """Open the approved area, then the pending and rejected ones, via ?unit=."""
    _, captured = seeded_db
    blob = ""
    for unit in ("IND.20.16.2_1", "IND.20.6.2_1", "IND.20.6.3_1"):
        captured.clear()
        at = _run_public(None, unit=unit)
        blob += _page_text(at) + json.dumps(captured.get("data", {}), ensure_ascii=False)
    assert APPROVED in blob, "the approved advisory should reach the map panel"
    for secret in (PENDING, REJECTED, PHONE, USER_NAME, AUDIT, f"officer {APPROVED}",
                   f"why {APPROVED}"):
        assert secret not in blob, f"{secret} leaked to the public page"


# ==========================================================================
# Navigation
# ==========================================================================
def test_signed_out_navigation_registers_only_public_pages(seeded_db, monkeypatch):
    import streamlit as st

    registered: list = []
    original = st.navigation

    def spy(pages, **kwargs):
        registered[:] = [p.url_path for p in pages]
        return original(pages, **kwargs)

    monkeypatch.setattr(st, "navigation", spy)
    _run_public(None)
    assert set(registered) == {"", "next-30-days", "accuracy", "about", "sign-in"}, registered


def test_an_old_advisory_is_never_shown_as_current(seeded_db, monkeypatch):
    """A back-test advisory (2024) must not appear beside a 2026 forecast."""
    import datetime as _dt

    from app import public_data as pdata

    assert pdata.approved_advisories_by_unit(_dt.date(2024, 6, 25))          # current
    assert pdata.approved_advisories_by_unit(_dt.date(2026, 9, 27)) == {}    # stale


def test_live_pages_survive_missing_live_tables(monkeypatch):
    """Right after a deploy, before src.live.run has created its tables, the public
    pages must see "no run yet" rather than a database error."""
    from sqlalchemy.exc import ProgrammingError

    from app import public_data

    def missing(statement):
        raise ProgrammingError("SELECT ...", {}, Exception('relation "live_runs" does not exist'))

    monkeypatch.setattr(public_data, "_frame", missing)
    public_data.live_run.clear()
    public_data.live_verification_frame.clear()
    try:
        assert public_data.live_run() is None
        assert public_data.live_verification_frame().empty
    finally:
        public_data.live_run.clear()
        public_data.live_verification_frame.clear()
