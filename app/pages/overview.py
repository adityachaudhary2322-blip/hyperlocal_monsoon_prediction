"""Overview: what is red, what is waiting, what went out, when it last ran."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pandas as pd
import streamlit as st

from app.common import (artifact_state, fmt_dt, log, require_login, scope_query,
                        visible_states)
from src.common import ROOT
from src.db.models import Advisory, Forecast, ForecastChange, ForecastRun, Message, Unit
from src.db.session import get_session

user = require_login()
st.title("Overview")

session = get_session()
try:
    states = visible_states(user)
    unit_query = scope_query(session.query(Unit), Unit, user)
    units = pd.read_sql(unit_query.statement, session.connection())
    unit_ids = set(units["unit_id"])

    run = (session.query(ForecastRun)
           .order_by(ForecastRun.as_of.desc(), ForecastRun.id.desc()).first())

    if run is None:
        st.info("No forecast has been run yet. Use **Run forecast** below "
                "(admin), or `python -m src.pipeline.run --as-of YYYY-MM-DD`.")
        red_units = pending = sent = 0
        forecasts = pd.DataFrame()
        advisories = pd.DataFrame()
    else:
        forecasts = pd.read_sql(
            session.query(Forecast).filter(Forecast.run_id == run.id).statement,
            session.connection())
        forecasts = forecasts[forecasts["unit_id"].isin(unit_ids)]
        advisories = pd.read_sql(
            session.query(Advisory).filter(Advisory.run_id == run.id).statement,
            session.connection())
        advisories = advisories[advisories["unit_id"].isin(unit_ids)]

        red_units = int(forecasts.loc[forecasts["risk_level"] == "red",
                                      "unit_id"].nunique())
        pending = int((advisories["status"] == "pending_approval").sum())
        message_query = session.query(Message)
        sent = message_query.count() if states is None else (
            message_query.join(Advisory, Message.advisory_id == Advisory.id, isouter=True)
            .count()
        )

    columns = st.columns(4)
    columns[0].metric("Units at high risk", red_units,
                      help="Any hazard in the >60% band on the latest run")
    columns[1].metric("Pending approvals", pending)
    columns[2].metric("Messages sent", sent, help="Mock messages count here too")
    columns[3].metric("Last run", run.as_of.isoformat() if run else "-",
                      help=fmt_dt(run.created_utc) if run else "never")

    if run and run.substitution_note:
        st.caption(f":material/info: {run.substitution_note}")

    # ---------------------------------------------------------------- per state
    st.subheader("By state")
    if forecasts.empty:
        st.caption("Nothing to show until a forecast has been run.")
    else:
        merged = forecasts.merge(units[["unit_id", "state", "district"]],
                                 on="unit_id", how="left")
        rows = []
        for state, group in merged.groupby("state"):
            adv = advisories.merge(units[["unit_id", "state"]], on="unit_id")
            adv = adv[adv["state"] == state]
            rows.append({
                "State": state,
                "Units": group["unit_id"].nunique(),
                "◆ High": int(group.loc[group["risk_level"] == "red",
                                     "unit_id"].nunique()),
                "▲ Medium": int(group.loc[group["risk_level"] == "amber",
                                       "unit_id"].nunique()),
                "Advisories": len(adv),
                "⏳ Pending": int((adv["status"] == "pending_approval").sum()),
                "✓ Approved": int((adv["status"] == "approved").sum()),
                "➤ Sent": int((adv["status"] == "sent").sum()),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    # ------------------------------------------------------------ what changed
    if run is not None:
        changes = pd.read_sql(
            session.query(ForecastChange)
            .filter(ForecastChange.run_id == run.id,
                    ForecastChange.escalated.is_(True)).statement,
            session.connection())
        changes = changes[changes["unit_id"].isin(unit_ids)]
        if not changes.empty:
            st.subheader("Escalated since the previous run")
            view = changes.merge(units[["unit_id", "unit_name", "district"]],
                                 on="unit_id", how="left")
            view = view[["unit_name", "district", "hazard", "horizon",
                         "previous_risk", "risk_level", "delta"]]
            view.columns = ["Unit", "District", "Hazard", "Horizon",
                            "Was", "Now", "Change"]
            st.dataframe(view.sort_values("Change", ascending=False),
                         width="stretch", hide_index=True)
finally:
    session.close()

# ------------------------------------------------------------------ run button
st.divider()
if user.role == "admin":
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
