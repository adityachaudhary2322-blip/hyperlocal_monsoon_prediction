"""Overview: what is waiting, what is urgent, what went out, and how fresh the forecast is.

Risk numbers come from the daily national run (live_runs, src/live/); advisories from the
pilot advisory run - or, for the evaluator demo account, from the demo copies only
(src/demo.py).
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pandas as pd
import streamlit as st

from app.common import artifact_state, log, require_login, visible_states
from app.permissions import can
from app.theme import page_header, tokens
from src import demo
from src.common import ROOT
from src.db.models import Advisory, LiveForecast, LiveRun, LiveUnit, Message, Subscriber, Unit
from src.db.session import get_session

FRESH_DAYS = 1          # the live run is daily; older than this is stale

user = require_login()
page_header("Overview", "What needs your review, what is urgent, what went out today, and "
            "how fresh the forecast is.")

states = visible_states(user)
today = dt.datetime.now(dt.timezone.utc).date()
session = get_session()
try:
    with st.spinner("Loading the latest numbers..."):
        # live national run: risk by state ------------------------------------------
        live = (session.query(LiveRun).filter(LiveRun.run_type == "live",
                                              LiveRun.keep_detail.is_(True))
                .order_by(LiveRun.run_date.desc(), LiveRun.id.desc()).first())
        risk = pd.DataFrame()
        if live is not None:
            q = (session.query(LiveUnit.state, LiveForecast.unit_id, LiveForecast.level)
                 .join(LiveUnit, LiveUnit.unit_id == LiveForecast.unit_id)
                 .filter(LiveForecast.run_id == live.id, LiveForecast.horizon == 7))
            if states is not None:
                q = q.filter(LiveUnit.state.in_(states))
            risk = pd.DataFrame(q.all(), columns=["state", "unit_id", "level"])

        # advisories this user works on ----------------------------------------------
        adv_q = demo.advisory_scope(session.query(Advisory.unit_id, Advisory.status,
                                                  Advisory.urgent, Unit.state)
                                    .join(Unit, Unit.unit_id == Advisory.unit_id), session, user)
        run = demo.advisory_run(session, user)
        adv_q = adv_q.filter(Advisory.run_id == (run.id if run else -1))
        if states is not None:
            adv_q = adv_q.filter(Unit.state.in_(states))
        advisories = pd.DataFrame(adv_q.all(), columns=["unit_id", "status", "urgent", "state"])

        start = dt.datetime.combine(today, dt.time.min, tzinfo=dt.timezone.utc)
        sent_q = demo.subscriber_scope(
            session.query(Message.id).join(Subscriber, Subscriber.id == Message.subscriber_id)
            .filter(Message.sent_utc >= start), user)
        if states is not None:
            sent_q = sent_q.filter(Subscriber.state.in_(states))
        sent_today = sent_q.count()
finally:
    session.close()

pending = advisories[advisories["status"] == "pending_approval"] if not advisories.empty else advisories
high_units = int(risk.loc[risk["level"] == "red", "unit_id"].nunique()) if not risk.empty else 0

with st.container(key="mo_metrics"):
    m = st.columns(4)
m[0].metric("Waiting for review", len(pending))
m[1].metric("Urgent", int(pending["urgent"].sum()) if not pending.empty else 0,
            help="Heavy rain within 7 days, or a red risk band with a short lead time")
m[2].metric("Sent today", sent_today,
            help="Demo outbox only" if demo.is_demo(user) else "Mock messages count too")
m[3].metric("Units at high risk", high_units, help="Any hazard above 60% in the next 7 days")

if live is None:
    st.html("<p class='mo-fresh mo-stale'>▲ <b>Last forecast run:</b> none yet. The daily "
            "run starts at 10:00 IST.</p>")
else:
    age = (today - live.run_date).days
    fresh = age <= FRESH_DAYS
    when = live.created_utc.strftime("%d %b %Y, %H:%M UTC") if live.created_utc else live.run_date
    st.html(f"<p class='mo-fresh {'mo-ok' if fresh else 'mo-stale'}'>"
            f"{'● ' if fresh else '▲ '}<b>Last forecast run:</b> {when} · "
            f"{'fresh' if fresh else f'stale, {age} days old'}"
            + (" · data delayed" if live.data_delayed else "") + "</p>")

# ---------------------------------------------------------------- per state
st.subheader("By state")
if risk.empty:
    st.caption("No forecast yet. New forecasts arrive every morning.")
else:
    rows = []
    for state, g in risk.groupby("state"):
        a = advisories[advisories["state"] == state] if not advisories.empty else advisories
        count = lambda s: int((a["status"] == s).sum()) if not a.empty else 0
        rows.append({"State": state, "Units": g["unit_id"].nunique(),
                     "High risk": int(g.loc[g["level"] == "red", "unit_id"].nunique()),
                     "Medium risk": int(g.loc[g["level"] == "amber", "unit_id"].nunique()),
                     "Waiting": count("pending_approval"), "Approved": count("approved"),
                     "Sent": count("sent")})
    frame = pd.DataFrame(rows).sort_values("High risk", ascending=False)
    tok = tokens()
    styled = (frame.style
              .format({"High risk": "◆ {}", "Medium risk": "▲ {}"})
              .map(lambda v: f"color: {tok['risk-high-ink']}; font-weight: 600", subset=["High risk"])
              .map(lambda v: f"color: {tok['risk-med-ink']}; font-weight: 600", subset=["Medium risk"]))
    st.dataframe(styled, hide_index=True, width="stretch", column_config={
        "State": st.column_config.TextColumn(width="medium"),
        "Units": st.column_config.NumberColumn(help="Sub-districts in the forecast"),
        "High risk": st.column_config.TextColumn(help="Units with any hazard above 60%, next 7 days"),
        "Medium risk": st.column_config.TextColumn(help="Units with a hazard at 30-60%, next 7 days"),
        "Waiting": st.column_config.NumberColumn(help="Advisories waiting for review"),
    })
    st.caption("Risk: daily national run, next 7 days. Advisories: "
               + ("demo copies for the pilot districts." if demo.is_demo(user)
                  else "the latest pilot advisory run."))

# ------------------------------------------------------------------ run button
st.divider()
if can(user, "run_forecast"):
    st.subheader("Run a forecast")

    from src.config import hosted
    from src.runtime import CHRONOS_OFF_REASON, chronos_enabled

    on_cloud = hosted()
    models_ready, models_detail = artifact_state()
    if not models_ready:
        st.warning(
            f":material/error: The forecast models are not loaded ({models_detail}). "
            + ("Commit them with `python scripts/export_models.py`." if on_cloud else
               "Train them with `python -m src.train_baselines`, or commit the "
               "assets with `python scripts/export_models.py`.")
        )

    # Hosted, Chronos-2 is not an option at all: the stacker that turns its quantiles
    # into probabilities is never persisted, and torch is not installed. The laptop
    # keeps the switch so a full Chronos run can be written to the cloud database with
    # `python -m src.pipeline.run --db-url-env DATABASE_URL_CLOUD`.
    if on_cloud or not chronos_enabled():
        st.caption(":material/memory: **Chronos-2 runs on the forecast engine.** "
                   "This site forecasts with LightGBM on CPU.")
        if not on_cloud:
            with st.expander("Why Chronos-2 is off"):
                st.write(CHRONOS_OFF_REASON)
    else:
        st.info(":material/memory: Chronos-2 is enabled for this forecast path.")

    left, right = st.columns([1, 3])
    as_of = left.date_input("As-of date", value=dt.date(2024, 6, 20),
                            help="A date with no issued forecast falls back to the "
                                 "nearest one, and the run records that.")
    if left.button("Run forecast", type="primary", disabled=not models_ready,
                   help=None if models_ready else models_detail):
        arguments = ["--pilot-only", "--as-of", as_of.isoformat(),
                     "--created-by", user.username]
        with st.spinner(f"Running the pipeline for {as_of}..."):
            if on_cloud:
                # In-process: the free tier has ~2.7 GB for the whole container, and a
                # second interpreter loading LightGBM and the feature table would
                # roughly double peak memory.
                import contextlib
                import io

                from src.pipeline.run import main as run_pipeline

                captured = io.StringIO()
                try:
                    with contextlib.redirect_stdout(captured):
                        code = run_pipeline(arguments)
                    output, error = captured.getvalue(), ""
                except Exception as problem:
                    code = 1
                    output = captured.getvalue()
                    error = f"{type(problem).__name__}: {problem}"
            else:
                finished = subprocess.run(
                    [sys.executable, "-m", "src.pipeline.run", *arguments],
                    cwd=ROOT, capture_output=True, text=True,
                )
                code = finished.returncode
                output, error = finished.stdout or "", finished.stderr or ""

        log(user.username, "run_forecast", "forecast_run", as_of.isoformat(),
            f"exit={code} hosted={on_cloud}")
        if code == 0:
            st.success(f"Forecast run for {as_of} finished.")
            right.code(output.strip()[-2500:] or "(no output)")
            st.rerun()
        else:
            st.error("The pipeline failed.")
            right.code((error or output)[-2500:] or "(no output)")
else:
    st.caption("Running a forecast is an admin action.")
