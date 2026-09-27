"""Guards on the Phase 4 stack: horizon-to-week mapping, link function, honest CV."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.stack_models import (
    chronos_columns,
    choose_models,
    fit_stack,
    logit,
    loyo_validation,
    predict_stack,
)


# --------------------------------------------------------------------------
# Feature selection per horizon
# --------------------------------------------------------------------------
@pytest.mark.parametrize("horizon,weeks", [(7, 1), (14, 2), (21, 3), (28, 4)])
def test_horizon_maps_to_the_weeks_it_covers(horizon, weeks):
    columns = chronos_columns(horizon)
    assert len(columns) == 3 * weeks
    assert f"w{weeks}_q50" in columns
    assert f"w{weeks + 1}_q50" not in columns, (
        "a horizon must not use a week that ends after it"
    )


def test_every_horizon_uses_all_three_quantiles():
    for horizon in (7, 14, 21, 28):
        columns = chronos_columns(horizon)
        for q in ("q10", "q50", "q90"):
            assert any(c.endswith(q) for c in columns)


# --------------------------------------------------------------------------
# The link function
# --------------------------------------------------------------------------
def test_logit_is_the_inverse_of_the_sigmoid():
    p = np.array([0.01, 0.1, 0.5, 0.9, 0.99])
    back = 1.0 / (1.0 + np.exp(-logit(p)))
    np.testing.assert_allclose(back, p, atol=1e-6)


def test_logit_is_finite_at_the_boundaries():
    """A raw 0.0 or 1.0 from isotonic must not become +/-inf and kill the fit."""
    assert np.isfinite(logit(np.array([0.0, 1.0]))).all()


def test_logit_is_monotone():
    p = np.linspace(0.001, 0.999, 50)
    assert (np.diff(logit(p)) > 0).all()


# --------------------------------------------------------------------------
# Fitting and leave-one-year-out
# --------------------------------------------------------------------------
def toy_design(seed: int = 0, n_per_year: int = 300) -> pd.DataFrame:
    """A design where the Chronos columns genuinely carry signal."""
    rng = np.random.default_rng(seed)
    rows = []
    for split, years in (("validate", [2019, 2020, 2021]), ("test", [2022, 2023])):
        for year in years:
            w1 = rng.gamma(2.0, 12.0, n_per_year)
            noise = rng.normal(0, 1, n_per_year)
            # Truth depends on the Chronos median; the LightGBM probability is
            # deliberately weak so the stack has something to add.
            p_event = 1 / (1 + np.exp(-(w1 - 24) / 10))
            y = (rng.random(n_per_year) < p_event).astype(float)
            rows.append(pd.DataFrame({
                "split": split, "year": year,
                "unit_id": "U1", "state": "S", "district": "D",
                "start_date": pd.Timestamp(f"{year}-06-04"),
                "target": "dry", "horizon": 7,
                "y_true": y,
                "p_clim": np.full(n_per_year, y.mean()),
                "p_lgbm": np.clip(0.5 + 0.02 * noise, 0.01, 0.99),
                "w1_q10": w1 * 0.5, "w1_q50": w1, "w1_q90": w1 * 1.8,
            }))
    return pd.concat(rows, ignore_index=True)


def test_stack_learns_a_signal_that_is_present():
    design = toy_design()
    columns = chronos_columns(7)
    train = design[design["split"] == "validate"]
    test = design[design["split"] == "test"]

    model = fit_stack(train, columns)
    p = predict_stack(model, test, columns)
    from sklearn.metrics import roc_auc_score

    assert roc_auc_score(test["y_true"], p) > 0.75, (
        "the stack failed to pick up an obvious Chronos signal"
    )


def test_stack_weights_chronos_above_a_useless_lightgbm_input():
    design = toy_design()
    columns = chronos_columns(7)
    model = fit_stack(design[design["split"] == "validate"], columns)
    weights = model.named_steps["logit"].coef_[0]
    assert abs(weights[0]) < np.abs(weights[1:]).max(), (
        "a noise LightGBM input outweighed the informative Chronos columns"
    )


def test_loyo_predicts_every_validation_row_out_of_fold():
    design = toy_design()
    p = loyo_validation(design, chronos_columns(7))
    validate = design[design["split"] == "validate"]
    assert len(p) == len(validate)
    assert np.isfinite(p).all(), "a validation row was left without a prediction"


def test_loyo_is_not_better_than_the_in_sample_fit():
    """The whole point: LOYO must not flatter the model the way its own fit does."""
    from sklearn.metrics import brier_score_loss

    design = toy_design()
    columns = chronos_columns(7)
    validate = design[design["split"] == "validate"]

    model = fit_stack(validate, columns)
    in_sample = brier_score_loss(validate["y_true"], predict_stack(model, validate,
                                                                  columns))
    held_out = brier_score_loss(validate["y_true"], loyo_validation(design, columns))
    assert held_out >= in_sample - 1e-9, (
        "leave-one-year-out scored better than the in-sample fit, which means the "
        "folds are leaking"
    )


# --------------------------------------------------------------------------
# Model selection
# --------------------------------------------------------------------------
def test_choose_models_ignores_in_sample_rows():
    validation = pd.DataFrame([
        {"model": "climatology", "target": "dry", "horizon": 7, "bss": 0.00,
         "n": 100, "n_pos": 30},
        {"model": "lightgbm", "target": "dry", "horizon": 7, "bss": 0.05,
         "n": 100, "n_pos": 30},
        {"model": "chronos_zeroshot_stack", "target": "dry", "horizon": 7,
         "bss": 0.02, "n": 100, "n_pos": 30},
        # An in-sample row with a huge score must never be selected.
        {"model": "chronos_zeroshot_stack_INSAMPLE", "target": "dry", "horizon": 7,
         "bss": 0.99, "n": 100, "n_pos": 30},
    ])
    choice = choose_models(validation)
    assert choice["dry"]["h7"]["model"] == "lightgbm"
    assert "caveat" in choice["dry"]["h7"], (
        "picking LightGBM on contaminated validation years must carry a warning"
    )


def test_choose_models_skips_targets_with_no_score():
    validation = pd.DataFrame([
        {"model": "climatology", "target": "dry", "horizon": 7, "bss": np.nan,
         "n": 0, "n_pos": 0},
    ])
    assert choose_models(validation) == {}
