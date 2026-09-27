"""Turn observations + EC46 + the short-range forecast into what the site shows.

All arrays are per unit in the order of app/assets/live/units.csv. Days run from the
season start (observed) through today+45 (EC46), so every event is judged on one
continuous series per ensemble member, with today at index `s` (CLAUDE.md §12).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import yaml

from src.advisory import risk_level
from src.common import CONFIG_DIR, load_config
from src.labels import (dry_spell, harvest_days, heavy_rain, late_heavy_rain, onset_forecast,
                        rabi_moisture_short, season_start_index)
from src.live import assets
from src.onset import onset_config
from src.withdrawal import detect_withdrawal, withdrawal_config


def season_for(day: dt.date) -> tuple[str, list[str]]:
    md = f"{day:%m-%d}"
    cfg = load_config("season")
    until = cfg.get("hazard_until") or {}
    for name, spec in cfg["seasons"].items():
        if spec["from"] <= md <= spec["to"]:
            return name, [h for h in spec["hazards"] if md <= until.get(h, "12-31")]
    return "off_season", []


def to_units(W, values: np.ndarray) -> np.ndarray:
    """(…, points) -> (…, units) with the sparse area weights; NaN points count as 0,
    which is safe because every unit's weights come from points that exist."""
    flat = np.nan_to_num(values.reshape(-1, values.shape[-1]), nan=0.0)
    out = (W @ flat.T).T
    return out.reshape(values.shape[:-1] + (W.shape[0],)).astype(np.float32)


def daily_normal(doys: np.ndarray) -> np.ndarray:
    """(units, len(doys)) training-years mean daily rain (weekly climatology / 7)."""
    mean = assets.climatology()["mean"]                      # (weeks, units)
    idx = np.array([assets.week_index(int(d)) for d in doys])
    return (mean[idx] / 7.0).T


def unit_onset_configs(units: pd.DataFrame) -> list:
    narp = pd.read_csv(CONFIG_DIR / "zones_narp.csv")
    zone = {(r.state, r.district): r.zone_id for r in narp.itertuples() if isinstance(r.zone_id, str)}
    return [onset_config(zone_id=zone.get((u.state, u.district)), state=u.state)
            for u in units.itertuples()]


def harvest_mask(units: pd.DataFrame, dates: pd.DatetimeIndex) -> np.ndarray:
    """(units, days) True on days inside a main kharif crop's harvest window."""
    cal = yaml.safe_load((CONFIG_DIR / "crop_calendar.yaml").read_text(encoding="utf-8"))["crops"]
    season = load_config("labels")["late_heavy"]["season"]
    crops = assets.district_crops()
    cache: dict = {}
    out = np.zeros((len(units), len(dates)), dtype=bool)
    for i, u in enumerate(units.itertuples()):
        key = (u.state, tuple((crops.get(f"{u.state}|{u.district}") or {}).get(season, [])))
        if key not in cache:
            windows = []
            for crop in key[1]:
                entry = cal.get(crop, {}).get(season, {})
                chosen = entry.get(u.state) or entry.get("All India")
                if chosen:
                    windows.append(tuple(chosen["harvest"]["window"]))
            cache[key] = harvest_days(dates, windows) if windows else np.zeros(len(dates), bool)
        out[i] = cache[key]
    return out


