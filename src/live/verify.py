"""Nightly verification of live forecasts whose window has closed.

Run:  python -m src.live.verify [--db-url-env DATABASE_URL_CLOUD]

For each Monday live run (config/live.yaml storage.keep_weekday) and horizon h whose
window days s .. s+h-1 - plus the 9 extra days a dry spell starting on the last day needs
- are all observed, the outcome per unit is computed from obs_unit_rain with exactly the
event definitions the forecast used: engine.probabilities() called with ONE "member", the
observations, returns 1.0 / 0.0 per unit. Each (run, unit, hazard, horizon) is scored
once; reruns skip what is already verified.
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import and_, insert, select

from src.common import load_config
from src.db.models import LiveForecast, LiveRun, LiveVerification, ObsUnitRain
from src.live import assets
from src.live.engine import probabilities
from src.report import DataError, summarize

DRY_TAIL = 9          # a 10-day run starting on the last window day ends 9 days later


def observed_matrix(session, unit_ids: list[str], start: dt.date, end: dt.date
                    ) -> tuple[np.ndarray, list[dt.date]]:
    rows = session.execute(select(ObsUnitRain.unit_id, ObsUnitRain.date, ObsUnitRain.rain_mm)
                           .where(ObsUnitRain.date >= start, ObsUnitRain.date <= end)).all()
    frame = pd.DataFrame(rows, columns=["unit_id", "date", "rain_mm"])
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    if frame.empty:
        return np.full((len(unit_ids), len(days)), np.nan, np.float32), days
    wide = frame.pivot(index="date", columns="unit_id", values="rain_mm")
    wide = wide.reindex(index=days, columns=unit_ids)
    return wide.to_numpy(dtype=np.float32).T, days


def verify(session, today: dt.date) -> int:
    cfg = load_config("live")
    units = assets.units()
    unit_ids = units["unit_id"].astype(str).tolist()
    meta = units.set_index("unit_id")
    latest = session.scalar(select(ObsUnitRain.date).order_by(ObsUnitRain.date.desc()).limit(1))
    if latest is None:
        return 0
    runs = session.execute(select(LiveRun).where(
        LiveRun.run_type == "live", LiveRun.keep_detail.is_(True))).scalars().all()
    written = 0
    for run in runs:
        if run.run_date.weekday() != cfg["storage"]["keep_weekday"]:
            continue
        hazards = [h for h in run.hazards.split(",") if h]
        season_start = dt.date.fromisoformat(f"{run.run_date.year}-{cfg['observed']['backfill_from']}")
        for h in cfg["horizons"]:
            end = run.run_date + dt.timedelta(days=h - 1 + DRY_TAIL)
            if end > latest:
                continue
            done = session.scalar(select(LiveVerification.run_id).where(and_(
                LiveVerification.run_id == run.id, LiveVerification.horizon == h)).limit(1))
            if done is not None:
                continue
            obs, days = observed_matrix(session, unit_ids, season_start, end)
            s = (run.run_date - season_start).days
            outcome = probabilities(obs[None], pd.DatetimeIndex(days), s, run.run_date,
                                    hazards, [h], units)
            fc = pd.DataFrame(session.execute(select(
                LiveForecast.unit_id, LiveForecast.hazard, LiveForecast.p_blend,
                LiveForecast.p_ml, LiveForecast.p_ec46).where(
                LiveForecast.run_id == run.id, LiveForecast.horizon == h)).all(),
                columns=["unit_id", "hazard", "p_blend", "p_ml", "p_ec46"])
            index = {u: i for i, u in enumerate(unit_ids)}
            rows = []
            for r in fc.itertuples():
                y = outcome.get((r.hazard, h))
                if y is None:
                    continue
                v = y[index[r.unit_id]]
                if not np.isfinite(v):
                    continue
                rows.append({"run_id": run.id, "unit_id": r.unit_id, "hazard": r.hazard,
                             "horizon": h, "run_date": run.run_date,
                             "state": meta.at[r.unit_id, "state"], "tier": meta.at[r.unit_id, "tier"],
                             "p_blend": r.p_blend, "p_ml": None if pd.isna(r.p_ml) else r.p_ml,
                             "p_ec46": None if pd.isna(r.p_ec46) else r.p_ec46,
                             "outcome": bool(v >= 0.5)})
            for i in range(0, len(rows), 5000):
                session.execute(insert(LiveVerification), rows[i:i + 5000])
            written += len(rows)
            print(f"  run {run.run_date} +{h}d: {len(rows):,} forecasts verified")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url-env", metavar="NAME", default=None)
    args = parser.parse_args(argv)
    if args.db_url_env:
        from src.db.session import use_url
        from src.runtime import env as read_env

        value = read_env(args.db_url_env, "") or ""
        if not value.strip():
            raise DataError(f"{args.db_url_env} is not set")
        use_url(value)
    from src.db.session import create_all, session_scope

    print("--- live.verify ---")
    create_all()
    today = dt.datetime.now(dt.timezone.utc).date()
    with session_scope() as session:
        n = verify(session, today)
        # Blend-weight SUGGESTIONS for the Models page; config/blend.yaml is not touched.
        import json

        from src.accuracy import suggest_blend_weights
        from src.db.models import LiveVerification as V
        from src.db.models import Setting

        frame = pd.read_sql(select(V.hazard, V.horizon, V.p_ml, V.p_ec46, V.outcome),
                            session.connection())
        if not frame.empty:
            suggestions = suggest_blend_weights(frame)
            row = session.get(Setting, "blend_suggestions")
            value = json.dumps({"computed": today.isoformat(), "weights": suggestions})
            if row is None:
                session.add(Setting(key="blend_suggestions", value=value, updated_by="live.verify"))
            else:
                row.value, row.updated_by = value, "live.verify"
            print(f"  blend suggestions: {sum(1 for s in suggestions.values() if s['suggested_w'] is not None)}"
                  f" of {len(suggestions)} hazard x horizon pairs have enough data")
    # Zero is a normal answer early in the season (no window has closed yet): report it,
    # do not fail the nightly job.
    summarize("live.verify", rows=n, extra={"note": "nothing due yet" if not n else "ok"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
