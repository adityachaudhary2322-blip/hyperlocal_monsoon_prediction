"""No feature may use rainfall from the start date onward.

The check is causal, not a code reading: build the rows twice, the second time with
every rainfall value from the start date onward replaced by nonsense, and assert the
feature columns are bit-identical while the targets move. If any feature peeked into
the horizon, that perturbation would change it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.build_features import (
    DRY_DAY_MM,
    DRY_SPELL_DAYS,
    HEAVY_RAIN_MM,
    HORIZONS,
    RAW_FEATURE_COLUMNS,
    RAW_TARGET_COLUMNS,
    TRAILING_WINDOWS,
    build_raw_rows,
)

YEAR = 2001
UNITS = ["U1", "U2"]
LAST_OBSERVED = pd.Timestamp(f"{YEAR}-12-31")


def make_rain(seed: int = 0) -> pd.DataFrame:
    """A plausible season: dry spring, wet monsoon, a few heavy days, a dry break."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(f"{YEAR}-01-01", f"{YEAR}-12-31", freq="D")
    data = {}
    for offset, unit in enumerate(UNITS):
        rain = rng.gamma(0.4, 2.0, size=len(dates))
        monsoon = (dates.month >= 6) & (dates.month <= 9)
        rain[monsoon] += rng.gamma(1.4, 5.0, size=int(monsoon.sum()))
        # A deliberate heavy day and a deliberate 12-day dry break.
        rain[dates.get_loc(pd.Timestamp(f"{YEAR}-07-{12 + offset:02d}"))] = 90.0
        i = dates.get_loc(pd.Timestamp(f"{YEAR}-08-{5 + offset:02d}"))
        rain[i : i + 12] = 0.0
        data[unit] = rain.astype("float32")
    return pd.DataFrame(data, index=dates)


def rows_at(rows: pd.DataFrame, start: pd.Timestamp) -> pd.DataFrame:
    """Rows for one start date, failing loudly if that date is not a start date.

    Start dates are May 15 + 5k, so a hand-picked date like 1 July is not one and an
    empty selection would make assertions like `.eq(0).all()` pass vacuously.
    """
    subset = rows[rows["start_date"] == start]
    assert not subset.empty, (
        f"{start:%Y-%m-%d} is not a forecast start date; valid ones are May 15 + 5k"
    )
    return subset.reset_index(drop=True)


def onset_map(date: str | None = f"{YEAR}-06-12") -> dict:
    stamp = pd.Timestamp(date) if date else pd.NaT
    return {(unit, YEAR): stamp for unit in UNITS}


# --------------------------------------------------------------------------
# The leakage guarantee
# --------------------------------------------------------------------------
def test_no_feature_changes_when_future_rainfall_is_destroyed():
    rain = make_rain()
    baseline = build_raw_rows(rain, onset_map(), LAST_OBSERVED)

    rng = np.random.default_rng(99)
    changed = 0
    for start in baseline["start_date"].unique():
        polluted = rain.copy()
        future = polluted.index >= start
        # Replace the whole future with large random rainfall.
        polluted.loc[future, :] = rng.uniform(
            50, 300, size=(int(future.sum()), len(UNITS))
        ).astype("float32")

        # Onset is held fixed: what is being tested here is the rainfall features.
        after = build_raw_rows(polluted, onset_map(), LAST_OBSERVED)

        a = rows_at(baseline, start)
        b = rows_at(after, start)

        for column in RAW_FEATURE_COLUMNS:
            pd.testing.assert_series_equal(
                a[column], b[column], check_names=False,
                obj=f"feature {column!r} at start {start:%Y-%m-%d}",
            )
        # Sanity: the perturbation must actually have bitten, or the test proves
        # nothing. Totals over the horizon have to change.
        if not np.allclose(a["total_7"], b["total_7"], equal_nan=True):
            changed += 1

    assert changed > 0, "perturbation never altered any target - test is vacuous"


def test_features_only_read_days_before_the_start_date():
    """Perturbing day s-1 must change trailing features; day s must not."""
    rain = make_rain()
    start = pd.Timestamp(f"{YEAR}-07-19")

    before = rain.copy()
    before.loc[start - pd.Timedelta(days=1)] = 500.0
    on = rain.copy()
    on.loc[start] = 500.0

    base = rows_at(build_raw_rows(rain, onset_map(), LAST_OBSERVED), start)
    pert_before = rows_at(build_raw_rows(before, onset_map(), LAST_OBSERVED), start)
    pert_on = rows_at(build_raw_rows(on, onset_map(), LAST_OBSERVED), start)

    for w in TRAILING_WINDOWS:
        column = f"rain_prev_{w}"
        assert not np.allclose(base[column], pert_before[column]), (
            f"{column} ignored day s-1, so it is not really a trailing window"
        )
        np.testing.assert_allclose(base[column], pert_on[column],
                                   err_msg=f"{column} leaked day s")


def test_targets_cover_exactly_h_days_from_the_start_date():
    rain = make_rain()
    # Zero everything, then put 10 mm on each of 28 days from a known start.
    rain.loc[:, :] = 0.0
    start = pd.Timestamp(f"{YEAR}-07-04")
    for k in range(28):
        rain.loc[start + pd.Timedelta(days=k), :] = 10.0

    rows = build_raw_rows(rain, onset_map(None), LAST_OBSERVED)
    row = rows_at(rows, start).iloc[0]
    for h in HORIZONS:
        assert row[f"total_{h}"] == pytest.approx(10.0 * h), (
            f"total_{h} should cover days s..s+{h - 1}"
        )


