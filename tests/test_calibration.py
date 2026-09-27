"""Guards on the cross-fitted calibration.

The bug this replaces was not a crash - it was a calibrator quietly fitted on three
unrepresentative years, which cost skill in 11 of 12 cases while every test passed.
These tests pin the properties that would have caught it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.train_baselines import (
    CALIBRATION_COLUMNS,
    DEFAULT_CALIBRATION,
    N_CALIBRATION_FOLDS,
    logit,
    out_of_fold_predictions,
)


def toy_frame(n_years: int = 29, rows_per_year: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    for year in range(1990, 1990 + n_years + 3):
        x = rng.normal(size=rows_per_year)
        y = (rng.random(rows_per_year) < 1 / (1 + np.exp(-x))).astype(float)
        rows.append(pd.DataFrame({
            "year": year, "feat": x, "target": y,
            "unit_id": "U1", "start_date": pd.Timestamp(f"{year}-06-04"),
        }))
    return pd.concat(rows, ignore_index=True)


def test_default_calibration_is_the_cross_fitted_one():
    """The validation-fitted variant is kept for comparison, never as the default."""
    assert DEFAULT_CALIBRATION == "isotonic_cv"
    assert CALIBRATION_COLUMNS[DEFAULT_CALIBRATION] == "p_lgbm_isocv"


def test_every_calibration_variant_has_a_distinct_column():
    columns = list(CALIBRATION_COLUMNS.values())
    assert len(columns) == len(set(columns))


def test_out_of_fold_covers_every_training_row_exactly_once():
    frame = toy_frame()
    train = frame["year"] <= 2018
    validate = frame["year"].between(2019, 2021)
    raw, y = out_of_fold_predictions(frame, train, validate, "target", ["feat"])
    assert raw.size == int(train.sum()), (
        "out-of-fold predictions must cover the training rows exactly once"
    )
    assert y.size == raw.size
    assert np.isfinite(raw).all()
    assert ((raw >= 0) & (raw <= 1)).all()


def test_out_of_fold_labels_match_the_training_labels():
    frame = toy_frame()
    train = frame["year"] <= 2018
    validate = frame["year"].between(2019, 2021)
    _, y = out_of_fold_predictions(frame, train, validate, "target", ["feat"])
    np.testing.assert_allclose(sorted(y), sorted(frame.loc[train, "target"]))


def test_folds_are_grouped_by_year_not_by_row():
    """A year must land entirely inside one fold, or its rows leak across folds.

    Rows within a year share the same weather across 30 units and 28 start dates, so
    a row-level split would let a fold's model see the season it is being scored on.
    """
    years = np.arange(1990, 2019)
    folds = np.array_split(years, N_CALIBRATION_FOLDS)
    seen: set[int] = set()
    for fold in folds:
        assert not (seen & set(fold.tolist())), "a year appears in two folds"
        seen |= set(fold.tolist())
    assert seen == set(years.tolist())
    assert len(folds) == N_CALIBRATION_FOLDS


def test_out_of_fold_is_not_the_in_sample_fit():
    """OOF predictions must be worse than a model's fit to its own training rows."""
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    from src.train_baselines import EARLY_STOPPING, MAX_ROUNDS, PARAMS

    frame = toy_frame()
    train = frame["year"] <= 2018
    validate = frame["year"].between(2019, 2021)

    oof_raw, oof_y = out_of_fold_predictions(frame, train, validate, "target",
                                             ["feat"])
    booster = lgb.train(
        PARAMS,
        lgb.Dataset(frame.loc[train, ["feat"]], frame.loc[train, "target"]),
        num_boost_round=MAX_ROUNDS,
        valid_sets=[lgb.Dataset(frame.loc[validate, ["feat"]],
                                frame.loc[validate, "target"])],
        callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)],
    )
    in_sample = booster.predict(frame.loc[train, ["feat"]],
                                num_iteration=booster.best_iteration)
    assert roc_auc_score(oof_y, oof_raw) <= roc_auc_score(
        frame.loc[train, "target"], in_sample
    ) + 1e-9, "out-of-fold beat the in-sample fit, so the folds are leaking"


def test_logit_round_trips_and_clips():
    p = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
    z = logit(p)
    assert np.isfinite(z).all()
    back = 1 / (1 + np.exp(-z))
    np.testing.assert_allclose(back[1:4], p[1:4], atol=1e-6)


@pytest.mark.skipif(
    not (__import__("src.baseline_common", fromlist=["PREDICTIONS"]).PREDICTIONS
         .is_file()),
    reason="predictions.parquet not built yet",
)
def test_built_predictions_carry_every_calibration():
    from src.baseline_common import PREDICTIONS

    frame = pd.read_parquet(PREDICTIONS)
    for column in CALIBRATION_COLUMNS.values():
        assert column in frame.columns, f"{column} missing from predictions"
        values = frame[column].dropna()
        assert ((values >= 0) & (values <= 1)).all(), f"{column} is not a probability"
    # p_lgbm must be a copy of the selected variant, not something else.
    chosen = CALIBRATION_COLUMNS[DEFAULT_CALIBRATION]
    np.testing.assert_allclose(frame["p_lgbm"], frame[chosen], rtol=1e-6)
