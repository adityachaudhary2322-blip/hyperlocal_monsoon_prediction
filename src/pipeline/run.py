"""Run a forecast for one date: predict, diff, advise, and queue for approval.

Run:  python -m src.pipeline.run --as-of 2024-06-20 --pilot-only

Steps
-----
1. Resolve the date. Forecasts are issued on a 5-day calendar (15 May + 5k), so an
   arbitrary date usually has no row. The nearest issued date is used instead and the
   substitution is recorded on the run and printed - never silently.
2. Predict with the models already in models/lgbm/, using whichever model
   config/model_choice.yaml selected per target and horizon.
3. Diff against the previous run so an officer can see what moved.
4. Build advisories through src/advisory.py.
5. Apply config/alert_policy.yaml to decide pending_approval vs approved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json

import lightgbm as lgb
import numpy as np
import pandas as pd
import yaml

from src.advisory import LANGUAGES, advise
from src.baseline_common import HORIZONS, TARGETS
from src.common import CONFIG_DIR, load_config
from src.db.models import (
    Advisory as AdvisoryRow,
    AuditLog,
    Forecast,
    ForecastChange,
    ForecastRun,
    Unit,
)
from src.db.session import create_all, describe as describe_db, session_scope, use_url
from src.llm.providers import build_chain
from src.report import DataError, summarize

MODEL_CHOICE = CONFIG_DIR / "model_choice.yaml"
ALERT_POLICY = "alert_policy"
LGBM_DIR = None  # resolved lazily from baseline_common


def model_source():
    """The module that supplies the feature table and the predictions.

    Hosted, the models are the committed assets in `app/assets/models/` - there is no
    `models/` or `data/processed/` on Streamlit Cloud, and no scikit-learn to unpickle a
    calibrator with. Locally the trained artifacts win, and the committed assets are the
    fallback so `HOSTED_MODE=true` can be rehearsed on the laptop.

    Both paths were verified to agree bit-for-bit on 2024-06-19 (360 probabilities,
    max difference 0.0), so which one answers is an availability question, not a
    correctness one.
    """
    from src.config import flag

    from src import hosted_models
    from src.baseline_common import TRAIN_TABLE

    if flag("HOSTED_MODE") or not TRAIN_TABLE.is_file():
        if hosted_models.available():
            return hosted_models, "app/assets/models (committed)"
        if flag("HOSTED_MODE"):
            raise DataError(
                "HOSTED_MODE is on but the committed model assets are incomplete "
                f"({hosted_models.describe()}). Run python scripts/export_models.py.")
    # `baseline_common` supplies the table but not the prediction, which lives in
    # this module, so the local source is assembled from both.
    from types import SimpleNamespace

    import src.baseline_common as local

    return (SimpleNamespace(load_table=local.load_table,
                            feature_columns=local.feature_columns,
                            predict=predict),
            "models/lgbm (trained locally)")


def load_model_choice() -> dict:
    if not MODEL_CHOICE.is_file():
        return {}
    data = yaml.safe_load(MODEL_CHOICE.read_text(encoding="utf-8")) or {}
    return data.get("chosen") or {}


def resolve_date(frame: pd.DataFrame, requested: dt.date) -> tuple[dt.date, str | None]:
    """Nearest issued forecast date, and a note if it is not the one asked for."""
    issued = pd.DatetimeIndex(sorted(frame["start_date"].unique()))
    target = pd.Timestamp(requested)
    if target in issued:
        return requested, None

    nearest = issued[np.argmin(np.abs(issued - target))]
    gap = abs((nearest - target).days)
    note = (
        f"No forecast is issued on {requested:%Y-%m-%d}. Forecasts run every 5 days "
        f"from 15 May, so the nearest issued date, {nearest:%Y-%m-%d} "
        f"({gap} day{'s' if gap != 1 else ''} away), was used instead."
    )
    return nearest.date(), note


def predict(frame: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """Probabilities for every (unit, hazard, horizon) on this date.

    The locally-trained path: boosters from `models/lgbm/` and pickled scikit-learn
    calibrators. `src.hosted_models.predict` is the committed-asset equivalent.
    """
    from src.baseline_common import LGBM_DIR as MODELS

    import joblib

    rows = []
    for target in TARGETS:
        for horizon in HORIZONS:
            stem = f"{target}_{horizon}"
            model_path = MODELS / f"{stem}.txt"
            if not model_path.is_file():
                raise DataError(
                    f"missing {model_path}; run python -m src.train_baselines first"
                )
            booster = lgb.Booster(model_file=str(model_path))
            raw = booster.predict(frame[features])

            # Use the calibration Phase 3 settled on; fall back to raw if absent.
            calibrator_path = MODELS / f"{stem}_isotonic_cv.joblib"
            if calibrator_path.is_file():
                probability = joblib.load(calibrator_path).predict(raw)
                model_name = "lightgbm+isotonic_cv"
            else:
                probability = raw
                model_name = "lightgbm_uncalibrated"

            for unit_id, p in zip(frame["unit_id"], probability):
                rows.append({"unit_id": unit_id, "hazard": target,
                             "horizon": horizon, "probability": float(p),
                             "model": model_name})
    return pd.DataFrame(rows)


def matches(rule: dict, context: dict) -> bool:
    when = rule["when"]
    for key, wanted in when.items():
        if key == "horizon_at_most":
            horizon = context.get("horizon")
            if horizon is None or horizon > wanted:
                return False
        elif key.endswith("_in"):
            if context.get(key[:-3]) not in wanted:
                return False
        elif context.get(key) != wanted:
            return False
    return True


def classify(context: dict, policy: dict) -> tuple[str, str, bool]:
    """(status, reason, urgent) for one advisory."""
    for rule in policy["hold_for_approval"]:
        if matches(rule, context):
            urgent = any(matches(u, context) for u in policy.get("urgent", []))
            return "pending_approval", rule["reason"], urgent
    return "approved", policy["auto_approve_reason"].strip(), False


def main(argv: list[str] | None = None) -> int:
    """Run one forecast. `argv` lets the app call this in-process.

    On Streamlit Community Cloud the whole container has ~2.7 GB, so forking a second
    Python to shell out to this module would roughly double peak memory for the run.
    The app passes the arguments here instead.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--pilot-only", action="store_true", default=True)
    parser.add_argument("--all-units", dest="pilot_only", action="store_false")
    parser.add_argument("--created-by", default="cli")
    parser.add_argument("--db-url", default=None,
                        help="write to this database instead of DATABASE_URL, e.g. "
                             "the cloud Postgres. Use --db-url-env to keep the "
                             "connection string out of the shell history.")
    parser.add_argument("--db-url-env", default=None, metavar="NAME",
                        help="name of an environment variable holding --db-url, e.g. "
                             "--db-url-env DATABASE_URL_CLOUD")
    args = parser.parse_args(argv)

    if args.db_url and args.db_url_env:
        raise DataError("pass either --db-url or --db-url-env, not both")
    if args.db_url_env:
        from src.runtime import env as read_env

        value = read_env(args.db_url_env, "") or ""
        if not value.strip():
            raise DataError(f"{args.db_url_env} is not set")
        use_url(value)
    elif args.db_url:
        use_url(args.db_url)

    requested = dt.date.fromisoformat(args.as_of)
    policy = load_config(ALERT_POLICY)
    rules_cfg = load_config("rules")
    choice = load_model_choice()

    print("--- pipeline.run ---")
    print(f"  database: {describe_db()}")
    create_all()

    source, source_name = model_source()
    print(f"  models  : {source_name}")
    table = source.load_table()
    features = source.feature_columns(table)
    as_of, note = resolve_date(table, requested)
    if note:
        print(f"  {note}")

    day = table[table["start_date"] == pd.Timestamp(as_of)].copy()
    if day.empty:
        raise DataError(f"no rows for {as_of}")

    with session_scope() as session:
        known = {u.unit_id for u in session.query(Unit).all()}
        if args.pilot_only:
            day = day[day["unit_id"].isin(known)]
        if day.empty:
            raise DataError(
                "no pilot units in the training table for this date; "
                "run python -m src.db.init first"
            )

        print(f"  as-of {as_of}  units {len(day)}  "
              f"model choice: {'loaded' if choice else 'not set'}")
        forecasts = source.predict(day, features)

        previous = (session.query(ForecastRun)
                    .order_by(ForecastRun.as_of.desc(), ForecastRun.id.desc())
                    .first())

        run = ForecastRun(
            as_of=as_of, requested_as_of=requested, substitution_note=note,
            model_choice=json.dumps(choice) if choice else None,
            pilot_only=args.pilot_only, n_units=int(day["unit_id"].nunique()),
            created_by=args.created_by,
        )
        session.add(run)
        session.flush()

        from src.advisory import risk_level

        for row in forecasts.itertuples():
            session.add(Forecast(
                run_id=run.id, unit_id=row.unit_id, hazard=row.hazard,
                horizon=row.horizon, probability=row.probability,
                risk_level=risk_level(row.probability, rules_cfg),
                model=row.model,
            ))

        # --- changes vs the previous run ---------------------------------
        n_changes = n_escalations = 0
        if previous is not None:
            old = pd.read_sql(
                session.query(Forecast)
                .filter(Forecast.run_id == previous.id).statement,
                session.connection(),
            )
            lookup = {(r.unit_id, r.hazard, r.horizon): (r.probability, r.risk_level)
                      for r in old.itertuples()}
            order = {"green": 0, "amber": 1, "red": 2}
            for row in forecasts.itertuples():
                key = (row.unit_id, row.hazard, row.horizon)
                if key not in lookup:
                    continue
                before_p, before_risk = lookup[key]
                now_risk = risk_level(row.probability, rules_cfg)
                escalated = order.get(now_risk, 0) > order.get(before_risk, 0)
                session.add(ForecastChange(
                    run_id=run.id, previous_run_id=previous.id, unit_id=row.unit_id,
                    hazard=row.hazard, horizon=row.horizon,
                    previous_probability=before_p, probability=row.probability,
                    delta=row.probability - (before_p or 0.0),
                    previous_risk=before_risk, risk_level=now_risk,
                    escalated=escalated,
                ))
                n_changes += 1
                n_escalations += int(escalated)

        # --- advisories ---------------------------------------------------
        chain = build_chain()
        wide = forecasts.pivot_table(index="unit_id",
                                     columns=["hazard", "horizon"],
                                     values="probability")
        units = {u.unit_id: u for u in session.query(Unit).all()}
        never_send = set(policy.get("never_send_actions") or [])

        counts = {"pending_approval": 0, "approved": 0, "skipped": 0}
        for unit_id, series in wide.iterrows():
            unit = units.get(unit_id)
            if unit is None:
                continue
            probabilities = {f"{h}_{k}": float(series[(h, k)])
                             for h in TARGETS for k in HORIZONS}
            onset_done = probabilities["onset_7"] != probabilities["onset_7"]

            result = advise(
                probabilities, state=unit.state, zone=unit.zone_id,
                date=as_of, unit_id=unit_id,
                onset_happened=None if onset_done else False,
                cfg=rules_cfg,
            )
            if result.action in never_send:
                counts["skipped"] += 1
                continue

            worst = max(
                ((h, k, probabilities[f"{h}_{k}"]) for h in TARGETS for k in HORIZONS),
                key=lambda t: t[2],
            )
            context = {
                "risk_level": result.risk.get(f"{worst[0]}_{worst[1]}"),
                "confidence": result.confidence,
                "hazard": worst[0], "horizon": worst[1],
                "action": result.action,
            }

            texts = {}
            # Track what the chain actually returned. Hardcoding "template" here
            # would label Sarvam or Gemini output as reviewed rule-base text and let
            # it skip the llm_text hold rule in alert_policy.yaml.
            text_source = "template"
            for language in LANGUAGES:
                body = result.text.get(language)
                if body is None:
                    texts[language] = None
                    continue
                payload = {
                    "template_text": body, "unit_name": unit.unit_name,
                    "district": unit.district, "state": unit.state,
                    "crops": ", ".join(result.crops), "action": result.action,
                    "action_keyword": (rules_cfg["actions"][result.action]
                                       .get("keywords") or {}).get(language),
                    "probability": worst[2], "horizon_days": worst[1],
                    "risk_level": context["risk_level"], "stage": result.stage,
                    "language": language,
                }
                try:
                    generated = chain.generate_advisory_text(payload, language)
                    texts[language] = generated.text
                    if generated.source == "llm":
                        text_source = "llm"
                except Exception:
                    texts[language] = body

            context["source"] = text_source
            status, reason, urgent = classify(context, policy)

            session.add(AdvisoryRow(
                run_id=run.id, unit_id=unit_id, as_of=as_of,
                action=result.action, stage=result.stage,
                crops=", ".join(result.crops), confidence=result.confidence,
                hazard=worst[0], horizon=worst[1], probability=worst[2],
                risk_level=context["risk_level"], status=status,
                status_reason=reason, urgent=urgent,
                text_en=texts.get("en"), text_hi=texts.get("hi"),
                text_mr=texts.get("mr"), source=text_source,
            ))
            counts[status] += 1

        session.add(AuditLog(
            username=args.created_by, action="forecast_run", entity="forecast_run",
            entity_id=str(run.id),
            detail=f"as_of={as_of} units={run.n_units} "
                   f"pending={counts['pending_approval']} "
                   f"approved={counts['approved']}",
        ))
        run_id = run.id

    summarize(
        "pipeline.run",
        rows=len(forecasts),
        files=[],
        date_range=(str(as_of), str(as_of)),
        extra={
            "run id": run_id,
            "requested": str(requested),
            "substituted": bool(note),
            "units": int(day["unit_id"].nunique()),
            "forecasts": len(forecasts),
            "changes vs previous": n_changes,
            "escalations": n_escalations,
            "pending approval": counts["pending_approval"],
            "auto-approved": counts["approved"],
            "skipped (MONITOR)": counts["skipped"],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
