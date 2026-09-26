"""Onset and dry-spell logic on hand-made series where the right answer is obvious."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.onset import (
    OnsetConfig,
    _dry_spell_starts,
    detect_onset,
    first_dry_spell_start,
    forecast_start_dates,
    onset_config,
)

CFG = OnsetConfig(
    accum_mm=20.0,
    accum_days=3,
    dry_spell_days=7,
    dry_day_mm=1.0,
    confirm_window_days=30,
    search_start="05-15",
    search_end="09-30",
)

YEAR = 2001  # non-leap, keeps hand-counted offsets simple


def series(year: int = YEAR) -> tuple[np.ndarray, pd.DatetimeIndex]:
    """A full dry year: May 1 to Nov 30, zero rain everywhere."""
    dates = pd.date_range(f"{year}-05-01", f"{year}-11-30", freq="D")
    return np.zeros(len(dates), dtype="float64"), dates


def put(rain: np.ndarray, dates: pd.DatetimeIndex, day: str, values: list[float]) -> None:
    i = dates.get_loc(pd.Timestamp(day))
    rain[i : i + len(values)] = values


# --------------------------------------------------------------------------
# Toy series 1: a true onset
# --------------------------------------------------------------------------
def test_true_onset_is_detected_on_the_first_trigger_day():
    rain, dates = series()
    # 8+7+6 = 21 mm >= 20 mm over 3 days, starting 10 June.
    put(rain, dates, f"{YEAR}-06-10", [8.0, 7.0, 6.0])
    # Then a wet monsoon: 5 mm every day for 40 days, so no 7-day dry spell can
    # begin inside the 30-day confirmation window.
    put(rain, dates, f"{YEAR}-06-13", [5.0] * 40)

    assert detect_onset(rain, dates, CFG) == pd.Timestamp(f"{YEAR}-06-10")


def test_onset_before_the_search_window_is_ignored():
    rain, dates = series()
    # A qualifying burst on 2 May, well before search_start (15 May).
    put(rain, dates, f"{YEAR}-05-02", [10.0, 10.0, 10.0])
    put(rain, dates, f"{YEAR}-05-05", [5.0] * 40)
    # ...and a genuine one in mid-June.
    put(rain, dates, f"{YEAR}-06-20", [7.0, 7.0, 7.0])
    put(rain, dates, f"{YEAR}-06-23", [5.0] * 40)

    assert detect_onset(rain, dates, CFG) == pd.Timestamp(f"{YEAR}-06-20")


# --------------------------------------------------------------------------
# Toy series 2: a false onset, rejected by the dry-spell rule
# --------------------------------------------------------------------------
def test_false_onset_followed_by_a_dry_spell_is_rejected():
    rain, dates = series()
    # Trigger on 1 June (21 mm over 3 days) then nothing at all: the confirmation
    # window is full of dry days, so this is a false start.
    put(rain, dates, f"{YEAR}-06-01", [8.0, 7.0, 6.0])
    # A real onset on 15 July, this time sustained.
    put(rain, dates, f"{YEAR}-07-15", [9.0, 6.0, 6.0])
    put(rain, dates, f"{YEAR}-07-18", [4.0] * 40)

    assert detect_onset(rain, dates, CFG) == pd.Timestamp(f"{YEAR}-07-15")


def test_a_dry_spell_one_day_short_does_not_reject():
    rain, dates = series()
    put(rain, dates, f"{YEAR}-06-01", [8.0, 7.0, 6.0])
    # Confirmation window starts 4 June. Give exactly 6 dry days then rain: 6 < 7,
    # so no qualifying dry spell begins and the onset stands.
    put(rain, dates, f"{YEAR}-06-04", [0.0] * 6)
    put(rain, dates, f"{YEAR}-06-10", [3.0] * 40)

    assert detect_onset(rain, dates, CFG) == pd.Timestamp(f"{YEAR}-06-01")


def test_a_seven_day_dry_spell_rejects_but_a_light_day_breaks_it():
    rain, dates = series()
    put(rain, dates, f"{YEAR}-06-01", [8.0, 7.0, 6.0])
    # 7 dry days from 4 June -> rejected.
    put(rain, dates, f"{YEAR}-06-04", [0.0] * 7)
    put(rain, dates, f"{YEAR}-06-11", [3.0] * 40)
    assert detect_onset(rain, dates, CFG) != pd.Timestamp(f"{YEAR}-06-01")

    # Same shape, but day 4 of the run gets 1.0 mm, which is NOT below dry_day_mm
    # of 1.0, so the run is broken into 3 + 3 and the onset stands.
    rain2, dates2 = series()
    put(rain2, dates2, f"{YEAR}-06-01", [8.0, 7.0, 6.0])
    put(rain2, dates2, f"{YEAR}-06-04", [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])
    put(rain2, dates2, f"{YEAR}-06-11", [3.0] * 40)
    assert detect_onset(rain2, dates2, CFG) == pd.Timestamp(f"{YEAR}-06-01")


# --------------------------------------------------------------------------
# Toy series 3: a dry spell, and no onset at all
# --------------------------------------------------------------------------
def test_no_onset_when_rain_never_reaches_the_threshold():
    rain, dates = series()
    # 6 mm/day for the whole season: any 3-day window sums to 18 mm < 20 mm.
    put(rain, dates, f"{YEAR}-05-15", [6.0] * 120)
    assert detect_onset(rain, dates, CFG) is None


def test_dry_spell_start_is_the_first_day_of_the_run():
    rain, dates = series()
    put(rain, dates, f"{YEAR}-06-01", [5.0] * 20)   # wet 1-20 June
    # 6 dry days (too short), rain, then 12 dry days from 28 June.
    put(rain, dates, f"{YEAR}-06-21", [0.0] * 6)
    put(rain, dates, f"{YEAR}-06-27", [5.0])
    put(rain, dates, f"{YEAR}-06-28", [0.0] * 12)
    put(rain, dates, f"{YEAR}-07-10", [5.0] * 30)

    found = first_dry_spell_start(
        rain, dates, dry_day_mm=2.5, dry_spell_days=10,
        window=(pd.Timestamp(f"{YEAR}-06-01"), pd.Timestamp(f"{YEAR}-07-31")),
    )
    assert found == pd.Timestamp(f"{YEAR}-06-28")


def test_dry_spell_must_start_inside_the_window():
    rain, dates = series()
    put(rain, dates, f"{YEAR}-06-01", [5.0] * 27)
    put(rain, dates, f"{YEAR}-06-28", [0.0] * 12)   # run starts 28 June
    put(rain, dates, f"{YEAR}-07-10", [5.0] * 30)

    # A window ending 27 June must not see a run that starts on the 28th, even
    # though the run overlaps nothing in the window.
    assert first_dry_spell_start(
        rain, dates, dry_day_mm=2.5, dry_spell_days=10,
        window=(pd.Timestamp(f"{YEAR}-06-01"), pd.Timestamp(f"{YEAR}-06-27")),
    ) is None


def test_nan_breaks_a_dry_run_rather_than_extending_it():
    rain = np.zeros(20)
    rain[5] = np.nan
    starts = _dry_spell_starts(rain, dry_day_mm=1.0, dry_spell_days=7)
    # A run starting at 0 would need days 0-6 all dry, but day 5 is unknown.
    assert not starts[0]
    # From day 6 onward there are 14 dry days, so day 6 does start a run.
    assert starts[6]


def test_late_candidate_without_enough_data_is_not_declared():
    # Series stops on 30 September, so a trigger on the 28th cannot be confirmed.
    dates = pd.date_range(f"{YEAR}-05-01", f"{YEAR}-09-30", freq="D")
    rain = np.zeros(len(dates))
    i = dates.get_loc(pd.Timestamp(f"{YEAR}-09-28"))
    rain[i : i + 3] = [8.0, 7.0, 6.0]
    assert detect_onset(rain, dates, CFG) is None


# --------------------------------------------------------------------------
# Config plumbing and the forecast calendar
# --------------------------------------------------------------------------
def test_config_defaults_match_the_specification():
    cfg = onset_config()
    assert (cfg.accum_mm, cfg.accum_days) == (20.0, 3)
    assert (cfg.dry_spell_days, cfg.dry_day_mm) == (7, 1.0)
    assert cfg.confirm_window_days == 30


def test_unknown_zone_falls_back_to_defaults():
    assert onset_config(zone_id="NOT-A-ZONE") == onset_config()


@pytest.mark.parametrize("year", [2001, 2004, 2024])
def test_forecast_start_dates_are_stable_across_leap_years(year):
    dates = forecast_start_dates(year)
    assert dates[0] == pd.Timestamp(f"{year}-05-15")
    assert dates[-1] <= pd.Timestamp(f"{year}-09-30")
    assert (np.diff(dates.values).astype("timedelta64[D]").astype(int) == 5).all()
    # Anchored on month-day, so every year issues on the same calendar dates.
    assert [(d.month, d.day) for d in dates] == [
        (d.month, d.day) for d in forecast_start_dates(2001)
    ]
