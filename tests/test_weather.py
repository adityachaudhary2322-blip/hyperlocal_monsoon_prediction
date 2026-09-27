"""The national weather jobs: batching, the all-or-nothing rule, aggregation, grids.

No network: requests.get is replaced by a fake that answers like Open-Meteo's
multi-location endpoint (a list of per-location objects).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.jobs import weather, weather_grid
from src.report import DataError

CFG = {"endpoint": "https://example.invalid/v1/forecast", "batch_size": 100,
       "pause_seconds": 0, "retries": 1, "backoff_seconds": 0, "timeout_seconds": 5,
       "min_coverage": 0.9, "forecast_hours": 168}


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code, self._payload, self.text = status, payload, ""

    def json(self):
        return self._payload


def fake_point(temp=30.0, rain=0.5):
    return {"current": {"time": "2026-09-27T06:00", "temperature_2m": temp},
            "hourly": {"precipitation": [rain] * 168}}


def districts(n: int, states: int = 3):
    return [weather.District(f"D{i}", f"State {i % states}", f"s{i % states}",
                             20 + i * 0.01, 78 + i * 0.01) for i in range(n)]


@pytest.fixture()
def temp_db(tmp_path):
    from src.db import session as session_module

    session_module.use_url(f"sqlite:///{tmp_path / 'w.db'}")
    session_module.create_all()
    yield session_module
    session_module.use_url(None)


def test_batches_never_exceed_the_configured_size():
    sizes = [len(b) for b in weather.batches(list(range(734)), 100)]
    assert max(sizes) == 100 and sum(sizes) == 734 and len(sizes) == 8


def test_every_request_carries_at_most_100_locations():
    seen = []

    def get(url, params, timeout):
        n = len(params["latitude"].split(","))
        seen.append(n)
        return FakeResponse(200, [fake_point() for _ in range(n)])

    readings = weather.fetch(districts(734), CFG, get=get, sleep=lambda s: None)
    assert len(readings) == 734 and max(seen) <= 100 and len(seen) == 8


def test_24h_and_7d_totals_come_from_hourly_rain():
    r = weather.parse(fake_point(rain=0.5), districts(1)[0])
    assert r.precip_24h_mm == pytest.approx(12.0) and r.precip_7d_mm == pytest.approx(84.0)


def test_state_means_match_a_hand_computation():
    ds = [weather.District("a", "S", "s", 0, 0), weather.District("b", "S", "s", 0, 0)]
    t = dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc)
    readings = [weather.Reading(ds[0], t, 30.0, 10.0, 70.0),
                weather.Reading(ds[1], t, 20.0, None, 30.0)]
    (state,) = weather.aggregate(readings)
    assert state["temp_c"] == 25.0
    assert state["precip_24h_mm"] == 10.0      # a missing value is skipped, not zero
    assert state["precip_7d_mm"] == 50.0 and state["n_districts"] == 2


def test_a_failed_fetch_keeps_the_last_good_rows(temp_db, monkeypatch):
    from src.db.models import WeatherFetch, WeatherNow

    monkeypatch.setattr(weather, "load_districts", lambda cfg: districts(20))
    good = lambda url, params, timeout: FakeResponse(
        200, [fake_point(temp=31.0) for _ in params["latitude"].split(",")])
    weather.run(CFG, get=good, sleep=lambda s: None)

    down = lambda url, params, timeout: FakeResponse(503)
    with pytest.raises(DataError):
        weather.run(CFG, get=down, sleep=lambda s: None)

    with temp_db.session_scope() as s:
        rows = s.query(WeatherNow).all()
        assert len(rows) == 20 and all(r.temp_c == 31.0 for r in rows)
        log = [(f.ok, f.error is not None) for f in s.query(WeatherFetch).order_by(WeatherFetch.id)]
    assert log == [(True, False), (False, True)]


def test_short_coverage_is_a_failure(temp_db, monkeypatch):
    monkeypatch.setattr(weather, "load_districts", lambda cfg: districts(10))
    monkeypatch.setattr(weather, "fetch", lambda d, cfg, get, sleep: [
        weather.parse(fake_point(), x) for x in d[:5]])
    with pytest.raises(DataError, match="min_coverage"):
        weather.run(CFG)


def test_a_400_is_not_retried():
    calls = []

    def get(url, params, timeout):
        calls.append(1)
        return FakeResponse(400)

    with pytest.raises(DataError, match="HTTP 400"):
        weather._get("u", {}, {**CFG, "retries": 3}, get=get)
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Animation grid
# --------------------------------------------------------------------------
GRID = {"lat_min": 5.0, "lat_max": 8.0, "lon_min": 65.0, "lon_max": 68.0, "step": 1.5,
        "hours": 72, "days": 7, "min_coverage": 0.95}


def grid_point(speed=36.0, direction=270.0):
    return {"time": "2026-09-27T06:00", "days": [f"2026-09-{27 + d}" for d in range(4)] +
            [f"2026-10-0{d}" for d in range(1, 4)],
            "wind": (*weather_grid.to_uv(speed, direction), "850hPa"),
            "rain": [0.1 * h for h in range(72)], "daily": [float(d) for d in range(7)]}


def test_grid_is_row_major_from_the_south_west():
    points, nx, ny = weather_grid.grid_points(GRID)
    assert (nx, ny) == (3, 3) and points[0] == (5.0, 65.0) and points[1] == (5.0, 66.5)
    assert points[nx] == (6.5, 65.0)


def test_the_full_grid_matches_the_documented_request_budget():
    from src.common import load_config

    points, nx, ny = weather_grid.grid_points(load_config("weather")["grid"])
    assert (ny, nx) == (23, 24) and len(points) == 552     # 552 x 4 runs = 2,208 calls/day


def test_westerly_wind_blows_toward_the_east():
    u, v = weather_grid.to_uv(36.0, 270.0)       # from the west, 10 m/s
    assert u == pytest.approx(10.0) and abs(v) < 1e-9


def test_payloads_are_compact_integers_with_days_4_to_7():
    points, nx, ny = weather_grid.grid_points(GRID)
    named = weather_grid.payloads([grid_point() for _ in points], nx, ny, GRID)
    wind, rain = named["wind"], named["rain_forecast"]
    assert wind["u"][0] == 100 and wind["scale"] == 10
    assert len(rain["h"]) == 72 * len(points)
    assert [d["date"] for d in rain["days"]][0] == "2026-09-30"   # day 4
    assert len(rain["days"]) == 4
    assert all(isinstance(v, int) for v in rain["h"])
    assert len(json.dumps(named, separators=(",", ":"))) < 5_000_000


def test_missing_wind_everywhere_is_an_error():
    points, nx, ny = weather_grid.grid_points(GRID)
    calm = [{**grid_point(), "wind": None} for _ in points]
    with pytest.raises(DataError, match="wind at only"):
        weather_grid.payloads(calm, nx, ny, GRID)
