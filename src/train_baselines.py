"""Train the Phase 3 baselines: climatology and a calibrated LightGBM per target.

Run:  python -m src.train_baselines [--force]

For each of the 12 (target, horizon) pairs:

  1. climatology  - P(event | unit, 5-day window) from training years only
  2. LightGBM     - fit on 1990-2018, early-stopped on 2019-2021
  3. calibration  - fitted on out-of-fold predictions across the TRAINING years

Why step 3 is not fitted on the validation years
------------------------------------------------
It originally was, and it cost skill in 11 of 12 cases. Fitting a calibrator on
2019-2021 means fitting it on the same rows that chose the tree count, and worse,
those three years are not representative: dry spells ran at 35.8% there against 53.5%
in training and 47.7% in test. Isotonic regression is flexible enough to lock that
three-year base rate in, after which every test-year forecast is biased.

Cross-fitting on the training years fixes both problems. Five folds **grouped by
year** - rows inside one year share weather across 30 units and 28 start dates, so a
random split would leak - give out-of-fold predictions over all 29 training years,
and the calibrator is fitted on those. Measured effect: test BSS improved in 11 of 12
cases (dry_28 -0.153 -> -0.010, onset_21 -0.141 -> -0.014), and the gap between the
validation and test scores fell from 0.19 BSS to 0.03, which is what makes a
validation score usable for choosing a model at all.

All four calibrations are written side by side so the choice stays auditable:
`p_lgbm_raw`, `p_lgbm_isoval`, `p_lgbm_isocv`, `p_lgbm_plattcv`. `p_lgbm` is whichever
`--calibration` selects (default `isotonic_cv`, the best on test).

Writes models/lgbm/ and data/processed/predictions.parquet.
"""

from __future__ import annotations

import argparse
import json
import shutil

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from src.baseline_common import (
    HORIZONS,
    LGBM_DIR,
    PREDICTIONS,
    TARGETS,
    climatology,
    feature_columns,
    load_table,
    split_masks,
    target_column,
)
from src.report import DataError, require_nonempty, summarize

PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "min_data_in_leaf": 60,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "seed": 42,
    "num_threads": 0,
}
MAX_ROUNDS = 2000
EARLY_STOPPING = 100


N_CALIBRATION_FOLDS = 5
EPSILON = 1e-6

#: Which calibration feeds the `p_lgbm` column that Phase 4 stacks on.
DEFAULT_CALIBRATION = "isotonic_cv"
CALIBRATION_COLUMNS = {
    "none": "p_lgbm_raw",
    "isotonic_val": "p_lgbm_isoval",
    "isotonic_cv": "p_lgbm_isocv",
    "platt_cv": "p_lgbm_plattcv",
}


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPSILON, 1.0 - EPSILON)
    return np.log(p / (1.0 - p))


