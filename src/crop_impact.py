"""Crop impact: for a unit and each of its main crops, which weather threat matters now.

Everything crop-specific lives in config/crop_vulnerability.yaml; stages come from
config/crop_calendar.yaml (DES sowing/harvest, derived middle stages); probabilities and
weather numbers come from the live run. This module only evaluates.

Level rules, per threat:
  * probability-driven threats use the forecast colour bands (medium >= 0.30, high > 0.60);
  * the observed trigger (e.g. 100 mm in 7 days) raises the level to at least medium, and
    to high when the forecast agrees;
  * the crop's `severity` caps the level: a "medium" damage never shows as high.

A crop with no threat reaching medium is reported as low, with a plain sentence.
"""

from __future__ import annotations

import datetime as dt
import functools
from dataclasses import dataclass, field

import yaml

from src.common import CONFIG_DIR

ORDER = {"low": 0, "medium": 1, "high": 2}
STAGE_ORDER = ["sowing", "vegetative", "flowering", "maturity", "harvest"]


@functools.lru_cache(maxsize=1)
def vulnerability() -> dict:
    return yaml.safe_load((CONFIG_DIR / "crop_vulnerability.yaml").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def translations() -> dict:
    return yaml.safe_load((CONFIG_DIR / "crop_vulnerability_i18n.yaml").read_text(encoding="utf-8"))


def _tr(lang: str) -> dict:
    return translations().get(lang, {}) if lang != "en" else {}


@functools.lru_cache(maxsize=1)
def calendar() -> dict:
    return yaml.safe_load((CONFIG_DIR / "crop_calendar.yaml").read_text(encoding="utf-8"))["crops"]


@dataclass
class Impact:
    crop: str
    crop_name: str
    stage: str | None
    level: str                              # low / medium / high
    threat: str | None = None
    label: str | None = None
    reason: str = ""
    action: str | None = None
    stage_basis: str | None = None          # sourced / derived / default
    notes: list[str] = field(default_factory=list)


def _in_window(day: dt.date, window: list[str]) -> bool:
    md = f"{day:%m-%d}"
    start, end = window
    return start <= md <= end if start <= end else (md >= start or md <= end)


def active_stages(crop: str, season: str, state: str, day: dt.date) -> tuple[list[str], str | None]:
    """Stages whose window contains `day` (they overlap), and the basis of that timing."""
    entry = calendar().get(crop, {}).get(season, {})
    chosen = entry.get(state) or entry.get("All India")
    if not chosen:
        return [], None
    stages = [s for s in STAGE_ORDER if s in chosen and _in_window(day, chosen[s]["window"])]
    basis = {chosen[s]["basis"] for s in stages} if stages else set()
    return stages, ("derived" if "derived" in basis else next(iter(basis), None))


def band(p: float | None) -> str:
    b = vulnerability()["bands"]
    if p is None or p != p:
        return "low"
    return "high" if p > b["high"] else "medium" if p >= b["medium"] else "low"


def _raise(level: str, to: str) -> str:
    return to if ORDER[to] > ORDER[level] else level


def _cap(level: str, severity: str) -> str:
    return "medium" if severity != "high" and level == "high" else level


def _pct(p: float | None) -> str:
    return "no" if p is None or p != p else f"{round(p * 100)}%"


def evaluate(threat_id: str, spec: dict, crop_spec: dict, stage: str, weather: dict,
             probs: dict, season: str) -> tuple[str, dict]:
    """(level, numbers used in the reason) for one threat on one crop."""
    level, nums = "low", {}
    prob = spec.get("probability")
    if prob:
        hazard = prob.get("post_monsoon_hazard") if season == "post_monsoon" and \
            prob.get("post_monsoon_hazard") else prob["hazard"]
        p = probs.get((hazard, prob["horizon"]))
        nums["p"] = p
        level = band(p)
    obs = spec.get("observed") or {}
    if "rain_7d_at_least_mm" in obs:
        r7 = weather.get("rain_7d")
        nums["rain_7d"] = r7
        if r7 is not None and r7 >= obs["rain_7d_at_least_mm"]:
            level = "high" if level != "low" else "medium"
    if "rain_14d_below_fraction_of_normal" in obs:
        r14, n14 = weather.get("rain_14d"), weather.get("normal_14d")
        nums.update(rain_14d=r14, normal_14d=n14)
        if r14 is not None and n14 and r14 < obs["rain_14d_below_fraction_of_normal"] * n14:
            level = "high" if level != "low" else "medium"
            nums["obs_low"] = True
    if "humidity" in spec:
        rh = weather.get("rh_next_7d")
        nums["rh"] = rh
        if rh is not None and rh >= spec["humidity"]["rh_next_7d_at_least"]:
            level = _raise(level, "medium")
    if "heat" in spec:
        limit = crop_spec.get("tmax_at_least_c", spec["heat"]["tmax_at_least_c"])
        tmax = weather.get("tmax_next_7d")
        nums["tmax"] = tmax
        if tmax is not None and tmax >= limit:
            level = _raise(level, "high" if tmax >= limit + 3 else "medium")
    if "soil" in spec:
        sm = weather.get("soil_moisture")
        if sm is not None and sm < spec["soil"]["soil_moisture_below"]:
            level = _raise(level, "medium")
    return _cap(level, crop_spec.get("severity", "medium")), nums


def reason_text(threat_id: str, spec: dict, crop: str, crop_spec: dict, crop_name: str,
                stage: str, nums: dict, lang: str = "en") -> str:
    v, tr = vulnerability(), _tr(lang)
    t_threat = (tr.get("threats") or {}).get(threat_id, {})
    t_crop = (tr.get("crops") or {}).get(crop, {})
    stage_words = (tr.get("stage_text") or {}).get(stage) or v["stage_text"].get(stage, stage)
    obs_text = ""
    if nums.get("obs_low"):
        template = t_threat.get("obs_text") or ("; only {rain_14d} mm fell in the last 2 weeks "
                                                "against a normal of {normal_14d} mm")
        obs_text = template.format(rain_14d=f"{nums['rain_14d']:.0f}",
                                   normal_14d=f"{nums['normal_14d']:.0f}")
    template = t_threat.get("reason") or spec["reason"]
    phrase = t_crop.get(threat_id)
    return template.format(
        p_pct=_pct(nums.get("p")), crop=crop_name, stage_text=stage_words, obs_text=obs_text,
        obs_rain_7d=f"{nums['rain_7d']:.0f}" if nums.get("rain_7d") is not None else "Little",
        damage=phrase or crop_spec.get("damage", ""),
        disease=phrase or crop_spec.get("disease", "disease"),
        rh=f"{nums['rh']:.0f}" if nums.get("rh") is not None else "high",
        tmax=f"{nums['tmax']:.0f}" if nums.get("tmax") is not None else "high")


def assess(state: str, tier: str, day: dt.date, crops: list[tuple[str, str]],
           weather: dict, probs: dict, season: str, lang: str = "en") -> list[Impact]:
    """One Impact per main crop: the most serious threat now, or a low-risk line."""
    v = vulnerability()
    out: list[Impact] = []
    for crop, crop_season in crops:
        cspec = v["crops"].get(crop)
        if not cspec:
            continue
        name = cspec["name"].get(lang, cspec["name"]["en"])
        stages, basis = active_stages(crop, crop_season, state, day)
        best: Impact | None = None
        for threat_id, spec in v["threats"].items():
            crop_threat = cspec.get(threat_id)
            if not crop_threat:
                continue
            allowed = crop_threat.get("stages", spec["stages"])
            hit = [s for s in stages if s in allowed]
            if not hit:
                continue
            stage = hit[-1]                              # most advanced matching stage
            level, nums = evaluate(threat_id, spec, crop_threat, stage, weather, probs, season)
            if level == "low":
                continue
            label = ((_tr(lang).get("threats") or {}).get(threat_id, {}).get("label")
                     or spec["label"])
            impact = Impact(crop=crop, crop_name=name, stage=stage, level=level,
                            threat=threat_id, label=label,
                            reason=reason_text(threat_id, spec, crop, crop_threat, name, stage,
                                               nums, lang),
                            action=spec.get("action"), stage_basis=basis)
            if best is None or ORDER[impact.level] > ORDER[best.level]:
                best = impact
        if best is None:
            stage = stages[-1] if stages else None
            tr = _tr(lang)
            words = ((tr.get("stage_text") or {}).get(stage) or v["stage_text"].get(stage, stage)
                     if stage else "")
            if tr.get("no_threat"):
                text = tr["no_threat"].format(crop=name, stage=words)
            else:
                text = (f"No weather threat to {name} expected in the next 2 weeks"
                        + (f" ({words})." if stage else "."))
            best = Impact(crop=crop, crop_name=name, stage=stage, level="low", reason=text,
                          stage_basis=basis)
        if tier != "validated":
            best.notes.append(_tr(lang).get("experimental_note") or v["experimental_note"])
        if basis == "default":
            best.notes.append("Crop timing is a general default for this crop.")
        out.append(best)
    return out
