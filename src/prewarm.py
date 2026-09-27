"""Pre-generate and cache advisory text so the demo survives losing the internet.

Run:  python -m src.prewarm [--languages en,hi,mr] [--dry-run]

Walks every pilot unit x action x probability bucket, puts each result through the
provider chain, and leaves it in the SQLite cache. After this runs, a live demo can
be switched to `offline_mode` - or simply lose the network - and still produce the
same text for every advisory it shows, because nothing needs a provider on a cache
hit.

Marathi is only warmed once rules.yaml carries Marathi text: the template provider
cannot translate, so asking for `mr` before the pre-translation step just burns API
calls. The script says so rather than silently producing English.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools

import geopandas as gpd

from src.advisory import LANGUAGES, advise, HAZARDS, HORIZONS
from src.common import UNITS_GPKG, load_config, pilot_districts
from src.llm import cache as cache_module
from src.llm.providers import build_chain
from src.report import DataError, require_nonempty, summarize

# One representative date per stage, so every stage-dependent rule gets exercised.
SAMPLE_DATES = ("2026-06-04", "2026-06-24", "2026-07-24", "2026-09-07")
# Bucket centres: green, amber, red. Matches the cache's 0.05 bucketing.
SAMPLE_PROBABILITIES = (0.10, 0.45, 0.75)


def pilot_units() -> gpd.GeoDataFrame:
    if not UNITS_GPKG.is_file():
        raise DataError(f"missing {UNITS_GPKG}; run python -m src.build_units first")
    units = gpd.read_file(UNITS_GPKG, layer="units", ignore_geometry=True)
    keep = []
    for state, districts in pilot_districts().items():
        keep.append(units[(units["state"] == state) &
                          units["district"].isin(districts)])
    import pandas as pd

    out = pd.concat(keep, ignore_index=True)
    require_nonempty(out, "pilot units")
    return out


def state_languages(state: str) -> list[str]:
    """Languages actually used for a state, from config/project.yaml."""
    return list(load_config("project")["languages"].get(state, ["en"]))


def advisory_payloads(units, languages: list[str], rules_cfg: dict):
    """Every (unit, date, hazard, probability) combination worth caching."""
    for row in units.itertuples():
        for date_text, hazard, probability in itertools.product(
            SAMPLE_DATES, HAZARDS, SAMPLE_PROBABILITIES
        ):
            probabilities = {f"{h}_{k}": 0.05 for h in HAZARDS for k in HORIZONS}
            for horizon in HORIZONS:
                probabilities[f"{hazard}_{horizon}"] = probability

            result = advise(
                probabilities, state=row.state, zone=row.zone_id,
                date=dt.date.fromisoformat(date_text), unit_id=row.unit_id,
                onset_happened=(hazard != "onset"),
            )
            action = rules_cfg["actions"][result.action]
            # Only the languages this state actually sends in. Warming Marathi for a
            # Bihar advisory burns a call on a message that will never be sent:
            # config/project.yaml puts Bihar on Hindi and English.
            for language in [l for l in languages if l in state_languages(row.state)]:
                template_text = result.text.get(language)
                if not template_text:
                    continue
                yield {
                    "template_text": template_text,
                    "unit_name": row.unit_name,
                    "district": row.district,
                    "state": row.state,
                    "crops": ", ".join(result.crops),
                    "action": result.action,
                    # Per-language: an English verb cannot appear in Devanagari.
                    "action_keyword": (action.get("keywords") or {}).get(language),
                    "probability": probability,
                    "horizon_days": 14,
                    "risk_level": result.risk.get(f"{hazard}_14"),
                    "stage": result.stage,
                    "language": language,
                }, language


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", default="en,hi")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    languages = [l.strip() for l in args.languages.split(",") if l.strip()]
    unknown = [l for l in languages if l not in LANGUAGES]
    if unknown:
        raise DataError(f"unknown language(s) {unknown}; expected {list(LANGUAGES)}")

    rules_cfg = load_config("rules")
    llm_cfg = load_config("llm")

    if "mr" in languages and not any(a.get("mr") for a in rules_cfg["actions"].values()):
        raise DataError(
            "rules.yaml has no Marathi text yet, so warming `mr` would call the API "
            "for every advisory and still fail validation. Run the Marathi "
            "pre-translation first, then re-run with --languages en,hi,mr."
        )

    print("--- prewarm ---")
    print(f"  languages : {languages}")
    print(f"  providers : {llm_cfg['fallback_order']} "
          f"(offline_mode={llm_cfg['offline_mode']})")
    print(f"  cache     : {cache_module.cache_path(llm_cfg)}")

    units = pilot_units()
    print(f"  units     : {len(units)} across "
          f"{units['district'].nunique()} pilot districts")

    chain = build_chain(llm_cfg)
    seen: set[str] = set()
    generated = 0
    hits = 0
    failures: list[str] = []

    for payload, language in advisory_payloads(units, languages, rules_cfg):
        key, _ = cache_module.make_key(
            kind="advisory", language=language, action=payload["action"],
            crop=payload["crops"], probability=payload["probability"], cfg=llm_cfg,
        )
        if key in seen:
            continue
        seen.add(key)
        if args.dry_run:
            continue
        try:
            result = chain.generate_advisory_text(payload, language)
        except Exception as exc:  # noqa: BLE001 - one bad combination must not stop the warm
            failures.append(f"{payload['action']}/{language}: {exc}")
            continue
        generated += 1
        hits += int(result.cache_hit)

    by_provider: dict[str, int] = {}
    for row in cache_module.daily_totals(cfg=llm_cfg):
        by_provider[row["provider"]] = by_provider.get(row["provider"], 0) + row["calls"]

    if failures:
        print(f"\n  {len(failures)} combination(s) failed:")
        for line in failures[:10]:
            print(f"      - {line}")

    summarize(
        "prewarm",
        rows=generated,
        files=[cache_module.cache_path(llm_cfg)],
        extra={
            "distinct cache keys": len(seen),
            "cache hits": hits,
            "failures": len(failures),
            "calls by provider": by_provider or "-",
            "dry run": args.dry_run,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
