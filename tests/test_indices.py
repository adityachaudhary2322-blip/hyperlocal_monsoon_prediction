"""Climate indices must be daily, complete, and free of look-ahead.

The publication-lag model is the point: a monthly value describes month M but is not
knowable until M has ended (or, for ONI's 3-month mean, until M+1 has ended). Getting
that wrong would leak the rest of the month into every feature built on it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.common import INDICES_PARQUET
from src.fetch_indices import SEASON_CENTRE, to_daily

pytestmark = pytest.mark.skipif(
    not INDICES_PARQUET.is_file(), reason="indices.parquet not built yet"
)


@pytest.fixture(scope="module")
def indices():
    return pd.read_parquet(INDICES_PARQUET)


def test_daily_and_gap_free(indices):
    dates = pd.DatetimeIndex(indices["date"])
    assert dates.is_monotonic_increasing
    assert not dates.has_duplicates
    steps = np.diff(dates.values).astype("timedelta64[D]").astype(int)
    assert (steps == 1).all(), "the index table is not a gap-free daily series"


def test_required_columns_present(indices):
    required = {"oni", "dmi", "nino34", "rmm1", "rmm2", "amplitude",
                "oni_age_days", "dmi_age_days", "nino34_age_days", "mjo_age_days"}
    assert required <= set(indices.columns)


def test_mjo_phase_is_an_integer_in_one_to_eight(indices):
    phase = indices["phase"].dropna()
    assert phase.between(1, 8).all(), "MJO phase outside 1-8"


def test_amplitude_matches_rmm_magnitude(indices):
    """amplitude should be sqrt(rmm1^2 + rmm2^2) - a check on the source parse."""
    rows = indices.dropna(subset=["rmm1", "rmm2", "amplitude"])
    computed = np.sqrt(rows["rmm1"] ** 2 + rows["rmm2"] ** 2)
    np.testing.assert_allclose(rows["amplitude"], computed, rtol=1e-3, atol=1e-3)


def test_ages_are_never_negative(indices):
    """A negative age would mean the value describes a month in the future."""
    for column in ("oni_age_days", "dmi_age_days", "nino34_age_days", "mjo_age_days"):
        ages = indices[column].dropna()
        assert (ages >= 0).all(), f"{column} has negative ages (look-ahead)"


def test_monthly_ages_are_at_least_a_month(indices):
    """A monthly value cannot be younger than the month it describes.

    ONI additionally spans M-1..M+1, so it can never be fresher than ~2 months.
    """
    assert indices["nino34_age_days"].dropna().min() >= 28
    assert indices["dmi_age_days"].dropna().min() >= 28
    assert indices["oni_age_days"].dropna().min() >= 59


def test_mjo_is_never_same_day(indices):
    """BoM publishes with a lag, so day D's RMM is only readable from D+1."""
    assert indices["mjo_age_days"].dropna().min() >= 1


def test_to_daily_never_reveals_a_month_before_it_ends():
    """The direct statement of the no-leakage rule for monthly series."""
    monthly = pd.Series(
        {pd.Timestamp("2020-05-01"): 1.0,
         pd.Timestamp("2020-06-01"): 2.0,
         pd.Timestamp("2020-07-01"): 3.0},
        name="demo",
    )
    days = pd.date_range("2020-05-01", "2020-08-05", freq="D")
    out = to_daily(monthly, days, months_until_published=1)

    # Through June, June's own value must not be visible.
    assert pd.isna(out.loc["2020-05-20", "demo"])      # nothing published yet
    assert out.loc["2020-06-01", "demo"] == 1.0        # May, published 1 June
    assert out.loc["2020-06-30", "demo"] == 1.0        # still May
    assert out.loc["2020-07-01", "demo"] == 2.0        # June, published 1 July
    assert out.loc["2020-08-01", "demo"] == 3.0        # July, published 1 August

    # Age is measured from the month the value describes.
    assert out.loc["2020-07-01", "demo_age_days"] == 30    # 1 June -> 1 July


def test_to_daily_with_two_month_lag_matches_oni_semantics():
    monthly = pd.Series(
        {pd.Timestamp("2020-06-01"): 9.0, pd.Timestamp("2020-07-01"): 10.0},
        name="oni",
    )
    days = pd.date_range("2020-06-01", "2020-09-30", freq="D")
    out = to_daily(monthly, days, months_until_published=2)
    # The value centred on June spans May-July, so it cannot be known before August.
    assert pd.isna(out.loc["2020-07-31", "oni"])
    assert out.loc["2020-08-01", "oni"] == 9.0
    assert out.loc["2020-09-01", "oni"] == 10.0


def test_season_centre_map_is_complete_and_ordered():
    assert len(SEASON_CENTRE) == 12
    assert SEASON_CENTRE["DJF"] == 1      # DJF is centred on January
    assert SEASON_CENTRE["JJA"] == 7
    assert sorted(SEASON_CENTRE.values()) == list(range(1, 13))
