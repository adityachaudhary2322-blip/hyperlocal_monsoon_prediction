"""SQLAlchemy schema for the control panel (data/monsoon.db).

Design notes that matter downstream:

* **Advisory text is versioned by column, not overwritten.** `text_en/hi/mr` is what
  the rule engine produced; `edited_text` is what an officer changed it to. Keeping
  both is what makes the audit trail meaningful - "approved" is only informative if
  you can see what was approved against what was proposed.
* **`source` records where the words came from** ("template", "llm", "officer"). Per
  config/llm.yaml, anything with source="llm" must not be sent without approval.
* **Subscribers carry `consent` and `do_not_send`**, both checked at send time. A
  message is never built for a subscriber who has not consented.
* Everything an officer does lands in `audit_log`.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """A timestamp column that stores UTC identically on SQLite and Postgres.

    The two backends disagree about what to do with a timezone-aware value bound to a
    naive column: SQLite's driver drops the offset while formatting, and Postgres
    discards it during the cast to `timestamp`. Both happen to be right while every
    caller passes UTC, and both are silently wrong the moment one does not - the offset
    vanishes and the wall-clock time is kept. Converting here means the correctness of
    the stored value does not depend on which database is behind the app, or on six
    call sites all remembering `timezone.utc`.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, dt.datetime) and value.tzinfo is not None:
            return value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Geography and forecasts
# --------------------------------------------------------------------------
class Unit(Base):
    __tablename__ = "units"

    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    unit_name: Mapped[str] = mapped_column(String(128))
    district: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(128), index=True)
    zone_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    unit_type: Mapped[str] = mapped_column(String(32), default="subdistrict")
    centroid_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    centroid_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_pilot: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ForecastRun(Base):
    __tablename__ = "forecast_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)
    requested_as_of: Mapped[dt.date] = mapped_column(Date)
    # Set when the requested date had no data and the nearest one was used instead.
    substitution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_choice: Mapped[str | None] = mapped_column(Text, nullable=True)
    pilot_only: Mapped[bool] = mapped_column(Boolean, default=True)
    n_units: Mapped[int] = mapped_column(Integer, default=0)
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    forecasts: Mapped[list["Forecast"]] = relationship(back_populates="run")


class Forecast(Base):
    __tablename__ = "forecasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("units.unit_id"), index=True)
    hazard: Mapped[str] = mapped_column(String(16), index=True)   # onset/dry/heavy
    horizon: Mapped[int] = mapped_column(Integer, index=True)     # 7/14/21/28
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(8))            # green/amber/red
    model: Mapped[str] = mapped_column(String(64))

    run: Mapped[ForecastRun] = relationship(back_populates="forecasts")

    __table_args__ = (
        UniqueConstraint("run_id", "unit_id", "hazard", "horizon",
                         name="uq_forecast"),
        Index("ix_forecast_lookup", "run_id", "hazard", "horizon"),
    )


class ForecastChange(Base):
    """How a unit's risk moved since the previous run - what an officer scans for."""

    __tablename__ = "forecast_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"), index=True)
    previous_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("units.unit_id"), index=True)
    hazard: Mapped[str] = mapped_column(String(16))
    horizon: Mapped[int] = mapped_column(Integer)
    previous_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_risk: Mapped[str | None] = mapped_column(String(8), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(8))
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# --------------------------------------------------------------------------
# Advisories and alerts
# --------------------------------------------------------------------------
ADVISORY_STATUSES = ("pending_approval", "approved", "rejected", "sent")


class Advisory(Base):
    __tablename__ = "advisories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("units.unit_id"), index=True)
    as_of: Mapped[dt.date] = mapped_column(Date, index=True)

    action: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crops: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String(8))
    hazard: Mapped[str | None] = mapped_column(String(16), nullable=True)
    horizon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(8), index=True)

    status: Mapped[str] = mapped_column(String(24), default="pending_approval",
                                        index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_mr: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="template")

    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "unit_id", name="uq_advisory_run_unit"),
    )


