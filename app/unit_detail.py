"""Everything shown for ONE area, in the viewer's language, from the live tables.

Used by the map's side panel (sent to the component as `data.detail`) and by the
"Next 30 days" page, so both say exactly the same thing. Only public data: live tables
through app/public_data.py and approved advisory text.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse

import numpy as np
import pandas as pd

from app import public_data as pdata
from app.i18n import span, t
from src.common import ROOT
from src.crop_impact import assess
from src.tiers import tier_on

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CROPS_FILE = ROOT / "app" / "assets" / "live" / "district_crops.json"
MAX_CROPS = 6


def fmt_ist(value: dt.datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(IST).strftime("%d %b %Y, %H:%M IST").lstrip("0")


def pct(p: float | None) -> str:
    return "-" if p is None or not np.isfinite(p) else f"{round(p * 100)}%"


def _district_crops() -> dict:
    return json.loads(CROPS_FILE.read_text(encoding="utf-8")) if CROPS_FILE.is_file() else {}


def share_link(base_url: str, unit_id: str, name: str, district: str, lang: str) -> str:
    url = f"{base_url.split('?')[0]}?{urllib.parse.urlencode({'unit': unit_id})}"
    text = t("share_text", lang, name=name, district=district, url=url)
    return "https://wa.me/?" + urllib.parse.urlencode({"text": text})


def build(unit_id: str, lang: str, base_url: str = "") -> dict | None:
    run = pdata.live_run()
    if run is None:
        return None
    units = pdata.live_units()
    match = units[units["unit_id"] == unit_id]
    if match.empty:
        return None
    u = match.iloc[0]
    rows = pdata.live_unit_rows(run["id"], unit_id)
    fc: pd.DataFrame = rows["forecasts"]
    tier = tier_on(u["tier"], bool(u["seasonal_low"]), run["run_date"])
    name = u["unit_name"] if u["name_ok"] else t("unnamed_area", lang)

    probs: dict = {}
    for r in fc.itertuples():
        if not pd.isna(r.p_blend):
            probs[(r.hazard, int(r.horizon))] = r.p_blend / 1000

    weeks = []
    for k in range(1, 5):
        h = 7 * k
        items = []
        for hz in run["hazards"]:
            p = probs.get((hz, h))
            if p is None:
                continue
            level = fc[(fc["hazard"] == hz) & (fc["horizon"] == h)]["level"].iloc[0]
            items.append({"hazard": hz, "label": t(f"hazard_{hz}", lang), "p": p,
                          "level": level, "level_text": t(f"level_{_lvl(level)}", lang),
                          "sentence": t(f"sentence_{hz}", lang, p=pct(p), span=span(k, lang))})
        weeks.append({"week": k, "title": t("week_card", lang, n=k), "items": items})

    outlook, parts = [], []
    for r in rows["outlook"].itertuples():
        below, above = (r.p_below or 0) / 1000, (r.p_above or 0) / 1000
        if above >= 0.5:
            sentence = t("outlook_above", lang, n=r.week, p=pct(above))
        elif below >= 0.5:
            sentence = t("outlook_below", lang, n=r.week, p=pct(below))
        else:
            sentence = t("outlook_near", lang, n=r.week)
        parts.append(sentence)
        outlook.append({"week": int(r.week), "p10": _f(r.p10), "p50": _f(r.p50), "p90": _f(r.p90),
                        "normal": _f(r.normal), "below": below, "above": above,
                        "t_mean": _f(r.t_mean), "rh_mean": _f(r.rh_mean)})

    w = rows["weather"]
    weather = {k: _f(w.get(k)) for k in ("rain_7d", "rain_14d", "normal_7d", "normal_14d",
                                          "temp_c", "rh_pct", "soil_moisture", "rain_next_24h",
                                          "rain_next_7d", "tmax_next_7d", "rh_next_7d")}

    crops_info = _district_crops().get(f"{u['state']}|{u['district']}", {})
    season = "kharif" if run["run_date"].month in range(4, 11) else "rabi"
    main = [(c, season) for c in crops_info.get(season, [])]
    impacts = assess(u["state"], tier, run["run_date"], main,
                     {k: v for k, v in weather.items() if v is not None}, probs,
                     run["season"], lang)
    order = {"high": 0, "medium": 1, "low": 2}
    crops = sorted(({"crop": i.crop, "name": i.crop_name, "level": i.level,
                     "level_text": t(f"level_{i.level}", lang), "label": i.label,
                     "reason": i.reason, "notes": i.notes, "basis": i.stage_basis}
                    for i in impacts), key=lambda c: order[c["level"]])
    more = max(0, len(crops) - MAX_CROPS)
    crops = crops[:MAX_CROPS]                  # highest risk first; the rest are counted
    source = crops_info.get("source", "")
    crops_note = t("crops_source_district", lang) if source == "district statistics" \
        else t("crops_source_state", lang)

    advisory = pdata.approved_advisories_by_unit(run["run_date"]).get(unit_id)
    return {
        "unit_id": unit_id, "name": name, "district": u["district"], "state": u["state"],
        "tier": tier, "tier_text": t(f"tier_{tier}", lang), "tier_help": t(f"tier_help_{tier}", lang),
        "updated": t("updated", lang, time=fmt_ist(run["created_utc"])),
        "delayed": run["data_delayed"], "delayed_text": t("data_delayed", lang),
        "weeks": weeks, "outlook": outlook,
        "month_sentence": t("outlook_month", lang, text=" ".join(parts)) if parts else "",
        "weather": weather, "crops": crops,
        "crops_note": crops_note + (f" (+{more})" if more else ""),
        "advisory": advisory, "source_line": t("source_line", lang),
        "ml_note": t("ml_coming_soon", lang) if "ec46_only" in run["sources"].get("blend_basis", []) else "",
        "share": share_link(base_url, unit_id, name, u["district"], lang) if base_url else "",
        "has_forecast": bool(probs),
    }


def _lvl(level: str) -> str:
    return {"green": "low", "amber": "medium", "red": "high"}.get(level, "none")


def _f(v):
    try:
        return None if v is None or not np.isfinite(float(v)) else round(float(v), 1)
    except (TypeError, ValueError):
        return None
