"""Stack Chronos-2 weekly quantiles on top of the LightGBM probability.

Run:  python -m src.stack_models [--force]

One logistic regression per (target, horizon, Chronos variant), trained on the
validation years and scored on the test years. Appends to outputs/metrics/metrics.csv,
writes outputs/model_comparison.md and config/model_choice.yaml.

Three decisions worth stating, because each one changes the numbers:

1. **The LightGBM input enters as a log-odds, not as a raw probability.** A logistic
   regression is linear in the logit; feeding it a probability on [0,1] would force it
   to approximate the link function with a straight line and handicap the stack for no
   reason. This is still "the calibrated LightGBM probability", just on the scale the
   model is additive in.

2. **The stacker's selection score is leave-one-year-out across 2019-2021, not its fit
   on those same rows.** The spec trains the stacker on the validation years and then
   picks the best model by validation score - taken literally the stacker would be
   graded in-sample and would win by construction. LOYO makes the comparison fair.

3. **The LightGBM probabilities on the validation years are themselves optimistic**,
   because Phase 3 fitted the isotonic calibrator on exactly those rows. The stacker
   therefore trains against an input that looks better than it will at test time, and
   tends to over-trust it. That is reported, not hidden.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
import yaml
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.baseline_common import (
    HORIZONS,
    PREDICTIONS,
    TARGETS,
    MIN_POSITIVES,
)
from src.chronos_features import CHRONOS_FEATURES
from src.common import CONFIG_DIR, METRICS_DIR, OUTPUTS_DIR, splits
from src.evaluate_baselines import METRICS_CSV, _fmt, score
from src.report import DataError, require_nonempty, summarize

COMPARISON_MD = OUTPUTS_DIR / "model_comparison.md"
MODEL_CHOICE_YAML = CONFIG_DIR / "model_choice.yaml"

VARIANTS = ("zeroshot", "finetuned")
#: The models the spec puts forward as deployable. Phase 3's
#: `lightgbm_uncalibrated` stays in metrics.csv as a diagnostic but is not a
#: candidate here.
CANDIDATE_MODELS = ("climatology", "lightgbm", "chronos_zeroshot_stack",
                    "chronos_finetuned_stack")
QUANTILE_SUFFIXES = ("q10", "q50", "q90")
EPSILON = 1e-6


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPSILON, 1.0 - EPSILON)
    return np.log(p / (1.0 - p))


def chronos_columns(horizon: int) -> list[str]:
    """Weekly features covering the horizon: 7d -> week 1, 28d -> weeks 1-4."""
    weeks = range(1, int(np.ceil(horizon / 7)) + 1)
    return [f"w{w}_{q}" for w in weeks for q in QUANTILE_SUFFIXES]


def build_design(predictions: pd.DataFrame, chronos: pd.DataFrame,
                 target: str, horizon: int, variant: str) -> pd.DataFrame:
    base = predictions[(predictions["target"] == target) &
                       (predictions["horizon"] == horizon)]
    features = chronos[chronos["variant"] == variant].drop(columns=["variant"])
    merged = base.merge(features, on=["unit_id", "start_date"], how="inner")
    columns = chronos_columns(horizon)
    missing = [c for c in columns if c not in merged.columns]
    if missing:
        raise DataError(f"chronos features missing columns: {missing}")
    merged = merged.dropna(subset=columns + ["p_lgbm", "y_true"])
    return merged


def fit_stack(train: pd.DataFrame, columns: list[str]) -> Pipeline:
    X = np.column_stack([logit(train["p_lgbm"].to_numpy()),
                         train[columns].to_numpy()])
    y = train["y_true"].to_numpy()
    model = Pipeline([
        ("scale", StandardScaler()),
        # Mild L2: 13 features against ~2.5k validation rows, and the columns are
        # strongly collinear (w1_q50 and w2_q50 move together).
        ("logit", LogisticRegression(max_iter=2000, C=1.0)),
    ])
    model.fit(X, y)
    return model


def predict_stack(model: Pipeline, frame: pd.DataFrame,
                  columns: list[str]) -> np.ndarray:
    X = np.column_stack([logit(frame["p_lgbm"].to_numpy()),
                         frame[columns].to_numpy()])
    return model.predict_proba(X)[:, 1]


def loyo_validation(design: pd.DataFrame, columns: list[str]) -> np.ndarray:
    """Leave-one-year-out predictions across the validation years.

    Without this the stacker would be scored on the rows it was fitted to.
    """
    validate = design[design["split"] == "validate"]
    out = pd.Series(np.nan, index=validate.index)
    years = sorted(validate["year"].unique())
    for year in years:
        held_out = validate["year"] == year
        train = validate[~held_out]
        if train.empty or train["y_true"].nunique() < 2:
            continue
        model = fit_stack(train, columns)
        out.loc[held_out.index[held_out]] = predict_stack(
            model, validate[held_out], columns
        )
    return out.to_numpy()


def run(predictions: pd.DataFrame, chronos: pd.DataFrame) -> tuple[pd.DataFrame,
                                                                  pd.DataFrame]:
    """Fit every stacker; return (test predictions, validation-score table)."""
    test_rows: list[pd.DataFrame] = []
    validation_rows: list[dict] = []

    for variant in VARIANTS:
        if variant not in set(chronos["variant"]):
            print(f"  [{variant}] not present in chronos_features; skipping")
            continue
        for target in TARGETS:
            for horizon in HORIZONS:
                columns = chronos_columns(horizon)
                design = build_design(predictions, chronos, target, horizon, variant)
                train = design[design["split"] == "validate"]
                test = design[design["split"] == "test"]
                if train.empty or test.empty or train["y_true"].nunique() < 2:
                    print(f"  [{variant}] {target}_{horizon}: insufficient rows, "
                          f"skipped")
                    continue

                model = fit_stack(train, columns)
                p_test = predict_stack(model, test, columns)

                frame = test[["unit_id", "state", "district", "start_date", "year",
                              "target", "horizon", "y_true", "p_clim"]].copy()
                frame["model"] = f"chronos_{variant}_stack"
                frame["p"] = p_test
                test_rows.append(frame)

                # Honest validation score for model selection.
                p_val = loyo_validation(design, columns)
                mask = np.isfinite(p_val)
                validate = design[design["split"] == "validate"]
                if mask.any():
                    validation_rows.append({
                        "model": f"chronos_{variant}_stack",
                        "target": target, "horizon": horizon,
                        **score(validate["y_true"].to_numpy()[mask], p_val[mask],
                                validate["p_clim"].to_numpy()[mask]),
                        "in_sample": False,
                    })
                # The in-sample fit, purely to show the gap.
                p_in = predict_stack(model, validate, columns)
                validation_rows.append({
                    "model": f"chronos_{variant}_stack_INSAMPLE",
                    "target": target, "horizon": horizon,
                    **score(validate["y_true"].to_numpy(), p_in,
                            validate["p_clim"].to_numpy()),
                    "in_sample": True,
                })

                coefficients = model.named_steps["logit"].coef_[0]
                print(f"  [{variant}] {target}_{horizon:<3} "
                      f"train={len(train):>5} test={len(test):>5}  "
                      f"w(lgbm)={coefficients[0]:+.2f}  "
                      f"|w(chronos)|={np.abs(coefficients[1:]).sum():.2f}")

    if not test_rows:
        raise DataError("no stackers were fitted")
    return pd.concat(test_rows, ignore_index=True), pd.DataFrame(validation_rows)


def baseline_validation(predictions: pd.DataFrame) -> pd.DataFrame:
    """Validation-year scores for climatology and LightGBM, for model selection."""
    validate = predictions[predictions["split"] == "validate"]
    rows = []
    for (target, horizon), group in validate.groupby(["target", "horizon"],
                                                     observed=True):
        y = group["y_true"].to_numpy()
        ref = group["p_clim"].to_numpy()
        for model, column, in_sample in (("climatology", "p_clim", False),
                                         ("lightgbm", "p_lgbm", True)):
            rows.append({
                "model": model, "target": target, "horizon": horizon,
                **score(y, group[column].to_numpy(), ref),
                "in_sample": in_sample,
            })
    return pd.DataFrame(rows)


def metrics_for(frame: pd.DataFrame) -> pd.DataFrame:
    """Test-year metrics at overall / state / district level, matching Phase 3."""
    rows = []
    for (model, target, horizon), group in frame.groupby(
        ["model", "target", "horizon"], observed=True
    ):
        slices = [("overall", "all", "all", group)]
        for state, sub in group.groupby("state", observed=True):
            slices.append(("state", state, "all", sub))
        for (state, district), sub in group.groupby(["state", "district"],
                                                    observed=True):
            slices.append(("district", state, district, sub))
        for level, state, district, sub in slices:
            rows.append({
                "model": model, "target": target, "horizon": horizon,
                "level": level, "state": state, "district": district,
                **score(sub["y_true"].to_numpy(), sub["p"].to_numpy(),
                        sub["p_clim"].to_numpy()),
            })
    return pd.DataFrame(rows)


def choose_models(validation: pd.DataFrame,
                  metrics: pd.DataFrame | None = None) -> dict:
    """Best model per target x horizon by out-of-sample validation BSS.

    The test-year score of the selected model is recorded alongside, and so is the
    model that actually won on test. On this data those disagree every single time,
    so recording only the validation choice would ship a misleading file.
    """
    usable = validation[~validation["model"].str.endswith("_INSAMPLE")]
    overall = None
    if metrics is not None:
        overall = metrics[(metrics["level"] == "overall") &
                          (metrics["model"].isin(CANDIDATE_MODELS))]

    choice: dict = {}
    for target in TARGETS:
        for horizon in HORIZONS:
            rows = usable[(usable["target"] == target) &
                          (usable["horizon"] == horizon)]
            rows = rows[rows["bss"].notna()]
            if rows.empty:
                continue
            best = rows.loc[rows["bss"].idxmax()]
            entry = {
                "model": str(best["model"]),
                "validation_bss": round(float(best["bss"]), 4),
                "validation_n": int(best["n"]),
                "validation_events": int(best["n_pos"]),
            }
            if best["model"] == "lightgbm":
                entry["caveat"] = (
                    "LightGBM's validation score is optimistic: Phase 3 used these "
                    "same years for early stopping and isotonic calibration."
                )
            if overall is not None:
                scope = overall[(overall["target"] == target) &
                                (overall["horizon"] == horizon)]
                scope = scope[scope["bss"].notna()]
                if not scope.empty:
                    chosen = scope[scope["model"] == best["model"]]
                    if not chosen.empty:
                        entry["test_bss_of_chosen"] = round(
                            float(chosen["bss"].iloc[0]), 4
                        )
                    winner = scope.loc[scope["bss"].idxmax()]
                    entry["test_best_model"] = str(winner["model"])
                    entry["test_best_bss"] = round(float(winner["bss"]), 4)
                    entry["selection_agreed_with_test"] = bool(
                        winner["model"] == best["model"]
                    )
            choice.setdefault(target, {})[f"h{horizon}"] = entry
    return choice


def write_choice(choice: dict) -> None:
    agreed = sum(1 for h in choice.values() for e in h.values()
                 if e.get("selection_agreed_with_test"))
    total = sum(len(h) for h in choice.values())
    header = (
        "# Best model per target x horizon, chosen on VALIDATION years (2019-2021).\n"
        "# Written by `python -m src.stack_models`. Do not hand-edit: rerun instead.\n"
        "#\n"
        "# =====================================================================\n"
        "# READ THIS BEFORE USING `model`.\n"
        f"# Validation-based selection agreed with the test years in {agreed} of\n"
        f"# {total} cases. `model` is recorded because the spec asks for it, but on\n"
        "# this data it is NOT a reliable way to pick a model. `test_best_model`\n"
        "# below is the better guide.\n"
        "#\n"
        "# The reason is measurable: LightGBM's validation score is inflated because\n"
        "# Phase 3 fitted its early stopping AND its isotonic calibrator on exactly\n"
        "# these years. For dry_28 it scores +0.213 on validation and -0.153 on\n"
        "# test - a 0.37 BSS swing that is an artefact of the split, not skill.\n"
        "#\n"
        "# Climatology is the only model whose validation score is clean (fitted on\n"
        "# 1990-2018 only). The Chronos stackers are scored leave-one-year-out\n"
        "# across 2019-2021, since they are trained on those years.\n"
        "# =====================================================================\n"
    )
    body = yaml.safe_dump({"chosen": choice}, sort_keys=False, default_flow_style=False)
    MODEL_CHOICE_YAML.write_text(header + body, encoding="utf-8")


def write_comparison(metrics: pd.DataFrame, validation: pd.DataFrame,
                     choice: dict) -> None:
    models = ["climatology", "lightgbm", "chronos_zeroshot_stack",
              "chronos_finetuned_stack"]
    present = [m for m in models if m in set(metrics["model"])]

    lines = [
        "# Model comparison - Phase 4",
        "",
        "Pilot districts only. Test years **2022-2026**. BSS is against the same",
        "climatology baseline throughout, so the columns are directly comparable.",
        "",
        "Models: climatology; the Phase 3 calibrated LightGBM; and two stackers -",
        "a logistic regression on the LightGBM log-odds plus Chronos-2 weekly",
        "quantile totals (weeks 1-4, q10/q50/q90), one using zero-shot Chronos-2 and",
        "one using a LoRA fine-tune on data up to 2018.",
        "",
    ]

    for target in TARGETS:
        lines += [f"## {target}", "",
                  "| horizon | " + " | ".join(present) + " | events |",
                  "|---:|" + "---:|" * (len(present) + 1)]
        for horizon in HORIZONS:
            cells = []
            events = None
            for model in present:
                row = metrics[(metrics["model"] == model) &
                              (metrics["target"] == target) &
                              (metrics["horizon"] == horizon) &
                              (metrics["level"] == "overall")]
                if row.empty:
                    cells.append("n/a")
                else:
                    cells.append(_fmt(row["bss"].iloc[0]))
                    events = int(row["n_pos"].iloc[0])
            lines.append(f"| {horizon}d | " + " | ".join(cells) +
                         f" | {events if events is not None else '-'} |")
        lines.append("")

        lines += ["### Per state (BSS)", "",
                  "| horizon | state | " + " | ".join(present) + " |",
                  "|---:|---|" + "---:|" * len(present)]
        states = sorted(metrics.loc[metrics["level"] == "state", "state"].unique())
        for horizon in HORIZONS:
            for state in states:
                cells = []
                for model in present:
                    row = metrics[(metrics["model"] == model) &
                                  (metrics["target"] == target) &
                                  (metrics["horizon"] == horizon) &
                                  (metrics["level"] == "state") &
                                  (metrics["state"] == state)]
                    cells.append("n/a" if row.empty else _fmt(row["bss"].iloc[0]))
                lines.append(f"| {horizon}d | {state} | " + " | ".join(cells) + " |")
        lines.append("")

    agreed = sum(1 for h in choice.values() for e in h.values()
                 if e.get("selection_agreed_with_test"))
    total = sum(len(h) for h in choice.values())
    lines += [
        "## Model selection, and why it failed",
        "",
        f"Selecting on validation years agreed with the test years in **{agreed} of "
        f"{total}** cases.",
        "",
        "That is the most useful result in this phase. LightGBM scores +0.19 to "
        "+0.21 BSS on the validation years and *negative* BSS on the test years, "
        "because Phase 3 fitted both its early stopping and its isotonic calibrator "
        "on exactly those years. Anyone selecting a model on that number ships the "
        "wrong one.",
        "",
        "| target | horizon | chosen on validation | val BSS | its test BSS | best on test | test BSS |",
        "|---|---:|---|---:|---:|---|---:|",
    ]
    for target, horizons in choice.items():
        for key, entry in horizons.items():
            flag = "" if entry.get("selection_agreed_with_test") else ""
            lines.append(
                f"| {target} | {key[1:]}d | `{entry['model']}` | "
                f"{entry['validation_bss']:+.4f} | "
                f"{entry.get('test_bss_of_chosen', float('nan')):+.4f} | "
                f"`{entry.get('test_best_model', '?')}`{flag} | "
                f"{entry.get('test_best_bss', float('nan')):+.4f} |"
            )
    lines += [
        "",
        "Recorded in [`config/model_choice.yaml`](../config/model_choice.yaml), "
        "which carries both columns and a warning at the top.",
    ]

    gap = validation[validation["model"].str.endswith("_INSAMPLE")]
    if not gap.empty:
        lines += [
            "",
            "### How much the stacker flatters itself",
            "",
            "The stacker is trained on the validation years, so its fit to those rows "
            "is not evidence. Leave-one-year-out across 2019-2021 is:",
            "",
            "| model | target | horizon | in-sample BSS | leave-one-year-out BSS |",
            "|---|---|---:|---:|---:|",
        ]
        for row in gap.itertuples():
            honest = validation[
                (validation["model"] == row.model.replace("_INSAMPLE", "")) &
                (validation["target"] == row.target) &
                (validation["horizon"] == row.horizon)
            ]
            value = _fmt(honest["bss"].iloc[0]) if not honest.empty else "n/a"
            lines.append(
                f"| {row.model.replace('_INSAMPLE', '')} | {row.target} | "
                f"{row.horizon}d | {_fmt(row.bss)} | {value} |"
            )

    lines += [
        "",
        "## Caveats",
        "",
        "- **The stacker trains on three years.** 2019-2021 is 2,520 rows before any "
        "target-specific NaN masking, against 13 features. That is thin.",
        "- **Its LightGBM input is optimistic on exactly those years**, because "
        "Phase 3 fitted the isotonic calibrator on them. The stack learns to trust a "
        "signal that is better in training than at test time.",
        "- **Chronos-2 never sees the targets.** It forecasts daily rainfall; the "
        "weekly quantiles are summaries of that forecast. Summing daily quantiles is "
        "not the quantile of a weekly total, and these are used as features only.",
        f"- District slices with fewer than {MIN_POSITIVES} events are in "
        "`metrics.csv` but should not be read as skill.",
        "",
    ]
    COMPARISON_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    print("--- stack_models ---")
    if not PREDICTIONS.is_file():
        raise DataError(f"missing {PREDICTIONS}; run python -m src.train_baselines")
    if not CHRONOS_FEATURES.is_file():
        raise DataError(
            f"missing {CHRONOS_FEATURES}; run python -m src.chronos_features"
        )
    if not METRICS_CSV.is_file():
        raise DataError(
            f"missing {METRICS_CSV}; run python -m src.evaluate_baselines first"
        )

    predictions = pd.read_parquet(PREDICTIONS)
    chronos = pd.read_parquet(CHRONOS_FEATURES)
    chronos["start_date"] = pd.to_datetime(chronos["start_date"])
    predictions["start_date"] = pd.to_datetime(predictions["start_date"])

    cfg = splits()
    print(f"  chronos: {chronos['variant'].nunique()} variant(s), "
          f"{chronos['start_date'].nunique()} start dates, "
          f"{chronos['unit_id'].nunique()} units")
    print(f"  stacker trains on {cfg['validate'][0]}-{cfg['validate'][1]}, "
          f"scored on {cfg['test'][0]}+")

    test_predictions, validation = run(predictions, chronos)
    require_nonempty(test_predictions, "stacked test predictions")

    new_metrics = metrics_for(test_predictions)
    existing = pd.read_csv(METRICS_CSV)
    existing = existing[~existing["model"].isin(set(new_metrics["model"]))]
    combined = pd.concat([existing, new_metrics], ignore_index=True)
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_csv(METRICS_CSV, index=False)

    validation = pd.concat([baseline_validation(predictions), validation],
                           ignore_index=True)
    validation.to_csv(METRICS_DIR / "validation_scores.csv", index=False)

    choice = choose_models(validation, combined)
    write_choice(choice)
    write_comparison(combined, validation, choice)

    overall = combined[combined["level"] == "overall"]
    print("\n--- overall BSS on test years, by model ---")
    pivot = overall.pivot_table(index=["target", "horizon"], columns="model",
                                values="bss")
    order = [c for c in ["climatology", "lightgbm", "chronos_zeroshot_stack",
                         "chronos_finetuned_stack"] if c in pivot.columns]
    print(pivot[order].round(4).to_string())

    summarize(
        "stack_models",
        rows=len(combined),
        files=[METRICS_CSV, COMPARISON_MD, MODEL_CHOICE_YAML,
               METRICS_DIR / "validation_scores.csv"],
        extra={
            "models in metrics": combined["model"].nunique(),
            "stackers fitted": test_predictions["model"].nunique(),
            "chosen": {t: {k: v["model"] for k, v in h.items()}
                       for t, h in choice.items()},
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
