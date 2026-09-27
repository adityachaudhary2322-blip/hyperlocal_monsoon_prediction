"""Rule-based crop advisory from hazard probabilities.

Run:  python -m src.advisory --demo
      python -m src.advisory --unit <unit_id> --date 2026-07-04

Every threshold, sowing window, action text and tie-break lives in config/rules.yaml;
this module only evaluates them. That is the same rule as config/onset.yaml - the
agronomy is data, not code, so it can be replaced when the agronomy notes arrive
without touching this file.

What the engine guarantees
--------------------------
* **One field operation per advisory.** A farmer cannot both irrigate and drain the
  same plot, so when rules with opposing operations match, `tie_break.priority`
  picks exactly one and the loser becomes a watch note, never a second instruction.
* **Conflicting hazards are surfaced, not averaged.** If dry and heavy are both in
  the red band the forecast is contradicting itself at that lead time; the advisory
  is issued at low confidence and says so.
* **Confidence never exceeds what the source supports.** A district with no sourced
  NARP zone is capped at low confidence, because its sowing window is a fallback.
* Marathi is deliberately `None` everywhere until Phase 6; `render()` reports the
  missing language rather than silently falling back to English.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from src.common import CONFIG_DIR, load_config
from src.report import DataError

RULES_NAME = "rules"
LEVELS = ("green", "amber", "red")
HAZARDS = ("onset", "dry", "heavy")
HORIZONS = (7, 14, 21, 28)
LANGUAGES = ("en", "hi", "mr")


# --------------------------------------------------------------------------
# Risk bands
# --------------------------------------------------------------------------
def risk_level(probability: float, cfg: dict | None = None) -> str:
    """green / amber / red, per the bands in rules.yaml.

    The bands are green < 0.30, amber 0.30-0.60 inclusive, red > 0.60. The
    boundaries belong to amber on both sides, so a value is never unclassified.
    """
    if probability is None or probability != probability:  # NaN
        return "unknown"
    if not 0.0 <= probability <= 1.0:
        raise DataError(f"probability out of range: {probability!r}")
    bands = (cfg or load_config(RULES_NAME))["risk_bands"]
    if probability < bands["green"]["below"]:
        return "green"
    if probability > bands["red"]["above"]:
        return "red"
    return "amber"


# --------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------
def zone_window(zone: str | None, crop: str | None = None,
                cfg: dict | None = None) -> dict:
    """Sowing window for a zone (and optionally a specific crop)."""
    cfg = cfg or load_config(RULES_NAME)
    windows = cfg["sowing_windows"]
    key = zone if zone in windows else cfg["meta"]["default_zone"]
    entry = windows[key]
    window = dict(entry["default"])
    if crop and crop in (entry.get("crops") or {}):
        window = dict(entry["crops"][crop])
    return {
        "zone_key": key,
        "start": window["start"],
        "end": window["end"],
        "confidence": entry.get("confidence", "low"),
        "source": entry.get("source"),
        "normal_onset": entry.get("normal_onset"),
        "is_fallback": key != zone,
    }


def derive_stage(date: dt.date, zone: str | None, crop: str | None = None,
                 cfg: dict | None = None) -> str:
    """pre_sowing / sowing_window / early_vegetative / flowering / maturity.

    Stages are counted from the CLOSE of the sowing window, not its start: the
    farmer who sowed on the last valid day is the one whose crop is youngest and
    most exposed, so anchoring there is the conservative choice.
    """
    cfg = cfg or load_config(RULES_NAME)
    window = zone_window(zone, crop, cfg)
    offsets = cfg["stage_offsets_days"]

    start = dt.date(date.year, *map(int, window["start"].split("-")))
    end = dt.date(date.year, *map(int, window["end"].split("-")))

    if date < start:
        return "pre_sowing"
    if date <= end:
        return "sowing_window"
    if date <= end + dt.timedelta(days=offsets["early_vegetative_until"]):
        return "early_vegetative"
    if date <= end + dt.timedelta(days=offsets["flowering_until"]):
        return "flowering"
    return "maturity"


def crop_phrase(crops: Sequence[str], language: str,
                cfg: dict | None = None) -> str:
    """The crop list as it appears in a message, capped and in the right language.

    Two reasons this is not just ", ".join(): Uttar Pradesh carries nine crops, which
    costs 66 characters of a 300-character SMS and reads like a catalogue; and the
    names themselves have to be in the message's own script, or a Hindi advisory ends
    up saying "soybean, cotton" in Latin letters mid-sentence.
    """
    cfg = cfg or load_config(RULES_NAME)
    if not crops:
        return {"en": "your kharif crops", "hi": "आपकी खरीफ फसलों",
                "mr": "तुमच्या खरीप पिकांना"}.get(language, "your kharif crops")

    names = cfg.get("crop_names") or {}
    localised = [(names.get(c) or {}).get(language) or c for c in crops]

    limit = cfg.get("max_crops_in_text") or len(localised)
    if len(localised) > limit:
        etc = (cfg.get("crops_etc") or {}).get(language)
        shown = ", ".join(localised[:limit])
        return f"{shown} {etc}" if etc else shown
    return ", ".join(localised)


def crops_for(state: str, zone: str | None, cfg: dict | None = None) -> list[str]:
    """Crops carried in this state, minus any barred from this zone."""
    cfg = cfg or load_config(RULES_NAME)
    crops = list(cfg["crops"].get(state, []))
    restrictions = cfg.get("crop_zone_restrictions") or {}
    return [c for c in crops
            if c not in restrictions or zone in restrictions[c]]


# --------------------------------------------------------------------------
# Rule matching
# --------------------------------------------------------------------------
def _wanted_levels(spec: Any) -> set[str]:
    return {spec} if isinstance(spec, str) else set(spec)


def _matches(rule: dict, *, stage: str, state: str, levels: dict[str, str],
             after_window: bool, onset_happened: bool | None) -> bool:
    when = rule["when"]
    if "stage" in when and stage not in _wanted_levels(when["stage"]):
        return False
    if "state_in" in when and state not in when["state_in"]:
        return False
    if "after_window" in when and bool(when["after_window"]) is not after_window:
        return False
    if "onset_happened" in when:
        # Unknown is not a match: recommending a variety switch or a sowing delay
        # without knowing whether the crop is already in the ground is worse than
        # staying silent.
        if onset_happened is None or bool(when["onset_happened"]) is not onset_happened:
            return False
    for key, spec in when.items():
        if key in ("stage", "state_in", "after_window", "onset_happened"):
            continue
        if key not in levels:
            return False
        if levels[key] not in _wanted_levels(spec):
            return False
    return True


@dataclass
class Advisory:
    unit_id: str | None
    date: dt.date
    state: str
    zone: str | None
    zone_used: str
    stage: str
    crops: list[str]
    risk: dict[str, str]                 # "onset_7" -> "amber"
    probabilities: dict[str, float]
    action: str
    confidence: str
    text: dict[str, str | None]          # en / hi / mr
    sources: list[str]
    source_notes: list[str]
    matched_rules: list[str] = field(default_factory=list)
    conflict: dict | None = None
    notes: list[str] = field(default_factory=list)

    def render(self, language: str = "en") -> str:
        if language not in LANGUAGES:
            raise DataError(f"unsupported language {language!r}; "
                            f"expected one of {LANGUAGES}")
        body = self.text.get(language)
        if body is None:
            raise DataError(
                f"no {language!r} text for action {self.action}: Marathi is filled in "
                "Phase 6 and must not fall back to English silently"
            )
        return body


def _lowest(confidences: Iterable[str]) -> str:
    order = ["low", "medium", "high"]
    present = [c for c in confidences if c in order]
    return min(present, key=order.index) if present else "low"


def advise(probabilities: dict[str, float], *, state: str, zone: str | None,
           date: dt.date, unit_id: str | None = None,
           crops: Sequence[str] | None = None,
           onset_happened: bool | None = None,
           cfg: dict | None = None) -> Advisory:
    """Build one advisory for a unit on a date.

    `probabilities` keys are "<hazard>_<horizon>", e.g. "dry_14".
    `onset_happened` gates the sowing rules; leave it None when unknown and those
    rules stay silent rather than guess.
    """
    cfg = cfg or load_config(RULES_NAME)

    missing = [f"{h}_{k}" for h in HAZARDS for k in HORIZONS
               if f"{h}_{k}" not in probabilities]
    if missing:
        raise DataError(f"missing probabilities: {missing}")

    levels = {key: risk_level(value, cfg) for key, value in probabilities.items()}
    window = zone_window(zone, None, cfg)
    stage = derive_stage(date, zone, None, cfg)
    end = dt.date(date.year, *map(int, window["end"].split("-")))
    after_window = date > end
    selected = list(crops) if crops else crops_for(state, zone, cfg)

    matched = [r for r in cfg["rules"]
               if _matches(r, stage=stage, state=state, levels=levels,
                           after_window=after_window,
                           onset_happened=onset_happened)]

    tie = cfg["tie_break"]
    priority = tie["priority"]
    notes: list[str] = []
    conflict = None

    # Conflicting hazards: both red at the same lead time. Detected BEFORE the
    # action is chosen, because the resolution decides which hazard the advisory is
    # allowed to act on - resolving it afterwards would leave the engine announcing
    # "dry wins" while issuing the drainage instruction.
    rule = tie["conflicting_hazards"]
    for first, second in rule["pairs"]:
        for horizon in HORIZONS:
            a, b = f"{first}_{horizon}", f"{second}_{horizon}"
            if levels.get(a) == "red" and levels.get(b) == "red":
                pa, pb = probabilities[a], probabilities[b]
                if pa > pb:
                    winner, loser = first, second
                elif pb > pa:
                    winner, loser = second, first
                else:
                    winner = rule["tie_goes_to"]
                    loser = first if winner == second else second
                conflict = {
                    "horizon": horizon,
                    "hazards": [first, second],
                    "primary": winner,
                    "secondary": loser,
                    "secondary_probability": probabilities[f"{loser}_{horizon}"],
                }
                break
        if conflict:
            break

    candidates = matched
    if conflict:
        contested = set(conflict["hazards"])
        # Keep rules that either act on the winning hazard or have nothing to do
        # with the contest at all (a flood warning is not part of a dry-vs-heavy
        # argument about irrigation).
        filtered = [
            r for r in matched
            if cfg["actions"][r["action"]].get("category") not in contested
            or cfg["actions"][r["action"]].get("category") == conflict["primary"]
        ]
        if filtered:
            candidates = filtered

    if candidates:
        chosen_rule = min(candidates, key=lambda r: priority.index(r["action"]))
        action_code = chosen_rule["action"]
    else:
        chosen_rule = None
        action_code = "MONITOR"

    action = cfg["actions"][action_code]
    confidences = [action.get("confidence", "low")]
    if conflict:
        confidences.append(rule["downgrade_confidence_to"])
        notes.append(
            f"{conflict['hazards'][0]} and {conflict['hazards'][1]} are both in the "
            f"red band at {conflict['horizon']} days; acting on "
            f"{conflict['primary']} (the higher probability) and reporting "
            f"{conflict['secondary']} as a watch."
        )

    if window["is_fallback"]:
        confidences.append(tie["unzoned_confidence_ceiling"])
        notes.append(
            f"District has no sourced NARP zone; using the {window['zone_key']} "
            "fallback sowing window, so this advisory is low confidence."
        )
    confidences.append(window["confidence"])

    text: dict[str, str | None] = {}
    for language in LANGUAGES:
        body = action.get(language)
        if body is not None:
            body = body.strip().replace("{crops}", crop_phrase(selected, language, cfg))
            if conflict:
                extra = rule.get(f"secondary_note_{language}")
                if extra:
                    pct = 100 * conflict["secondary_probability"]
                    body = body + " " + extra.strip().replace(
                        "{other}", conflict["secondary"]
                    ).replace("{other_pct}", f"{pct:.0f}")
        text[language] = body

    sources = [s for s in (action.get("source"), window.get("source")) if s]
    source_notes = [n for n in (action.get("source_note"),
                                window.get("source_note")) if n]

    return Advisory(
        unit_id=unit_id, date=date, state=state, zone=zone,
        zone_used=window["zone_key"], stage=stage, crops=selected,
        risk=levels, probabilities=dict(probabilities),
        action=action_code, confidence=_lowest(confidences), text=text,
        sources=sources, source_notes=source_notes,
        matched_rules=[r["id"] for r in matched],
        conflict=conflict, notes=notes,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
DEMO = [
    ("Bihar", "BI-2", "2026-07-19",
     {"heavy_14": 0.82, "heavy_7": 0.71, "dry_14": 0.10}, True),
    ("Maharashtra", "MH-6/MH-7", "2026-08-08",
     {"dry_14": 0.74, "heavy_7": 0.05}, True),
    ("Madhya Pradesh", "MP-10", "2026-06-19",
     {"onset_7": 0.78, "onset_14": 0.88}, False),
    ("Uttar Pradesh", "UP-10", "2026-06-19",
     {"onset_7": 0.12, "onset_14": 0.18}, False),
    # Both hazards red at 14 days: the tie-break has to pick one field operation.
    ("Maharashtra", "MH-6/MH-7", "2026-08-08",
     {"dry_14": 0.72, "heavy_14": 0.66, "heavy_7": 0.64}, True),
]


def _safe(text: str) -> str:
    """Windows consoles are cp1252 and will raise on Devanagari."""
    import sys

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, "replace").decode(encoding)


def _demo_probabilities(overrides: dict) -> dict[str, float]:
    base = {f"{h}_{k}": 0.05 for h in HAZARDS for k in HORIZONS}
    base.update(overrides)
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true",
                        help="run one worked example per state")
    parser.add_argument("--language", choices=LANGUAGES, default="en")
    args = parser.parse_args()

    cfg = load_config(RULES_NAME)
    print("--- advisory ---")
    print(f"  rules: {CONFIG_DIR / 'rules.yaml'} (version {cfg['meta']['version']})")
    print(f"  actions: {len(cfg['actions'])}, rules: {len(cfg['rules'])}, "
          f"zones: {len(cfg['sowing_windows'])}")

    if not args.demo:
        print("\n  nothing to do; pass --demo for worked examples")
        return 0

    for state, zone, date_text, overrides, happened in DEMO:
        date = dt.date.fromisoformat(date_text)
        result = advise(_demo_probabilities(overrides), state=state, zone=zone,
                        date=date, onset_happened=happened, cfg=cfg)
        print(f"\n  {state} / {zone} / {date}")
        print(f"    stage      : {result.stage}")
        print(f"    action     : {result.action}  (confidence {result.confidence})")
        print(f"    matched    : {result.matched_rules or ['-']}")
        bands = {k: v for k, v in result.risk.items() if v in ("amber", "red")}
        print(f"    amber/red  : {bands or '-'}")
        if result.conflict:
            print(f"    CONFLICT   : {result.conflict['hazards']} both red at "
                  f"{result.conflict['horizon']}d -> "
                  f"{result.conflict['primary']} wins")
        body = result.render(args.language)
        print(f"    text [{args.language}]  : {_safe(body)[:220]}")
        for note in result.notes:
            print(f"    note       : {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
