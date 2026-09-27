"""Fetch the animation grid: 850 hPa wind and 72 h / 7 d rain over India from Open-Meteo.

Run:  python -m src.jobs.weather_grid [--db-url-env DATABASE_URL_CLOUD] [--write-static]

Runs every 6 hours from .github/workflows/weather-grid.yml. The grid, the rate budget and
the provider's terms are in config/weather.yaml (`grid:`).

Two JSON payloads are stored in the `weather_grid` table:

* **wind** - u/v (m/s x 10, integers) at the current hour, 850 hPa where the model has
  it, else 10 m, per point. Drives the wind-flow particles.
* **rain_forecast** - hourly rain for 72 h (mm x 10, integers, frame-major) and daily
  totals for days 4-7. Drives the rain playback.

Both grids are row-major from the south-west corner: index = row * nx + col, row 0 at
lat_min, col 0 at lon_min. Same all-or-nothing rule as src.jobs.weather: a failed or
short run replaces nothing and exits non-zero.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from typing import Callable

import requests

from src.common import ROOT, load_config
from src.jobs.weather import _get, batches
from src.report import DataError, summarize

STATIC = ROOT / "app" / "static"


def grid_points(g: dict) -> tuple[list[tuple[float, float]], int, int]:
    """[(lat, lon)] row-major from the south-west corner, plus (nx, ny)."""
    ny = int(round((g["lat_max"] - g["lat_min"]) / g["step"])) + 1
    nx = int(round((g["lon_max"] - g["lon_min"]) / g["step"])) + 1
    points = [(round(g["lat_min"] + r * g["step"], 3), round(g["lon_min"] + c * g["step"], 3))
              for r in range(ny) for c in range(nx)]
    return points, nx, ny


def to_uv(speed_kmh: float, direction_deg: float) -> tuple[float, float]:
    """Meteorological convention: direction is where the wind comes FROM."""
    speed = speed_kmh / 3.6
    rad = math.radians(direction_deg)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def _first(values: list | None):
    return values[0] if values else None


def parse_point(item: dict, hours: int, days: int) -> dict:
    hourly = item.get("hourly") or {}
    daily = item.get("daily") or {}
    wind = None
    for level in ("850hPa", "10m"):
        s = _first(hourly.get(f"wind_speed_{level}"))
        d = _first(hourly.get(f"wind_direction_{level}"))
        if s is not None and d is not None:
            wind = (*to_uv(float(s), float(d)), level)
            break
    rain = hourly.get("precipitation") or []
    if len(rain) < hours:
        raise DataError(f"expected {hours} hourly rain values, got {len(rain)}")
    day_totals = daily.get("precipitation_sum") or []
    if len(day_totals) < days:
        raise DataError(f"expected {days} daily totals, got {len(day_totals)}")
    return {
        "time": (hourly.get("time") or [None])[0],
        "days": daily.get("time") or [],
        "wind": wind,
        "rain": [None if v is None else float(v) for v in rain[:hours]],
        "daily": [None if v is None else float(v) for v in day_totals[:days]],
    }


def fetch_grid(cfg: dict, get: Callable = requests.get,
               sleep: Callable = time.sleep) -> tuple[list[dict], int, int]:
    g = cfg["grid"]
    points, nx, ny = grid_points(g)
    levels = g["wind_levels"]
    hourly = ["precipitation"] + [f"wind_{kind}_{lvl}" for lvl in levels
                                  for kind in ("speed", "direction")]
    out: list[dict] = []
    groups = list(batches(points, g["batch_size"]))
    for index, group in enumerate(groups):
        if index:
            sleep(g["pause_seconds"])
        params = {
            "latitude": ",".join(f"{p[0]:.3f}" for p in group),
            "longitude": ",".join(f"{p[1]:.3f}" for p in group),
            "hourly": ",".join(hourly),
            "forecast_hours": g["hours"],
            "daily": "precipitation_sum",
            "forecast_days": g["days"],
            "timezone": "GMT",
        }
        items = _get(cfg["endpoint"], params, cfg, get=get)
        if len(items) != len(group):
            raise DataError(f"grid batch {index + 1}: asked {len(group)}, got {len(items)}")
        out.extend(parse_point(item, g["hours"], g["days"]) for item in items)
        print(f"  grid batch {index + 1}/{len(groups)}: {len(group)} points")
    return out, nx, ny


def payloads(points: list[dict], nx: int, ny: int, g: dict) -> dict[str, dict]:
    header = {"lat0": g["lat_min"], "lon0": g["lon_min"], "step": g["step"],
              "nx": nx, "ny": ny}
    with_wind = [p for p in points if p["wind"]]
    if len(with_wind) / len(points) < g["min_coverage"]:
        raise DataError(f"wind at only {len(with_wind)}/{len(points)} points")
    levels = sorted({p["wind"][2] for p in with_wind})
    wind = {**header, "t": points[0]["time"], "levels": levels, "scale": 10,
            "u": [round(p["wind"][0] * 10) if p["wind"] else None for p in points],
            "v": [round(p["wind"][1] * 10) if p["wind"] else None for p in points]}

    hours = g["hours"]
    frames = []
    for h in range(hours):
        frames.extend(None if p["rain"][h] is None else round(p["rain"][h] * 10)
                      for p in points)
    first_days = hours // 24          # days 1-3 are covered hour by hour
    rain = {**header, "t0": points[0]["time"], "hours": hours, "scale": 10,
            "unit": "mm per hour x 10", "h": frames,
            "days": [{"date": points[0]["days"][d],
                      "v": [None if p["daily"][d] is None else round(p["daily"][d] * 10)
                            for p in points]}
                     for d in range(first_days, g["days"])]}
    return {"wind": wind, "rain_forecast": rain}


def store(named: dict[str, dict], n_points: int, fetched: dt.datetime) -> None:
    from src.db.models import WeatherGrid
    from src.db.session import create_all, session_scope

    create_all()
    with session_scope() as session:
        for name, body in named.items():
            row = session.get(WeatherGrid, name)
            text = json.dumps(body, separators=(",", ":"))
            if row is None:
                session.add(WeatherGrid(name=name, payload=text, fetched_utc=fetched,
                                        n_points=n_points))
            else:
                row.payload, row.fetched_utc, row.n_points = text, fetched, n_points


def write_static(named: dict[str, dict]) -> list:
    written = []
    for name, body in named.items():
        path = STATIC / f"{name}.json"
        path.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url-env", default=None, metavar="NAME",
                        help="name of an environment variable holding the database URL")
    parser.add_argument("--write-static", action="store_true",
                        help="also write app/static/{wind,rain_forecast}.json locally")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and summarise without touching the database")
    args = parser.parse_args(argv)
    if args.db_url_env:
        from src.db.session import use_url
        from src.runtime import env as read_env

        value = read_env(args.db_url_env, "") or ""
        if not value.strip():
            raise DataError(f"{args.db_url_env} is not set")
        use_url(value)

    cfg = load_config("weather")
    print("--- jobs.weather_grid ---")
    fetched = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    points, nx, ny = fetch_grid(cfg)
    named = payloads(points, nx, ny, cfg["grid"])
    sizes = {k: len(json.dumps(v, separators=(",", ":"))) for k, v in named.items()}
    files = []
    if not args.dry_run:
        store(named, len(points), fetched)
    if args.write_static:
        files = write_static(named)
    wettest = max(named["rain_forecast"]["h"], key=lambda v: v or 0) / 10
    summarize(
        "jobs.weather_grid",
        rows=len(points),
        files=files or None,
        date_range=(named["rain_forecast"]["t0"],
                    named["rain_forecast"]["days"][-1]["date"]),
        extra={"grid": f"{ny} x {nx} = {len(points)} points",
               "wind levels": ", ".join(named["wind"]["levels"]),
               "payload sizes": ", ".join(f"{k} {v / 1e3:.0f} kB" for k, v in sizes.items()),
               "max hourly rain": f"{wettest:.1f} mm/h",
               "database": "(dry run)" if args.dry_run else "weather_grid updated"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
