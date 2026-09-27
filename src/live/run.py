"""Daily live national run. GitHub Actions calls this at 04:30 UTC; nothing is committed.

Run:  python -m src.live.run [--db-url-env DATABASE_URL_CLOUD] [--dry-run] [--cache]
                             [--date YYYY-MM-DD]

Steps, each printed with numbers:
  1. 0.5 deg short-range forecast (+14 past days)   -> weather now, fallback rainfall
  2. EC46 51 members at 1.5 deg + ensemble mean      -> event probabilities, outlook
  3. IMD real-time observed rain (season to date)    -> history for onset / dry spells
  4. probabilities per unit x hazard x horizon: EC46 member fractions; ML is "coming soon"
     (Stage 2 deferred), so p_blend = p_ec46 and the run says blend_basis = ec46_only
  5. month outlook (weeks 1-4) and recent weather vs normal
  6. store, prune, national weather layer, DB size (warn above 400 MB)

Replay of past dates (run_type = "replay") needs the national ML models: the free API
keeps no archive of past EC46 runs. Until Stage 2 lands, replay is "coming soon" and the
pilot replay stays python -m src.pipeline.run --as-of <date>.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from src.common import DATA_PROCESSED, ROOT, load_config
from src.live import assets
from src.live.engine import (blend, levels, outlook, probabilities, recent_weather, season_for,
                             to_units)
from src.live.observed import assemble, bias_ratio, imd_units
from src.live.openmeteo import Client, ec46_mean, ec46_members, short_range
from src.report import DataError, summarize


def _dates(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def national_weather(sr_units_unused, sr_daily, sr_dates, current, today) -> list:
    """weather_now rows for the public national layer: each Survey of India district takes
    the nearest 0.5 deg point (the layer the retired 3-hourly job used to fill)."""
    from src.jobs.weather import District, Reading

    g = assets.grid("05")
    cents = pd.read_csv(ROOT / "app" / "static" / "district_centroids.csv")
    idx = [int(np.argmin((g["lat"] - r.lat) ** 2 + (g["lon"] - r.lon) ** 2))
           for r in cents.itertuples()]
    t0 = sr_dates.index(today.isoformat()) if today.isoformat() in sr_dates else len(sr_dates) - 7
    rain = sr_daily["precipitation_sum"]
    valid = dt.datetime.fromisoformat(current["time"]).replace(
        tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).astimezone(dt.timezone.utc)
    out = []
    for r, p in zip(cents.itertuples(), idx):
        def f(x):
            return None if not np.isfinite(x) else round(float(x), 2)
        out.append(Reading(District(r.district, r.state, r.state_key, r.lat, r.lon), valid,
                           f(current["temperature_2m"][p]), f(rain[t0, p]),
                           f(np.nansum(rain[t0:t0 + 7, p]))))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url-env", metavar="NAME", default=None)
    parser.add_argument("--dry-run", action="store_true", help="compute, do not write")
    parser.add_argument("--cache", action="store_true",
                        help="cache API responses in data/cache/live (laptop development)")
    parser.add_argument("--date", default=None, help="override today's date (UTC)")
    args = parser.parse_args(argv)

    if args.db_url_env:
        from src.db.session import use_url
        from src.runtime import env as read_env

        value = read_env(args.db_url_env, "") or ""
        if not value.strip():
            raise DataError(f"{args.db_url_env} is not set")
        use_url(value)

    cfg = load_config("live")
    today = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    season, hazards = season_for(today)
    horizons = cfg["horizons"]
    print(f"--- live.run {today} ({season}: {', '.join(hazards) or 'no hazards'}) ---")

    assets.check_alignment()
    units = assets.units()
    unit_ids = units["unit_id"].astype(str).tolist()
    client = Client(cfg["retry"], DATA_PROCESSED.parent / "cache" / "live" if args.cache else None,
                    rate=cfg["rate"])
    g05, g15 = assets.grid("05"), assets.grid("15")

    # 1. short range ----------------------------------------------------------
    print("  1. 0.5 deg forecast")
    sr_daily, sr_dates, current = short_range(client, g05["lat"], g05["lon"], cfg["short_range"])
    sr_units = {v: to_units(g05["W"], a) for v, a in sr_daily.items()}      # (days, units)
    cur_units = {v: to_units(g05["W"], a[None])[0] for v, a in current.items() if v != "time"}

    # 2. EC46 -----------------------------------------------------------------
    print("  2. EC46")
    members, ec_dates = ec46_members(client, g15["lat"], g15["lon"], cfg["ec46"])
    ec_mean, _ = ec46_mean(client, g15["lat"], g15["lon"], cfg["ec46"])
    ec_units = np.transpose(to_units(g15["W"], members), (0, 2, 1))       # (members, units, days)
    ec_first = dt.date.fromisoformat(ec_dates[0])
    if ec_first > today:
        raise DataError(f"EC46 starts {ec_first}, after today {today}")
    ec_units = ec_units[:, :, (today - ec_first).days:]                  # align day 0 = today
    ec_mean_units = {v: to_units(g15["W"], a) for v, a in ec_mean.items()}

    # 3. observed -------------------------------------------------------------
    print("  3. observed rain")
    obs_cfg = cfg["observed"]
    start = dt.date.fromisoformat(f"{today.year}-{obs_cfg['backfill_from']}")
    obs_dates = _dates(start, today - dt.timedelta(days=1))
    recent = set(obs_dates[-obs_cfg["imd_realtime_days"]:])
    stored: dict = {}
    if not args.dry_run:
        from src.db.session import create_all, session_scope
        from src.live.store import stored_obs

        create_all()
        with session_scope() as session:
            stored = stored_obs(session, unit_ids, start)
    need = [d for d in obs_dates if d in recent or d not in stored]
    imd = imd_units(need)
    om = {dt.date.fromisoformat(d): sr_units["precipitation_sum"][i]
          for i, d in enumerate(sr_dates) if dt.date.fromisoformat(d) < today}
    ratio = bias_ratio(imd, om, tuple(obs_cfg["bias_clip"]))
    obs, obs_sources = assemble(obs_dates, stored, imd, om, ratio)
    latest_imd = max((d for d, s in zip(obs_dates, obs_sources) if s == "imd"), default=None)
    counts = pd.Series(obs_sources).value_counts().to_dict()
    print(f"     {len(obs_dates)} days: {counts}; latest IMD day {latest_imd}")

    # 4. probabilities --------------------------------------------------------
    print("  4. probabilities")
    series = np.concatenate([np.repeat(obs[None], ec_units.shape[0], axis=0),
                             ec_units[:, :, :46]], axis=-1)
    all_dates = pd.DatetimeIndex(obs_dates + _dates(today, today + dt.timedelta(days=series.shape[-1] - len(obs_dates) - 1)))
    s = len(obs_dates)
    p_ec = probabilities(series, all_dates, s, today, hazards, horizons, units)
    forecasts, basis = {}, set()
    for (hazard, h), p in p_ec.items():
        p_bl, b = blend(p, None, hazard, h)
        basis.add(b)
        forecasts[(hazard, h)] = {"p_ec46": p, "p_ml": np.full_like(p, np.nan), "p_blend": p_bl,
                                  "level": levels(p_bl)}

    # 5. outlook + weather -----------------------------------------------------
    look = outlook(ec_units, today, cfg["weeks"],
                   {v: a[(today - ec_first).days:] for v, a in ec_mean_units.items()})
    wx = recent_weather(obs, pd.DatetimeIndex(obs_dates))
    t0 = sr_dates.index(today.isoformat()) if today.isoformat() in sr_dates else len(sr_dates) - 7
    wx.update({
        "temp_c": cur_units["temperature_2m"], "rh_pct": cur_units["relative_humidity_2m"],
        "soil_moisture": sr_units["soil_moisture_0_to_7cm_mean"][t0],
        "rain_next_24h": sr_units["precipitation_sum"][t0],
        "rain_next_7d": np.nansum(sr_units["precipitation_sum"][t0:t0 + 7], axis=0),
        "tmax_next_7d": np.nanmax(sr_units["temperature_2m_max"][t0:t0 + 7], axis=0),
        "rh_next_7d": np.nanmean(sr_units["relative_humidity_2m_mean"][t0:t0 + 7], axis=0),
        "obs_source": "imd" if latest_imd and (today - latest_imd).days <= 2 else "imd+om_adj"})

    # freshness -----------------------------------------------------------------
    limit = cfg["freshness"]["delayed_after_days"]
    ages = {"imd_observed": None if latest_imd is None else (today - latest_imd).days,
            "ec46": (today - ec_first).days,
            "short_range": (today - dt.date.fromisoformat(current["time"][:10])).days}
    delayed = any(a is None or a > limit for a in ages.values())
    sources = {"observed": counts, "latest_imd_day": latest_imd, "ages_days": ages,
               "ec46": {"model": cfg["ec46"]["model_members"], "run_day": ec_dates[0],
                        "points": int(len(g15["lat"]))},
               "short_range": {"points": int(len(g05["lat"])), "current": current["time"]},
               "blend_basis": sorted(basis) or ["none"], "ml": "coming soon (Stage 2)",
               "bias_ratio_median": float(np.median(ratio))}

    # 6. store --------------------------------------------------------------------
    size_mb = None
    run_id = None
    if not args.dry_run:
        from src.db.session import session_scope
        from src.jobs.weather import store as store_weather
        from src.live.store import db_size_mb, ensure_units, prune, write_obs, write_run

        with session_scope() as session:
            added = ensure_units(session, units)
            write_obs(session, unit_ids, obs_dates, obs, obs_sources, recent | {d for d in obs_dates
                                                                           if d not in stored})
            run_id = write_run(session, today=today, run_type="live", season=season,
                               hazards=hazards, sources=sources, calls=client.calls,
                               delayed=delayed, unit_ids=unit_ids, forecasts=forecasts,
                               outlook=look, weather=wx)
            pruned = prune(session, cfg["storage"], today)
        readings = national_weather(None, sr_daily, sr_dates, current, today)
        store_weather(readings, len(readings),
                      dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
        with session_scope() as session:
            size_mb = db_size_mb(session)
        print(f"     run {run_id}: {added} units added, {pruned} old runs pruned")

    warn = size_mb is not None and size_mb > cfg["storage"]["warn_mb"]
    n_rows = sum(int(np.isfinite(r["p_blend"]).sum()) for r in forecasts.values())
    high = {f"{hz} {h}d": int(sum(1 for lv in r["level"] if lv == "red"))
            for (hz, h), r in forecasts.items() if h == 7}
    lines = {
        "units": len(unit_ids), "hazards": ", ".join(hazards) or "none",
        "forecast values": n_rows, "high-risk units, 7 d": high,
        "observed days": counts, "latest IMD day": latest_imd,
        "input ages (days)": ages, "data delayed": delayed,
        "Open-Meteo calls (weighted)": round(client.calls, 1),
        "requests": client.requests, "blend": ", ".join(sorted(basis)) or "none",
        "DB size": f"{size_mb:.1f} MB" + (" - ABOVE 80% OF THE FREE TIER" if warn else "")
        if size_mb is not None else "(dry run)",
    }
    summarize("live.run", rows=n_rows, date_range=(obs_dates[0], all_dates[-1].date()),
              extra=lines)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"## Live run {today}{' - DATA DELAYED' if delayed else ''}\n\n")
            fh.write("| item | value |\n|---|---|\n")
            for k, v in lines.items():
                fh.write(f"| {k} | {json.dumps(v, default=str) if isinstance(v, dict) else v} |\n")
    if warn:
        print(f"WARNING: database is {size_mb:.0f} MB, above {cfg['storage']['warn_mb']} MB")
    if not n_rows and hazards:
        raise DataError("no forecast values produced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