class Alert(Base):
    """An officer-written message, not produced by the rule engine."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160))
    target_kind: Mapped[str] = mapped_column(String(16))     # state / district
    target_value: Mapped[str] = mapped_column(String(128), index=True)
    text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_hi: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_mr: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="officer")
    status: Mapped[str] = mapped_column(String(24), default="pending_approval",
                                        index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_utc: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------
# People and messages
# --------------------------------------------------------------------------
class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    language: Mapped[str] = mapped_column(String(4), default="hi")
    unit_id: Mapped[str | None] = mapped_column(ForeignKey("units.unit_id"),
                                                nullable=True, index=True)
    district: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(128), index=True)
    # Both are checked before a message is built. Consent is opt-in, not assumed.
    consent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    do_not_send: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"),
                                               index=True)
    advisory_id: Mapped[int | None] = mapped_column(ForeignKey("advisories.id"),
                                                    nullable=True, index=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"),
                                                 nullable=True, index=True)
    language: Mapped[str] = mapped_column(String(4))
    body: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16), default="mock", index=True)
    delivery_status: Mapped[str] = mapped_column(String(32),
                                                 default="delivered (mock)")
    provider_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow,
                                                  index=True)
    sent_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(160), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="officer")  # admin/officer
    # Empty for an admin, who sees everything.
    assigned_states: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    def states(self) -> list[str]:
        return [s for s in (self.assigned_states or "").split("|") if s]


class Setting(Base):
    """Runtime overrides an admin can change from the Models page.

    Kept in the database rather than written back into config/llm.yaml: that file
    carries the provenance comments for every verified endpoint and model name, and a
    round-trip through yaml.safe_dump would delete all of them. Every change is
    mirrored into audit_log.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------
# National weather (public dashboard). Written by src/jobs/weather.py.
# --------------------------------------------------------------------------
class WeatherNow(Base):
    """Latest Open-Meteo values per Survey of India district, one row each.

    Replaced wholesale by a successful fetch and left alone by a failed one, so the
    public page always has the last good values plus the time they were fetched.
    """

    __tablename__ = "weather_now"

    state: Mapped[str] = mapped_column(String(128), primary_key=True)
    district: Mapped[str] = mapped_column(String(128), primary_key=True)
    state_key: Mapped[str] = mapped_column(String(64), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    valid_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    fetched_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_24h_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_7d_mm: Mapped[float | None] = mapped_column(Float, nullable=True)


class WeatherState(Base):
    """State means of weather_now, written in the same transaction."""

    __tablename__ = "weather_state"

    state_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(128))
    n_districts: Mapped[int] = mapped_column(Integer)
    fetched_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_24h_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    precip_7d_mm: Mapped[float | None] = mapped_column(Float, nullable=True)


class WeatherFetch(Base):
    """One row per job run, successful or not - the public page reads the last ok one."""

    __tablename__ = "weather_fetch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow,
                                                     index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    n_districts: Mapped[int] = mapped_column(Integer, default=0)
    n_expected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WeatherGrid(Base):
    """Latest gridded animation data (wind.json, rain_forecast.json) as JSON text.

    Written every 6 h by src/jobs/weather_grid.py from GitHub Actions. Kept in the
    database rather than committed to app/static/, because a commit every 6 hours would
    redeploy the hosted app four times a day and grow the repo by ~0.5 MB each time. The
    app copies the payload into app/static/ on its own disk (app/public_data.py).
    """

    __tablename__ = "weather_grid"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    fetched_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    n_points: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------
# Live national engine (src/live/, GitHub Actions daily). New tables only, so no
# migration of existing Postgres tables is ever needed. Probabilities are stored as
# per-mille SmallIntegers to keep the Neon free tier (0.5 GB) comfortable.
# --------------------------------------------------------------------------
class LiveUnit(Base):
    """All-India units with their confidence tier (the pilot `units` table is left alone)."""

    __tablename__ = "live_units"

    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    unit_name: Mapped[str] = mapped_column(String(128))
    name_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    district: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[str] = mapped_column(String(128), index=True)
    tier: Mapped[str] = mapped_column(String(16))
    seasonal_low: Mapped[bool] = mapped_column(Boolean, default=False)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)


