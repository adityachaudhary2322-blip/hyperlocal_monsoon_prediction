"""Scoring (src/accuracy.py) and nightly live verification (src/live/verify.py)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.accuracy import auc, score, sentences, suggest_blend_weights, verdict


def test_perfect_forecasts_have_full_skill():
    y = np.array([0, 1, 0, 1, 1, 0])
    s = score(y, y.astype(float), np.full(6, 0.5))
    assert s["bss"] == pytest.approx(1.0) and s["auc"] == 1.0
    assert s["hit_rate"] == 1.0 and s["false_alarm_ratio"] == 0.0


def test_climatology_itself_scores_zero():
    y = np.array([0, 1, 0, 0])
    s = score(y, np.full(4, 0.25))            # sample base rate as the reference
    assert s["bss"] == pytest.approx(0.0) and verdict(s["bss"]) == "about the same"


def test_auc_handles_ties_and_one_class():
    assert auc(np.array([0, 1, 0, 1]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert auc(np.array([1, 1]), np.array([0.2, 0.9])) is None


def test_reliability_sentence_reads_in_tens():
    y = np.array([1] * 7 + [0] * 3)
    s = score(y, np.full(10, 0.8))
    assert "When we said high chance (over 60%), it happened 7 in 10 times" in sentences(s)[0]


def test_no_single_overall_accuracy_is_reported():
    s = score(np.array([0, 1]), np.array([0.1, 0.9]))
    assert "accuracy" not in s


def test_blend_suggestion_prefers_the_better_model():
    rng = np.random.default_rng(1)
    y = rng.integers(0, 2, 400)
    frame = pd.DataFrame({"hazard": "dry", "horizon": 7, "outcome": y.astype(bool),
                          "p_ml": np.where(y == 1, 800, 200),           # sharp and right
                          "p_ec46": rng.integers(0, 1000, 400)})        # noise
    s = suggest_blend_weights(frame)["dry_7"]
    assert s["suggested_w"] >= 0.8 and s["brier_ml_only"] < s["brier_ec46_only"]


def test_blend_suggestion_waits_for_ml():
    frame = pd.DataFrame({"hazard": "dry", "horizon": 7, "outcome": [True] * 50,
                          "p_ml": [None] * 50, "p_ec46": [500] * 50})
    assert suggest_blend_weights(frame)["dry_7"]["suggested_w"] is None


def test_verify_scores_a_closed_monday_run_once(tmp_path, monkeypatch):
    from src.db import session as session_module
    from src.db.models import LiveForecast, LiveRun, LiveVerification, ObsUnitRain
    from src.live import assets, verify

    units = pd.DataFrame({"unit_id": ["u1", "u2"], "state": ["Bihar", "Bihar"],
                          "district": ["Gaya", "Gaya"], "tier": ["validated", "validated"]})
    monkeypatch.setattr(assets, "units", lambda: units)
    session_module.use_url(f"sqlite:///{tmp_path / 'v.db'}")
    session_module.create_all()
    monday = dt.date(2026, 7, 6)
    assert monday.weekday() == 0
    with session_module.session_scope() as s:
        run = LiveRun(run_date=monday, run_type="live", season="monsoon", hazards="dry",
                      sources="{}", n_units=2)
        s.add(run)
        s.flush()
        for u, p in (("u1", 800), ("u2", 100)):
            s.add(LiveForecast(run_id=run.id, unit_id=u, hazard="dry", horizon=7, p_blend=p,
                               p_ec46=p, level="red" if p > 600 else "green"))
        day, rows = dt.date(2026, 5, 15), []
        while day <= monday + dt.timedelta(days=40):
            dry_u1 = monday + dt.timedelta(days=2) <= day <= monday + dt.timedelta(days=14)
            rows += [ObsUnitRain(unit_id="u1", date=day, rain_mm=0.0 if dry_u1 else 8.0, source="imd"),
                     ObsUnitRain(unit_id="u2", date=day, rain_mm=8.0, source="imd")]
            day += dt.timedelta(days=1)
        s.add_all(rows)
        # a Tuesday run must never be scored
        s.add(LiveRun(run_date=monday + dt.timedelta(days=1), run_type="live",
                      season="monsoon", hazards="dry", sources="{}", n_units=2))
    try:
        with session_module.session_scope() as s:
            n = verify.verify(s, monday + dt.timedelta(days=41))
        with session_module.session_scope() as s:
            got = {(v.unit_id, v.horizon): v.outcome for v in s.query(LiveVerification)}
            again = verify.verify(s, monday + dt.timedelta(days=41))
    finally:
        session_module.use_url(None)
    assert n == 2 and again == 0
    assert got == {("u1", 7): True, ("u2", 7): False}