# --------------------------------------------------------------------------
# Target definitions
# --------------------------------------------------------------------------
def test_heavy_target_fires_only_at_or_above_the_threshold():
    rain = make_rain()
    rain.loc[:, :] = 0.0
    start = pd.Timestamp(f"{YEAR}-07-04")
    rain.loc[start + pd.Timedelta(days=3), :] = HEAVY_RAIN_MM - 0.1
    rows = build_raw_rows(rain, onset_map(None), LAST_OBSERVED)
    assert rows_at(rows, start)["heavy_7"].eq(0).all()

    rain.loc[start + pd.Timedelta(days=3), :] = HEAVY_RAIN_MM
    rows = build_raw_rows(rain, onset_map(None), LAST_OBSERVED)
    row = rows_at(rows, start)
    assert row["heavy_7"].eq(1).all()
    # A day 3 into the horizon is inside every horizon, so all fire.
    assert row["heavy_28"].eq(1).all()


def test_dry_target_needs_the_run_to_start_inside_the_horizon():
    rain = make_rain()
    rain.loc[:, :] = 5.0          # wet everywhere
    start = pd.Timestamp(f"{YEAR}-07-04")

    # A 10-day dry run starting on day 10 of the horizon: inside 14/21/28, not 7.
    run_start = start + pd.Timedelta(days=10)
    for k in range(DRY_SPELL_DAYS):
        rain.loc[run_start + pd.Timedelta(days=k), :] = DRY_DAY_MM - 0.1

    rows = build_raw_rows(rain, onset_map(None), LAST_OBSERVED)
    row = rows_at(rows, start).iloc[0]
    assert row["dry_7"] == 0
    assert row["dry_14"] == 1
    assert row["dry_28"] == 1


def test_dry_target_ignores_a_run_one_day_too_short():
    rain = make_rain()
    rain.loc[:, :] = 5.0
    start = pd.Timestamp(f"{YEAR}-07-04")
    run_start = start + pd.Timedelta(days=2)
    for k in range(DRY_SPELL_DAYS - 1):
        rain.loc[run_start + pd.Timedelta(days=k), :] = 0.0
    rows = build_raw_rows(rain, onset_map(None), LAST_OBSERVED)
    assert rows_at(rows, start)["dry_28"].eq(0).all()


def test_onset_target_is_nan_once_onset_has_happened():
    rain = make_rain()
    onset_date = pd.Timestamp(f"{YEAR}-06-12")
    rows = build_raw_rows(rain, onset_map(str(onset_date.date())), LAST_OBSERVED)

    # A start date before onset, with onset inside the 14-day horizon -> 1.
    start = pd.Timestamp(f"{YEAR}-06-09")
    row = rows_at(rows, start).iloc[0]
    assert row["onset_7"] == 1     # 9..15 June contains 12 June
    assert row["onset_14"] == 1

    # A start date after onset -> NaN for every horizon, for the rest of the season.
    for start in rows["start_date"].unique():
        if start > onset_date:
            row = rows_at(rows, start).iloc[0]
            for h in HORIZONS:
                assert np.isnan(row[f"onset_{h}"]), f"{start:%Y-%m-%d} h={h}"


def test_onset_target_is_zero_when_onset_falls_beyond_the_horizon():
    rain = make_rain()
    onset_date = pd.Timestamp(f"{YEAR}-07-20")
    rows = build_raw_rows(rain, onset_map(str(onset_date.date())), LAST_OBSERVED)
    start = pd.Timestamp(f"{YEAR}-06-14")     # 36 days before onset
    row = rows_at(rows, start).iloc[0]
    assert row["onset_7"] == 0
    assert row["onset_28"] == 0


def test_onset_happened_waits_for_the_confirmation_window_to_close():
    rain = make_rain()
    onset_date = pd.Timestamp(f"{YEAR}-06-12")
    rows = build_raw_rows(rain, onset_map(str(onset_date.date())), LAST_OBSERVED)

    # Default config confirms over accum_days + confirm_window_days = 33 days, so the
    # flag cannot be set before 15 July - anything earlier would need days >= s.
    knowable_from = onset_date + pd.Timedelta(days=33)
    for start in rows["start_date"].unique():
        flag = rows_at(rows, start)["onset_happened"].iloc[0]
        assert flag == (1.0 if start >= knowable_from else 0.0), (
            f"onset_happened at {start:%Y-%m-%d} (knowable from "
            f"{knowable_from:%Y-%m-%d})"
        )


def test_targets_are_nan_past_the_last_observed_day():
    rain = make_rain()
    last = pd.Timestamp(f"{YEAR}-07-15")
    rain.loc[rain.index > last, :] = np.nan
    rows = build_raw_rows(rain, onset_map(None), last)

    start = pd.Timestamp(f"{YEAR}-07-09")     # horizons run past 15 July
    row = rows_at(rows, start).iloc[0]
    for h in HORIZONS:
        if start + pd.Timedelta(days=h - 1) > last:
            assert np.isnan(row[f"total_{h}"]), f"total_{h} should be unknown"
            assert np.isnan(row[f"onset_{h}"]), f"onset_{h} should be unknown"


def test_every_declared_column_is_actually_produced():
    rows = build_raw_rows(make_rain(), onset_map(), LAST_OBSERVED)
    missing = set(RAW_FEATURE_COLUMNS + RAW_TARGET_COLUMNS) - set(rows.columns)
    assert not missing, f"declared but not produced: {sorted(missing)}"