class LiveRun(Base):
    __tablename__ = "live_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    run_type: Mapped[str] = mapped_column(String(16), default="live", index=True)  # live/replay
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="ok")        # ok / partial
    data_delayed: Mapped[bool] = mapped_column(Boolean, default=False)
    season: Mapped[str] = mapped_column(String(16))                      # monsoon / post
    hazards: Mapped[str] = mapped_column(String(128))
    sources: Mapped[str] = mapped_column(Text)                           # JSON
    calls: Mapped[float] = mapped_column(Float, default=0.0)             # Open-Meteo weight
    n_units: Mapped[int] = mapped_column(Integer, default=0)
    keep_detail: Mapped[bool] = mapped_column(Boolean, default=True)


class LiveForecast(Base):
    __tablename__ = "live_forecasts"

    run_id: Mapped[int] = mapped_column(ForeignKey("live_runs.id", ondelete="CASCADE"),
                                        primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hazard: Mapped[str] = mapped_column(String(16), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    p_ml: Mapped[int | None] = mapped_column(Integer, nullable=True)     # per mille
    p_ec46: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p_blend: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(8))


class LiveOutlook(Base):
    __tablename__ = "live_outlook"

    run_id: Mapped[int] = mapped_column(ForeignKey("live_runs.id", ondelete="CASCADE"),
                                        primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    week: Mapped[int] = mapped_column(Integer, primary_key=True)          # 1..4
    p10: Mapped[float] = mapped_column(Float)
    p50: Mapped[float] = mapped_column(Float)
    p90: Mapped[float] = mapped_column(Float)
    normal: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_below: Mapped[int | None] = mapped_column(Integer, nullable=True)   # per mille
    p_near: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p_above: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # EC46 ensemble-mean weekly averages, for crop threats in weeks 2-4.
    t_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    rh_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    soil_mean: Mapped[float | None] = mapped_column(Float, nullable=True)


class LiveWeather(Base):
    """Observed and near-term weather per unit for the latest run (the side panel)."""

    __tablename__ = "live_weather"

    run_id: Mapped[int] = mapped_column(ForeignKey("live_runs.id", ondelete="CASCADE"),
                                        primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rain_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_14d: Mapped[float | None] = mapped_column(Float, nullable=True)
    normal_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    normal_14d: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    rh_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    soil_moisture: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_next_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    rain_next_7d: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmax_next_7d: Mapped[float | None] = mapped_column(Float, nullable=True)   # hottest day
    rh_next_7d: Mapped[float | None] = mapped_column(Float, nullable=True)     # mean RH
    obs_source: Mapped[str] = mapped_column(String(32), default="")


class ObsUnitRain(Base):
    """Season-to-date observed daily unit rainfall (IMD, else bias-adjusted Open-Meteo)."""

    __tablename__ = "obs_unit_rain"

    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    rain_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16))                       # imd / om_adj


class LiveVerification(Base):
    """A live forecast scored against what the observed rain did, once its window closed.

    Only the Monday runs are scored (config/live.yaml storage.keep_weekday), per unit, so a
    season stays near 50 MB; the Accuracy page aggregates to district and state.
    """

    __tablename__ = "live_verification"

    run_id: Mapped[int] = mapped_column(ForeignKey("live_runs.id", ondelete="CASCADE"),
                                        primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    hazard: Mapped[str] = mapped_column(String(16), primary_key=True)
    horizon: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_date: Mapped[dt.date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(128), index=True)
    tier: Mapped[str] = mapped_column(String(16), index=True)
    p_blend: Mapped[int | None] = mapped_column(Integer, nullable=True)   # per mille
    p_ml: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p_ec46: Mapped[int | None] = mapped_column(Integer, nullable=True)
    p_clim: Mapped[int | None] = mapped_column(Integer, nullable=True)    # base rate
    outcome: Mapped[bool] = mapped_column(Boolean)
    verified_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_utc: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow,
                                                     index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
