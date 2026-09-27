"""Live engine: event definitions, blend, outlook, observed-rain assembly, call weights.

Synthetic series only - no network, no database.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src import labels
from src.live import engine, observed
from src.live.openmeteo import call_weight
from src.onset import onset_config

WET, DRY = 8.0, 0.0


def series(days: int, fill: float = WET) -> np.ndarray:
    return np.full((1, days), fill, dtype=np.float32)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def test_dry_spell_must_start_inside_the_window():
    r = series(60)
    r[0, 12:24] = DRY                        # 12 dry days starting at day 12
    assert labels.dry_spell(r, s=10, h=7)[0] == 1.0      # starts on day 12, inside 10..16
    assert labels.dry_spell(r, s=13, h=7)[0] == 1.0      # 13..22 is itself a 10-day run
    assert labels.dry_spell(r, s=16, h=7)[0] == 0.0      # no 10-day run starts at 16+


def test_nine_dry_days_are_not_a_dry_spell():
    r = series(60)
    r[0, 12:21] = DRY
    assert labels.dry_spell(r, s=10, h=14)[0] == 0.0


def test_heavy_rain_threshold_is_64_5():
    r = series(40)
    r[0, 15] = 64.4
    assert labels.heavy_rain(r, 10, 7)[0] == 0.0
    r[0, 15] = 64.5
    assert labels.heavy_rain(r, 10, 7)[0] == 1.0
    assert labels.heavy_rain(r, 16, 7)[0] == 0.0         # outside the window


def test_late_heavy_needs_a_harvest_day():
    r = series(40)
    r[0, 12] = 80.0
    none = np.zeros((1, 40), bool)
    some = none.copy()
    some[0, 12] = True
    assert np.isnan(labels.late_heavy_rain(r, 10, 7, none)[0])
    assert labels.late_heavy_rain(r, 10, 7, some)[0] == 1.0


def test_harvest_window_wraps_the_year_end():
    dates = pd.date_range("2026-12-25", "2027-01-10")
    mask = labels.harvest_days(dates, [("12-28", "01-05")])
    assert mask[3] and mask[10] and not mask[0] and not mask[-1]


def test_rabi_moisture_short_compares_with_the_normal():
    r = series(30, 2.0)                       # 2 mm/day
    assert labels.rabi_moisture_short(r, 20, 7, 0, np.array([100.0]))[0] == 1.0   # 54 < 80
    assert labels.rabi_moisture_short(r, 20, 7, 0, np.array([50.0]))[0] == 0.0    # 54 > 40


def _dates(n: int, start="2026-05-15"):
    return pd.date_range(start, periods=n)


def test_onset_already_happened_is_not_forecast():
    cfg = onset_config()
    dates = _dates(140)
    rain = np.full(140, WET)                   # wet from 15 May: onset long confirmed
    already, when = labels.onset_forecast(rain, dates, 120, cfg)
    assert already and when is None


def test_onset_in_the_forecast_window_is_found():
    cfg = onset_config()
    dates = _dates(90)
    rain = np.zeros(90)
    s = 40                                     # today = 24 Jun, dry so far
    rain[s + 3:] = WET                         # rains begin 3 days in, and continue
    already, when = labels.onset_forecast(rain, dates, s, cfg)
    assert not already and when == dates[s + 3]


# --------------------------------------------------------------------------
# blend, levels, outlook
# --------------------------------------------------------------------------
def test_blend_uses_ec46_alone_while_ml_is_coming_soon():
    p = np.array([0.2, 0.7])
    out, basis = engine.blend(p, None, "dry", 7)
    assert basis == "ec46_only" and np.allclose(out, p)


def test_blend_weights_default_to_half():
    out, basis = engine.blend(np.array([0.2]), np.array([0.6]), "dry", 7)
    assert basis == "blend" and out[0] == pytest.approx(0.4)


def test_levels_follow_the_rule_bands():
    assert engine.levels(np.array([0.1, 0.45, 0.8, np.nan])) == ["green", "amber", "red", "none"]


def test_outlook_terciles_sum_to_one(monkeypatch):
    from src.live import assets

    weeks = 52
    clim = {"mean": np.full((weeks, 2), 20.0, np.float32),
            "p33": np.full((weeks, 2), 10.0, np.float32),
            "p67": np.full((weeks, 2), 30.0, np.float32)}
    monkeypatch.setattr(assets, "climatology", lambda: clim)
    rng = np.random.default_rng(0)
    ec = rng.gamma(2.0, 2.0, size=(51, 2, 46)).astype(np.float32)
    look = engine.outlook(ec, dt.date(2026, 7, 1), 4)
    total = look["below"] + look["near"] + look["above"]
    assert np.allclose(total, 1.0)
    assert (look["p10"] <= look["p50"]).all() and (look["p50"] <= look["p90"]).all()


def test_season_switches_on_1_october():
    assert engine.season_for(dt.date(2026, 7, 1))[1] == ["onset", "dry", "heavy"]
    # after mid-August onset is no longer forecast (config/season.yaml hazard_until)
    assert engine.season_for(dt.date(2026, 9, 30))[1] == ["dry", "heavy"]
    assert engine.season_for(dt.date(2026, 10, 1))[1] == ["withdrawal", "late_heavy",
                                                         "rabi_moisture"]
    assert engine.season_for(dt.date(2026, 1, 10))[1] == []


def test_probabilities_are_member_fractions():
    units = pd.DataFrame({"unit_id": ["a", "b"], "state": ["Bihar", "Bihar"],
                          "district": ["Gaya", "Gaya"]})
    s, n_members = 30, 4
    ser = np.full((n_members, 2, 80), WET, dtype=np.float32)
    ser[:2, 0, s + 2:s + 14] = DRY             # half the members dry for unit a
    dates = _dates(80)
    p = engine.probabilities(ser, dates, s, dates[s].date(), ["dry"], [7], units)
    assert p[("dry", 7)][0] == pytest.approx(0.5) and p[("dry", 7)][1] == 0.0


# --------------------------------------------------------------------------
# observed rain, call weights
# --------------------------------------------------------------------------
def test_bias_ratio_scales_open_meteo_to_imd():
    d = [dt.date(2026, 9, i) for i in range(1, 4)]
    imd = {day: np.array([10.0, 0.0]) for day in d[:2]}
    om = {day: np.array([5.0, 0.0]) for day in d}
    ratio = observed.bias_ratio(imd, om, (0.3, 3.0))
    assert ratio[0] == pytest.approx(21 / 11) and ratio[1] == pytest.approx(1.0)


def test_assemble_prefers_fresh_imd_then_stored_then_adjusted(monkeypatch):
    from src.live import assets

    monkeypatch.setattr(assets, "units", lambda: pd.DataFrame({"unit_id": ["a"]}))
    d = [dt.date(2026, 9, i) for i in range(1, 5)]
    obs, src = observed.assemble(
        d, stored={d[1]: (np.array([3.0]), "imd")}, imd={d[0]: np.array([1.0])},
        om={d[2]: np.array([4.0])}, ratio=np.array([2.0]))
    assert src == ["imd", "imd", "om_adj", "missing"]
    assert obs[0, 2] == 8.0 and np.isnan(obs[0, 3])


def test_ec46_weight_matches_the_documented_budget():
    assert call_weight(51, 46) == pytest.approx(5.1 * 46 / 14)
    assert call_weight(3, 46) == pytest.approx(46 / 14)
    assert call_weight(6, 21) == pytest.approx(1.5)
    assert 188 * call_weight(51, 46) < 3200


def test_client_paces_itself_inside_the_hourly_limit():
    """Three requests of 2,000 calls must not all land in one hour (limit 4,500)."""
    from src.live.openmeteo import Client

    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    def get(url, params, timeout):
        n = len(params["latitude"].split(","))

        class R:
            status_code = 200

            def json(self_inner):
                return [{"daily": {}} for _ in range(n)]
        return R()

    c = Client({"attempts": 1, "backoff_seconds": 0, "timeout_seconds": 5}, get=get,
               sleep=sleep, rate={"per_minute": 5000, "per_hour": 4500}, clock=lambda: now[0])
    lat = np.zeros(3)
    for _ in range(3):
        c.fetch("u", lat[:1], lat[:1], {}, 1, 0, 2000.0, "t")
    assert c.calls == 6000 and now[0] >= 3600
