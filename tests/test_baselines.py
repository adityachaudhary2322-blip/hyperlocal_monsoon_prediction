"""Guards on the Phase 3 baselines: no leakage into climatology, correct scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.baseline_common import CLIM_ALPHA, climatology
from src.evaluate_baselines import reliability, score


def toy_frame(seed: int = 0) -> pd.DataFrame:
    """Three units x 10 windows x 20 years of a binary target."""
    rng = np.random.default_rng(seed)
    rows = []
    for year in range(2000, 2020):
        for unit in ("A", "B", "C"):
            for window in range(10):
                rows.append({
                    "unit_id": unit,
                    "start_md": f"06-{window + 1:02d}",
                    "year": year,
                    "y": float(rng.random() < 0.2 + 0.05 * window),
                })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Climatology must not see anything outside the training years
# --------------------------------------------------------------------------
def test_climatology_ignores_non_training_years():
    frame = toy_frame()
    train = frame["year"] <= 2014
    baseline = climatology(frame, "y", train)

    polluted = frame.copy()
    # Flip every label outside the training years.
    polluted.loc[~train, "y"] = 1.0 - polluted.loc[~train, "y"]
    after = climatology(polluted, "y", train)

    pd.testing.assert_series_equal(baseline, after, check_names=False)


def test_climatology_changes_when_training_labels_change():
    """The mirror of the test above - otherwise it could pass vacuously."""
    frame = toy_frame()
    train = frame["year"] <= 2014
    baseline = climatology(frame, "y", train)

    polluted = frame.copy()
    polluted.loc[train, "y"] = 1.0 - polluted.loc[train, "y"]
    after = climatology(polluted, "y", train)

    assert not np.allclose(baseline.to_numpy(), after.to_numpy())


def test_climatology_is_a_probability_everywhere():
    frame = toy_frame()
    p = climatology(frame, "y", frame["year"] <= 2014)
    assert p.notna().all(), "climatology must never be NaN - it is the reference"
    assert (p >= 0).all() and (p <= 1).all()


def test_climatology_shrinks_a_noisy_cell_toward_the_pooled_rate():
    """A cell that never fires should not be handed a probability of exactly zero."""
    frame = toy_frame()
    train = frame["year"] <= 2014
    frame.loc[(frame["unit_id"] == "A") & (frame["start_md"] == "06-01"), "y"] = 0.0

    p = climatology(frame, "y", train)
    cell = (frame["unit_id"] == "A") & (frame["start_md"] == "06-01")
    value = p[cell].iloc[0]
    assert value > 0, "a zero-count cell was not shrunk toward the pooled rate"
    # With 15 training years and alpha, the cell stays well below the pooled rate.
    pooled = frame.loc[train & (frame["start_md"] == "06-01"), "y"].mean()
    assert value < pooled


def test_climatology_alpha_controls_shrinkage():
    assert CLIM_ALPHA > 0, "zero smoothing would give 0/1 probabilities on rare cells"


def test_unseen_cell_falls_back_rather_than_going_nan():
    frame = toy_frame()
    train = (frame["year"] <= 2014) & (frame["unit_id"] != "C")
    p = climatology(frame, "y", train)
    # Unit C never appears in training, so its cells must use the pooled rate.
    assert p[frame["unit_id"] == "C"].notna().all()


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def test_bss_is_zero_when_the_model_equals_the_reference():
    y = np.array([0.0, 1, 0, 1, 1, 0, 0, 1])
    p = np.array([0.3, 0.7, 0.2, 0.8, 0.6, 0.1, 0.4, 0.9])
    out = score(y, p, p)
    assert out["bss"] == pytest.approx(0.0)
    assert out["brier"] == pytest.approx(out["brier_ref"])


def test_bss_is_positive_when_the_model_is_better():
    y = np.array([0.0, 1, 0, 1])
    good = np.array([0.1, 0.9, 0.1, 0.9])
    poor = np.array([0.5, 0.5, 0.5, 0.5])
    assert score(y, good, poor)["bss"] > 0
    assert score(y, poor, good)["bss"] < 0


def test_perfect_forecast_scores_a_bss_of_one():
    y = np.array([0.0, 1, 0, 1])
    perfect = y.copy()
    reference = np.full(4, 0.5)
    out = score(y, perfect, reference)
    assert out["brier"] == pytest.approx(0.0)
    assert out["bss"] == pytest.approx(1.0)
    assert out["auc"] == pytest.approx(1.0)


def test_auc_is_nan_when_only_one_class_is_present():
    y = np.zeros(10)
    out = score(y, np.linspace(0, 1, 10), np.full(10, 0.5))
    assert np.isnan(out["auc"])
    assert out["n_pos"] == 0


def test_score_reports_sample_size_and_reliability_flag():
    y = np.concatenate([np.ones(3), np.zeros(50)])
    out = score(y, np.full(53, 0.1), np.full(53, 0.1))
    assert out["n"] == 53 and out["n_pos"] == 3
    assert out["reliable"] is False, "3 events should not be called reliable"

    y = np.concatenate([np.ones(30), np.zeros(50)])
    out = score(y, np.full(80, 0.4), np.full(80, 0.4))
    assert out["reliable"] is True


def test_mean_forecast_is_recorded():
    """The calibration diagnosis in the report depends on this column."""
    y = np.array([0.0, 1, 0, 1])
    out = score(y, np.array([0.2, 0.2, 0.2, 0.2]), np.full(4, 0.5))
    assert out["mean_forecast"] == pytest.approx(0.2)


# --------------------------------------------------------------------------
# Reliability binning
# --------------------------------------------------------------------------
def test_reliability_recovers_a_perfectly_calibrated_forecast():
    rng = np.random.default_rng(3)
    p = rng.uniform(0.05, 0.95, size=40000)
    y = (rng.random(40000) < p).astype(float)
    pred, obs, counts = reliability(y, p)
    assert counts.sum() == y.size
    # A calibrated forecast must land on the diagonal within sampling noise.
    np.testing.assert_allclose(pred, obs, atol=0.03)


def test_reliability_skips_empty_bins():
    y = np.array([0.0, 1.0])
    p = np.array([0.05, 0.95])
    pred, obs, counts = reliability(y, p)
    assert len(pred) == 2, "only the two populated bins should be returned"
    assert counts.sum() == 2