def probabilities(series: np.ndarray, dates: pd.DatetimeIndex, s: int, today: dt.date,
                  hazards: list[str], horizons: list[int], units: pd.DataFrame
                  ) -> dict[tuple[str, int], np.ndarray]:
    """{(hazard, horizon): p_ec46 (units,)} - fraction of members with the event; NaN
    where the event cannot be judged (e.g. onset already happened)."""
    out: dict[tuple[str, int], np.ndarray] = {}
    n_members, n_units, _ = series.shape

    def member_mean(events: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return np.where(np.isnan(events).all(axis=0), np.nan, np.nanmean(events, axis=0))

    for h in horizons:
        if "dry" in hazards:
            out[("dry", h)] = member_mean(dry_spell(series, s, h))
        if "heavy" in hazards:
            out[("heavy", h)] = member_mean(heavy_rain(series, s, h))

    if "late_heavy" in hazards:
        mask = harvest_mask(units, dates)
        for h in horizons:
            out[("late_heavy", h)] = member_mean(late_heavy_rain(series, s, h, mask[None]))

    if "rabi_moisture" in hazards:
        start = season_start_index(dates, today)
        normal = daily_normal(dates.dayofyear.to_numpy())
        for h in horizons:
            total = normal[:, start:s + h].sum(axis=1)
            out[("rabi_moisture", h)] = member_mean(rabi_moisture_short(series, s, h, start, total))

    if "onset" in hazards or "withdrawal" in hazards:
        cfgs = unit_onset_configs(units)
        for h in horizons:
            for hz in ("onset", "withdrawal"):
                if hz in hazards:
                    out[(hz, h)] = np.full(n_units, np.nan, dtype=np.float32)
        last_day = {h: dates[min(s + h, len(dates)) - 1] for h in horizons}
        for u in range(n_units):
            obs = series[0, u, :s]
            if np.isnan(obs).all():
                continue
            if "onset" in hazards:
                hits = {h: 0 for h in horizons}
                already = False
                for m in range(n_members):
                    already, when = onset_forecast(series[m, u], dates, s, cfgs[u])
                    if already:
                        break
                    for h in horizons:
                        hits[h] += int(when is not None and dates[s] <= when <= last_day[h])
                if not already:
                    for h in horizons:
                        out[("onset", h)][u] = hits[h] / n_members
            if "withdrawal" in hazards:
                from src.onset import detect_onset

                onset = detect_onset(obs, dates[:s], cfgs[u])
                if onset is None:
                    continue
                wcfg = withdrawal_config(state=units.iloc[u]["state"])
                if detect_withdrawal(obs, dates[:s], wcfg, onset) is not None:
                    continue
                hits = {h: 0 for h in horizons}
                for m in range(n_members):
                    got = detect_withdrawal(series[m, u], dates, wcfg, onset)
                    for h in horizons:
                        hits[h] += int(got is not None and dates[s] <= got <= last_day[h])
                for h in horizons:
                    out[("withdrawal", h)][u] = hits[h] / n_members
    return out


def blend(p_ec46: np.ndarray, p_ml: np.ndarray | None, hazard: str, horizon: int
          ) -> tuple[np.ndarray, str]:
    """w * ML + (1 - w) * EC46 per config/blend.yaml; EC46 alone while ML is absent."""
    if p_ml is None:
        return p_ec46, "ec46_only"
    cfg = load_config("blend")
    w = float((cfg.get("weights") or {}).get(hazard, {}).get(horizon, cfg["default"]))
    both = np.isfinite(p_ml) & np.isfinite(p_ec46)
    return np.where(both, w * p_ml + (1 - w) * p_ec46,
                    np.where(np.isfinite(p_ml), p_ml, p_ec46)), "blend"


def levels(p: np.ndarray) -> list[str]:
    return ["none" if not np.isfinite(v) else risk_level(float(min(max(v, 0.0), 1.0)))
            for v in p]


def outlook(ec_units: np.ndarray, today: dt.date, weeks: int,
            ec_mean_units: dict | None = None) -> dict[str, np.ndarray]:
    """Weekly totals (weeks, units): p10/p50/p90 across members, the training-years
    normal, and the share of members below / near / above the normal terciles."""
    clim = assets.climatology()
    res = {k: [] for k in ("p10", "p50", "p90", "normal", "below", "near", "above")}
    for k in range(weeks):
        totals = np.nansum(ec_units[:, :, 7 * k:7 * k + 7], axis=-1)       # (members, units)
        wi = assets.week_index((today + dt.timedelta(days=7 * k)).timetuple().tm_yday)
        p33, p67 = clim["p33"][wi], clim["p67"][wi]
        res["p10"].append(np.percentile(totals, 10, axis=0))
        res["p50"].append(np.percentile(totals, 50, axis=0))
        res["p90"].append(np.percentile(totals, 90, axis=0))
        res["normal"].append(clim["mean"][wi])
        res["below"].append((totals < p33).mean(axis=0))
        res["above"].append((totals > p67).mean(axis=0))
        res["near"].append(1 - res["below"][-1] - res["above"][-1])
        for key, var in (("t_mean", "temperature_2m_mean"), ("rh_mean", "relative_humidity_2m_mean"),
                         ("soil_mean", "soil_moisture_0_to_7cm_mean")):
            block = (ec_mean_units or {}).get(var)            # (days, units)
            res.setdefault(key, []).append(
                np.nanmean(block[7 * k:7 * k + 7], axis=0) if block is not None
                else np.full(ec_units.shape[1], np.nan))
    return {k: np.array(v, dtype=np.float32) for k, v in res.items()}


def recent_weather(obs: np.ndarray, obs_dates: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    def total(days: int) -> np.ndarray:
        window = obs[:, -days:]
        with np.errstate(invalid="ignore"):
            return np.where(np.isnan(window).all(axis=1), np.nan, np.nansum(window, axis=1))

    normal = daily_normal(obs_dates.dayofyear.to_numpy())
    return {"rain_7d": total(7), "rain_14d": total(14),
            "normal_7d": normal[:, -7:].sum(axis=1), "normal_14d": normal[:, -14:].sum(axis=1)}
