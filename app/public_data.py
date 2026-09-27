"""Everything the public pages may read. Nothing else in app/pages/public/ touches the DB.

The rule (tests/test_public_access.py enforces it both by reading this file and by
recording every SQL statement a public page run issues):

* **Allowed tables:** units, forecast_runs, forecasts, advisories (approved or sent
  only), weather_now, weather_state, weather_fetch, weather_grid.
* **Never:** subscribers, messages, users, audit_log, alerts, settings,
  forecast_changes - and never an advisory that is pending or rejected, nor who decided
  it or why.

An advisory an officer edited is shown as the edited text only: `edited_text` is what
was approved and what `src.sender` actually delivers, while the original Hindi/Marathi
translations were written before the edit and were never approved in that form.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from src.db.models import (Advisory, Forecast, ForecastRun, Unit, WeatherFetch,
                           WeatherGrid, WeatherNow, WeatherState)
from src.db.session import get_session

PUBLIC_STATUSES = ("approved", "sent")
ALLOWED_TABLES = frozenset({"units", "forecast_runs", "forecasts", "advisories",
                            "weather_now", "weather_state", "weather_fetch",
                            "weather_grid"})

SKILL_FILE = Path(__file__).resolve().parent / "assets" / "model_skill.csv"
STATIC = Path(__file__).resolve().parent / "static"
GRID_FILES = ("wind", "rain_forecast")
TTL = 600


def _frame(statement) -> pd.DataFrame:
    session = get_session()
    try:
        return pd.read_sql(statement, session.connection())
    finally:
        session.close()


@st.cache_data(ttl=TTL, show_spinner=False)
def latest_run() -> dict | None:
    rows = _frame(select(ForecastRun.id, ForecastRun.as_of, ForecastRun.created_utc,
                         ForecastRun.n_units)
                  .order_by(ForecastRun.as_of.desc(), ForecastRun.id.desc()).limit(1))
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {"id": int(row["id"]), "as_of": pd.Timestamp(row["as_of"]).date(),
            "created_utc": pd.Timestamp(row["created_utc"]).to_pydatetime(),
            "n_units": int(row["n_units"])}


@st.cache_data(ttl=TTL, show_spinner=False)
def forecasts(run_id: int, horizon: int) -> pd.DataFrame:
    """unit_id, unit_name, district, state, hazard, probability, risk_level."""
    return _frame(
        select(Forecast.unit_id, Unit.unit_name, Unit.district, Unit.state,
               Forecast.hazard, Forecast.probability, Forecast.risk_level)
        .join(Unit, Unit.unit_id == Forecast.unit_id)
        .where(Forecast.run_id == run_id, Forecast.horizon == horizon)
    )


@st.cache_data(ttl=TTL, show_spinner=False)
def approved_advisories(run_id: int) -> dict[str, dict]:
    """unit_id -> {"en","hi","mr","edited"} for approved/sent advisories of one run."""
    rows = _frame(
        select(Advisory.unit_id, Advisory.text_en, Advisory.text_hi, Advisory.text_mr,
               Advisory.edited_text)
        .where(Advisory.run_id == run_id, Advisory.status.in_(PUBLIC_STATUSES))
    )
    out: dict[str, dict] = {}
    for row in rows.itertuples():
        if row.edited_text:
            out[row.unit_id] = {"en": row.edited_text, "hi": None, "mr": None,
                                "edited": True}
        else:
            out[row.unit_id] = {"en": row.text_en, "hi": row.text_hi,
                                "mr": row.text_mr, "edited": False}
    return out


@st.cache_data(ttl=TTL, show_spinner=False)
def weather_states() -> pd.DataFrame:
    return _frame(select(WeatherState.state_key, WeatherState.state,
                         WeatherState.n_districts, WeatherState.temp_c,
                         WeatherState.precip_24h_mm, WeatherState.precip_7d_mm))


@st.cache_data(ttl=TTL, show_spinner=False)
def weather_districts() -> pd.DataFrame:
    return _frame(select(WeatherNow.district, WeatherNow.state, WeatherNow.state_key,
                         WeatherNow.temp_c, WeatherNow.precip_24h_mm,
                         WeatherNow.precip_7d_mm))


@st.cache_data(ttl=TTL, show_spinner=False)
def weather_updated() -> dict:
    """When the stored weather was fetched, and whether the latest attempt failed."""
    last_ok = _frame(select(func.max(WeatherFetch.started_utc))
                     .where(WeatherFetch.ok.is_(True))).iloc[0, 0]
    last_any = _frame(select(WeatherFetch.started_utc, WeatherFetch.ok)
                      .order_by(WeatherFetch.started_utc.desc()).limit(1))
    ok_time = None if last_ok is None or pd.isna(last_ok) else \
        pd.Timestamp(last_ok).to_pydatetime().replace(tzinfo=dt.timezone.utc)
    latest_failed = bool(not last_any.empty and not bool(last_any.iloc[0]["ok"]))
    return {"fetched_utc": ok_time, "latest_failed": latest_failed}


@st.cache_data(ttl=3600, show_spinner=False)
def model_skill() -> pd.DataFrame:
    """Test-year skill of the served model (no DB). Written by scripts/export_models.py."""
    if not SKILL_FILE.is_file():
        return pd.DataFrame()
    return pd.read_csv(SKILL_FILE)


@st.cache_data(ttl=1800, show_spinner=False)
def animation_files() -> dict:
    """Publish the animation grids into app/static/ and say which animations can run.

    The 6-hourly job stores wind / rain_forecast JSON in `weather_grid` (never in git, so
    the hosted app is not redeployed four times a day). This copies each payload onto
    the server's own disk, where Streamlit's static serving hands it to the browser, and
    returns a cache-busting URL per file. A missing or unwritable file just leaves that
    animation out: the map hides the option instead of failing.
    """
    out: dict = {"wind": None, "rain": None, "advance": None, "seasons": []}
    try:
        rows = _frame(select(WeatherGrid.name, WeatherGrid.payload, WeatherGrid.fetched_utc))
    except Exception:
        rows = pd.DataFrame(columns=["name", "payload", "fetched_utc"])
    for row in rows.itertuples():
        if row.name not in GRID_FILES:
            continue
        path = STATIC / f"{row.name}.json"
        try:
            if not path.is_file() or path.read_text(encoding="utf-8") != row.payload:
                path.write_text(row.payload, encoding="utf-8")
        except OSError:
            continue
        version = pd.Timestamp(row.fetched_utc).strftime("%Y%m%d%H%M")
        out["wind" if row.name == "wind" else "rain"] = f"{row.name}.json?v={version}"
    # Local development: files written by `--write-static` with no DB rows behind them.
    for name, slot in (("wind", "wind"), ("rain_forecast", "rain")):
        path = STATIC / f"{name}.json"
        if out[slot] is None and path.is_file():
            out[slot] = f"{name}.json?v={int(path.stat().st_mtime)}"
    replay = STATIC / "onset_replay.json"
    if replay.is_file():
        import json

        try:
            out["seasons"] = json.loads(replay.read_text(encoding="utf-8"))["years"]
            out["advance"] = f"onset_replay.json?v={int(replay.stat().st_mtime)}"
        except (OSError, ValueError, KeyError):
            pass
    return out
