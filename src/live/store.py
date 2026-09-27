"""Database writes for the live engine: runs, forecasts, outlook, weather, observations.

Storage policy (config/live.yaml `storage`, Neon free tier 0.5 GB):
* full detail (forecasts, outlook, weather) for the last `keep_daily_days` live runs,
  every run issued on `keep_weekday` (Mondays - the runs verification scores), and every
  replay run;
* older daily runs keep only their live_runs row (sources, flags, call count);
* observed unit rain is kept for the current season only.
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
from sqlalchemy import delete, func, insert, select, text, update

from src.db.models import (LiveForecast, LiveOutlook, LiveRun, LiveUnit, LiveWeather,
                           ObsUnitRain)

CHUNK = 5000


def _bulk(session, model, rows: list[dict]) -> None:
    for i in range(0, len(rows), CHUNK):
        session.execute(insert(model), rows[i:i + CHUNK])


def ensure_units(session, units: pd.DataFrame) -> int:
    have = session.scalar(select(func.count()).select_from(LiveUnit)) or 0
    if have == len(units):
        return 0
    session.execute(delete(LiveUnit))
    _bulk(session, LiveUnit, [
        {"unit_id": str(u.unit_id), "unit_name": str(u.unit_name), "name_ok": bool(u.name_ok),
         "district": str(u.district), "state": str(u.state), "tier": str(u.tier),
         "seasonal_low": bool(u.seasonal_low), "lat": float(u.lat), "lon": float(u.lon)}
        for u in units.itertuples()])
    return len(units)


def stored_obs(session, unit_ids: list[str], since: dt.date) -> dict[dt.date, tuple[np.ndarray, str]]:
    rows = session.execute(select(ObsUnitRain.unit_id, ObsUnitRain.date, ObsUnitRain.rain_mm,
                                  ObsUnitRain.source).where(ObsUnitRain.date >= since)).all()
    if not rows:
        return {}
    frame = pd.DataFrame(rows, columns=["unit_id", "date", "rain_mm", "source"])
    wide = frame.pivot(index="date", columns="unit_id", values="rain_mm").reindex(columns=unit_ids)
    src = frame.groupby("date")["source"].first()
    return {pd.Timestamp(d).date(): (wide.loc[d].to_numpy(dtype=np.float32), src.loc[d])
            for d in wide.index}


def write_obs(session, unit_ids: list[str], dates: list[dt.date], obs: np.ndarray,
              sources: list[str], only: set[dt.date]) -> int:
    """Replace stored observations for the dates in `only` (the recent window)."""
    days = [d for d in dates if d in only]
    if not days:
        return 0
    session.execute(delete(ObsUnitRain).where(ObsUnitRain.date.in_(days)))
    rows = []
    for j, day in enumerate(dates):
        if day not in only or sources[j] == "missing":
            continue
        col = obs[:, j]
        rows += [{"unit_id": u, "date": day, "rain_mm": None if not np.isfinite(v) else float(v),
                  "source": sources[j]} for u, v in zip(unit_ids, col)]
    _bulk(session, ObsUnitRain, rows)
    return len(rows)


def _pm(v) -> int | None:
    return None if v is None or not np.isfinite(v) else int(round(float(v) * 1000))


def write_run(session, *, today: dt.date, run_type: str, season: str, hazards: list[str],
              sources: dict, calls: float, delayed: bool, unit_ids: list[str],
              forecasts: dict, outlook: dict, weather: dict) -> int:
    run = LiveRun(run_date=today, run_type=run_type, season=season, hazards=",".join(hazards),
                  sources=json.dumps(sources, default=str), calls=round(calls, 1),
                  data_delayed=delayed, n_units=len(unit_ids),
                  status="ok" if forecasts else "partial")
    session.add(run)
    session.flush()
    rows = []
    for (hazard, horizon), rec in forecasts.items():
        for u, p_ec, p_ml, p_bl, lvl in zip(unit_ids, rec["p_ec46"], rec["p_ml"],
                                             rec["p_blend"], rec["level"]):
            if lvl == "none":
                continue                        # not judgeable (e.g. onset already happened)
            rows.append({"run_id": run.id, "unit_id": u, "hazard": hazard, "horizon": horizon,
                         "p_ec46": _pm(p_ec), "p_ml": _pm(p_ml), "p_blend": _pm(p_bl),
                         "level": lvl})
    _bulk(session, LiveForecast, rows)
    orows = []
    for k in range(outlook["p50"].shape[0]):
        for i, u in enumerate(unit_ids):
            orows.append({"run_id": run.id, "unit_id": u, "week": k + 1,
                          "p10": float(outlook["p10"][k, i]), "p50": float(outlook["p50"][k, i]),
                          "p90": float(outlook["p90"][k, i]),
                          "normal": None if not np.isfinite(outlook["normal"][k, i])
                          else float(outlook["normal"][k, i]),
                          "p_below": _pm(outlook["below"][k, i]), "p_near": _pm(outlook["near"][k, i]),
                          "p_above": _pm(outlook["above"][k, i]),
                          **{key: (None if key not in outlook or not np.isfinite(outlook[key][k, i])
                                   else round(float(outlook[key][k, i]), 3))
                             for key in ("t_mean", "rh_mean", "soil_mean")}})
    _bulk(session, LiveOutlook, orows)
    wrows = []
    for i, u in enumerate(unit_ids):
        rec = {k: (None if not np.isfinite(v[i]) else round(float(v[i]), 2))
               for k, v in weather.items() if k != "obs_source"}
        wrows.append({"run_id": run.id, "unit_id": u, "obs_source": weather["obs_source"], **rec})
    _bulk(session, LiveWeather, wrows)
    return run.id


def prune(session, cfg: dict, today: dt.date) -> int:
    """Drop detail rows of old daily live runs except Mondays; returns runs pruned."""
    cutoff = today - dt.timedelta(days=cfg["keep_daily_days"])
    runs = session.execute(select(LiveRun.id, LiveRun.run_date).where(
        LiveRun.run_type == "live", LiveRun.keep_detail.is_(True),
        LiveRun.run_date < cutoff)).all()
    drop = [r.id for r in runs if r.run_date.weekday() != cfg["keep_weekday"]]
    if drop:
        for model in (LiveForecast, LiveOutlook, LiveWeather):
            session.execute(delete(model).where(model.run_id.in_(drop)))
        session.execute(update(LiveRun).where(LiveRun.id.in_(drop)).values(keep_detail=False))
    session.execute(delete(ObsUnitRain).where(ObsUnitRain.date < dt.date(today.year, 1, 1)))
    return len(drop)


def db_size_mb(session) -> float | None:
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        return session.scalar(text("SELECT pg_database_size(current_database())")) / 1e6
    if bind.dialect.name == "sqlite":
        from pathlib import Path

        path = Path(bind.url.database or "")
        return path.stat().st_size / 1e6 if path.is_file() else None
    return None