def out_of_fold_predictions(frame: pd.DataFrame, train: pd.Series,
                            validate: pd.Series, column: str,
                            features: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Out-of-fold predictions across the training years, grouped by year.

    Each fold's model is early-stopped on the validation years, exactly as the final
    model is, so the predictions being calibrated come from the same recipe.
    """
    years = np.array(sorted(frame.loc[train, "year"].unique()))
    folds = np.array_split(years, min(N_CALIBRATION_FOLDS, len(years)))

    y_val = frame.loc[validate, column].to_numpy()
    dval = lgb.Dataset(frame.loc[validate, features], label=y_val,
                       free_raw_data=False)

    raw_parts, y_parts = [], []
    for fold_years in folds:
        held_out = train & frame["year"].isin(fold_years)
        fold_train = train & ~frame["year"].isin(fold_years)
        if not held_out.any() or frame.loc[fold_train, column].nunique() < 2:
            continue
        booster = lgb.train(
            PARAMS,
            lgb.Dataset(frame.loc[fold_train, features],
                        label=frame.loc[fold_train, column].to_numpy(),
                        free_raw_data=False),
            num_boost_round=MAX_ROUNDS, valid_sets=[dval], valid_names=["validate"],
            callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)],
        )
        raw_parts.append(booster.predict(frame.loc[held_out, features],
                                         num_iteration=booster.best_iteration))
        y_parts.append(frame.loc[held_out, column].to_numpy())

    if not raw_parts:
        raise DataError(f"{column}: could not build out-of-fold predictions")
    return np.concatenate(raw_parts), np.concatenate(y_parts)


def fit_one(frame: pd.DataFrame, masks: dict, target: str, horizon: int,
            features: list[str], calibration: str) -> tuple[dict, pd.DataFrame]:
    """Train + calibrate one (target, horizon). Returns (info, predictions)."""
    column = target_column(target, horizon)
    labelled = frame[column].notna()

    train = masks["train"] & labelled
    validate = masks["validate"] & labelled
    test = masks["test"] & labelled

    y_train = frame.loc[train, column].to_numpy()
    y_val = frame.loc[validate, column].to_numpy()
    if y_train.size == 0 or y_val.size == 0:
        raise DataError(f"{column}: empty train ({y_train.size}) or "
                        f"validate ({y_val.size}) split")
    if len(np.unique(y_train)) < 2:
        raise DataError(f"{column}: the training split has only one class")

    dtrain = lgb.Dataset(frame.loc[train, features], label=y_train,
                         free_raw_data=False)
    dval = lgb.Dataset(frame.loc[validate, features], label=y_val,
                       reference=dtrain, free_raw_data=False)

    booster = lgb.train(
        PARAMS, dtrain, num_boost_round=MAX_ROUNDS, valid_sets=[dval],
        valid_names=["validate"],
        callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)],
    )

    raw_val = booster.predict(frame.loc[validate, features],
                              num_iteration=booster.best_iteration)
    # Isotonic is monotone and non-parametric, so it can only re-map probabilities;
    # it never reorders them, which is why AUC is unchanged by calibration.
    calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    calibrator.fit(raw_val, y_val)

    # Cross-fitted calibrators, fitted on out-of-fold predictions over the 29
    # TRAINING years instead of the 3 validation years. Phase 3 measured why this
    # matters: 2019-2021 had a dry-spell rate of 35.8% against 53.5% in training and
    # 47.7% in test, and isotonic locked that 3-year rate in, costing skill in 11 of
    # 12 cases. Folds are grouped by YEAR - rows inside a year share weather across
    # 30 units and 28 start dates, so a random split would leak.
    oof_raw, oof_y = out_of_fold_predictions(frame, train, validate, column, features)
    cv_isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    cv_isotonic.fit(oof_raw, oof_y)
    # Platt has two parameters against isotonic's arbitrarily many, so it extrapolates
    # far more gracefully when the calibration set is small or shifted.
    cv_platt = LogisticRegression(C=1e6, max_iter=1000)
    cv_platt.fit(logit(oof_raw).reshape(-1, 1), oof_y)

    parts = []
    for name, mask in (("train", train), ("validate", validate), ("test", test)):
        if not mask.any():
            continue
        raw = booster.predict(frame.loc[mask, features],
                              num_iteration=booster.best_iteration)
        parts.append(pd.DataFrame({
            "unit_id": frame.loc[mask, "unit_id"].to_numpy(),
            "state": frame.loc[mask, "state"].astype(str).to_numpy(),
            "district": frame.loc[mask, "district"].to_numpy(),
            "start_date": frame.loc[mask, "start_date"].to_numpy(),
            "year": frame.loc[mask, "year"].to_numpy(),
            "split": name,
            "target": target,
            "horizon": horizon,
            "y_true": frame.loc[mask, column].to_numpy(),
            "p_clim": frame.loc[mask, "p_clim"].to_numpy(),
            "p_lgbm_raw": raw,
            "p_lgbm_isoval": calibrator.predict(raw),
            "p_lgbm_isocv": cv_isotonic.predict(raw),
            "p_lgbm_plattcv": cv_platt.predict_proba(
                logit(raw).reshape(-1, 1))[:, 1],
        }))

    predictions = pd.concat(parts, ignore_index=True)
    # `p_lgbm` is whichever calibration is in force; the alternatives stay beside it
    # so evaluate_baselines can score them all and the choice stays auditable.
    predictions["p_lgbm"] = predictions[CALIBRATION_COLUMNS[calibration]]

    LGBM_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{target}_{horizon}"
    booster.save_model(str(LGBM_DIR / f"{stem}.txt"),
                       num_iteration=booster.best_iteration)
    joblib.dump(calibrator, LGBM_DIR / f"{stem}_isotonic.joblib")
    joblib.dump(cv_isotonic, LGBM_DIR / f"{stem}_isotonic_cv.joblib")
    joblib.dump(cv_platt, LGBM_DIR / f"{stem}_platt_cv.joblib")

    info = {
        "target": target,
        "horizon": horizon,
        "best_iteration": int(booster.best_iteration),
        "n_train": int(train.sum()),
        "n_validate": int(validate.sum()),
        "n_test": int(test.sum()),
        "train_positive_rate": float(y_train.mean()),
        "validate_positive_rate": float(y_val.mean()),
        "calibration": calibration,
        "n_oof": int(oof_y.size),
        "oof_positive_rate": float(oof_y.mean()),
        "test_positive_rate": float(frame.loc[test, column].mean())
        if test.any() else float("nan"),
    }
    return info, predictions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--calibration", choices=sorted(CALIBRATION_COLUMNS),
                        default=DEFAULT_CALIBRATION,
                        help="which calibration feeds the p_lgbm column")
    args = parser.parse_args()

    if PREDICTIONS.is_file() and not args.force:
        print(f"{PREDICTIONS} exists; pass --force to retrain")
        return 0

    print("--- train_baselines ---")
    frame = load_table()
    masks = split_masks(frame)
    features = feature_columns(frame)
    print(f"  {len(frame):,} rows, {len(features)} features")
    print(f"  train {int(masks['train'].sum()):,} / "
          f"validate {int(masks['validate'].sum()):,} / "
          f"test {int(masks['test'].sum()):,}")
    print(f"  test years: {sorted(frame.loc[masks['test'], 'year'].unique())}")

    if LGBM_DIR.is_dir() and args.force:
        shutil.rmtree(LGBM_DIR)

    rows: list[dict] = []
    frames: list[pd.DataFrame] = []
    for target in TARGETS:
        for horizon in HORIZONS:
            column = target_column(target, horizon)
            # Climatology must be recomputed per target: it is fitted on training
            # years only, and which rows are labelled differs by target.
            frame["p_clim"] = climatology(frame, column, masks["train"])
            info, predictions = fit_one(frame, masks, target, horizon,
                                        features, args.calibration)
            rows.append(info)
            frames.append(predictions)
            print(f"  {column:<9} trees={info['best_iteration']:>4}  "
                  f"train={info['n_train']:>5} val={info['n_validate']:>4} "
                  f"test={info['n_test']:>4}  "
                  f"pos(train)={info['train_positive_rate']:5.1%} "
                  f"pos(test)={info['test_positive_rate']:5.1%}")

    predictions = pd.concat(frames, ignore_index=True)
    require_nonempty(predictions, "baseline predictions")
    for column in ("p_clim", "p_lgbm", "y_true", *CALIBRATION_COLUMNS.values()):
        predictions[column] = predictions[column].astype("float32")

    PREDICTIONS.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(PREDICTIONS, index=False)
    (LGBM_DIR / "training_info.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )

    summarize(
        "train_baselines",
        rows=len(predictions),
        files=[PREDICTIONS, LGBM_DIR],
        extra={
            "models": len(rows),
            "features": len(features),
            "test rows": int((predictions["split"] == "test").sum()),
            "calibration": args.calibration,
            "mean trees": f"{np.mean([r['best_iteration'] for r in rows]):.0f}",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
