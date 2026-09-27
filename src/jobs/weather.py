"""Fetch current weather and 7-day rain for every district in India from Open-Meteo.

Run:  python -m src.jobs.weather [--db-url-env DATABASE_URL_CLOUD] [--dry-run]

Runs every 3 hours from .github/workflows/weather.yml so the public map stays fresh
while the laptop is off. Settings and the provider's terms are in config/weather.yaml.

**All or nothing.** Every batch is fetched first; only then are `weather_now` and
`weather_state` replaced, in one transaction. A network failure, a malformed response or
coverage under `min_coverage` leaves the previous rows untouched, records the failure in
`weather_fetch`, and exits non-zero (CLAUDE.md §11) - the public page keeps showing the
last good values with their real timestamp instead of a half-empty map.

Uses only requests + SQLAlchemy + the standard library, so the GitHub Actions job installs
requirements-weather.txt and not the whole site.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

from src.common import ROOT, load_config
from src.report import DataError, summarize


@dataclass(frozen=True)
class District:
    district: str
    state: str
    state_key: str
    lat: float
    lon: float


@dataclass
class Reading:
    district: District
    valid_utc: dt.datetime
    temp_c: float | None
    precip_24h_mm: float | None
    precip_7d_mm: float | None


def load_districts(cfg: dict) -> list[District]:
    path = ROOT / cfg["centroids"]
    if not path.is_file():
        raise DataError(f"missing {path}; run python scripts/build_public_map_assets.py")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [District(r["district"], r["state"], r["state_key"],
                         float(r["lat"]), float(r["lon"]))
                for r in csv.DictReader(fh)]
    if not rows:
        raise DataError(f"{path} has no districts")
    return rows


def batches(items: list, size: int) -> Iterable[list]:
    if size < 1:
        raise ValueError("batch size must be >= 1")
    for start in range(0, len(items), size):
        yield items[start:start + size]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _get(url: str, params: dict, cfg: dict, get: Callable = requests.get) -> list:
    """GET with retry on 429/5xx/network errors. Returns the per-location list."""
    last: Exception | None = None
    for attempt in range(cfg["retries"] + 1):
        if attempt:
            delay = cfg["backoff_seconds"] * 3 ** (attempt - 1)
            time.sleep(delay + random.uniform(0, 1))
        try:
            response = get(url, params=params, timeout=cfg["timeout_seconds"])
        except requests.RequestException as error:
            last = error
            continue
        if response.status_code == 429 or response.status_code >= 500:
            last = DataError(f"HTTP {response.status_code}")
            continue
        if response.status_code != 200:
            # A 400 is our request being wrong; retrying will not fix it.
            raise DataError(f"Open-Meteo HTTP {response.status_code}: "
                            f"{response.text[:200]}")
        payload = response.json()
        # One location returns an object, several return a list.
        return payload if isinstance(payload, list) else [payload]
    raise DataError(f"Open-Meteo failed after {cfg['retries'] + 1} attempts: {last}")


def _sum(values: list, n: int) -> float | None:
    window = [v for v in values[:n] if v is not None]
    # Half the window missing is not a total, it is a guess.
    if len(window) < n / 2:
        return None
    return round(float(sum(window)), 2)


def parse(item: dict, district: District) -> Reading:
    current = item.get("current") or {}
    hourly = (item.get("hourly") or {}).get("precipitation") or []
    stamp = current.get("time")
    if stamp is None:
        raise DataError(f"no current block for {district.district}")
    # timezone=GMT in the request, so the stamp is UTC.
    valid = dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.timezone.utc)
    temp = current.get("temperature_2m")
    return Reading(
        district=district,
        valid_utc=valid,
        temp_c=None if temp is None else float(temp),
        precip_24h_mm=_sum(hourly, 24),
        precip_7d_mm=_sum(hourly, 168),
    )


def fetch(districts: list[District], cfg: dict,
          get: Callable = requests.get, sleep: Callable = time.sleep) -> list[Reading]:
    readings: list[Reading] = []
    groups = list(batches(districts, cfg["batch_size"]))
    for index, group in enumerate(groups):
        if index:
            sleep(cfg["pause_seconds"])
        params = {
            "latitude": ",".join(f"{d.lat:.4f}" for d in group),
            "longitude": ",".join(f"{d.lon:.4f}" for d in group),
            "current": "temperature_2m,precipitation",
            "hourly": "precipitation",
            "forecast_hours": cfg["forecast_hours"],
            "timezone": "GMT",
        }
        items = _get(cfg["endpoint"], params, cfg, get=get)
        if len(items) != len(group):
            raise DataError(f"batch {index + 1}: asked for {len(group)} locations, "
                            f"got {len(items)}")
        for item, district in zip(items, group):
            readings.append(parse(item, district))
        print(f"  batch {index + 1}/{len(groups)}: {len(group)} districts")
    return readings


# --------------------------------------------------------------------------
# Aggregation and storage
# --------------------------------------------------------------------------
def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 2) if present else None


def aggregate(readings: list[Reading]) -> list[dict]:
    """State means of the district values. Unweighted: the map colours a state by what
    a typical district in it sees, which is what the weather layer claims to show."""
    by_state: dict[str, list[Reading]] = {}
    for r in readings:
        by_state.setdefault(r.district.state_key, []).append(r)
    out = []
    for key, rows in sorted(by_state.items()):
        out.append({
            "state_key": key,
            "state": rows[0].district.state,
            "n_districts": len(rows),
            "temp_c": _mean([r.temp_c for r in rows]),
            "precip_24h_mm": _mean([r.precip_24h_mm for r in rows]),
            "precip_7d_mm": _mean([r.precip_7d_mm for r in rows]),
        })
    return out


def store(readings: list[Reading], n_expected: int, fetched: dt.datetime) -> int:
    """Replace both tables and log the run, in one transaction."""
    from src.db.models import WeatherFetch, WeatherNow, WeatherState
    from src.db.session import session_scope

    states = aggregate(readings)
    with session_scope() as session:
        session.query(WeatherNow).delete()
        session.query(WeatherState).delete()
        session.add_all(WeatherNow(
            state=r.district.state, district=r.district.district,
            state_key=r.district.state_key, lat=r.district.lat, lon=r.district.lon,
            valid_utc=r.valid_utc, fetched_utc=fetched, temp_c=r.temp_c,
            precip_24h_mm=r.precip_24h_mm, precip_7d_mm=r.precip_7d_mm,
        ) for r in readings)
        session.add_all(WeatherState(fetched_utc=fetched, **s) for s in states)
        session.add(WeatherFetch(started_utc=fetched, ok=True,
                                 n_districts=len(readings), n_expected=n_expected))
    return len(states)


def record_failure(error: Exception, n_expected: int, started: dt.datetime) -> None:
    from src.db.models import WeatherFetch
    from src.db.session import session_scope

    try:
        with session_scope() as session:
            session.add(WeatherFetch(started_utc=started, ok=False, n_districts=0,
                                     n_expected=n_expected,
                                     error=f"{type(error).__name__}: {error}"[:500]))
    except Exception as log_error:          # the original error is the one to raise
        print(f"  could not record the failure: {type(log_error).__name__}")


def run(cfg: dict, *, dry_run: bool = False, get: Callable = requests.get,
        sleep: Callable = time.sleep) -> dict:
    from src.db.session import create_all

    districts = load_districts(cfg)
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if not dry_run:
        create_all()
    try:
        readings = fetch(districts, cfg, get=get, sleep=sleep)
        coverage = len(readings) / len(districts)
        if coverage < cfg["min_coverage"]:
            raise DataError(f"only {len(readings)}/{len(districts)} districts "
                            f"({coverage:.0%}) - below min_coverage "
                            f"{cfg['min_coverage']:.0%}; previous data kept")
        n_states = 0 if dry_run else store(readings, len(districts), started)
    except Exception as error:
        if not dry_run:
            record_failure(error, len(districts), started)
        raise
    valid = sorted(r.valid_utc for r in readings)
    return {"readings": readings, "n_states": n_states, "n_expected": len(districts),
            "valid_range": (valid[0], valid[-1]), "fetched": started}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-url-env", default=None, metavar="NAME",
                        help="name of an environment variable holding the database "
                             "URL, e.g. DATABASE_URL_CLOUD (never pass a URL itself)")
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

    from src.db.session import describe

    cfg = load_config("weather")
    print("--- jobs.weather ---")
    print(f"  database: {'(dry run)' if args.dry_run else describe()}")
    result = run(cfg, dry_run=args.dry_run)
    readings = result["readings"]
    if not readings:
        raise DataError("no readings")
    wet = sorted(readings, key=lambda r: r.precip_24h_mm or 0, reverse=True)[:3]
    summarize(
        "jobs.weather",
        rows=len(readings),
        date_range=result["valid_range"],
        extra={
            "districts": f"{len(readings)} of {result['n_expected']}",
            "states": result["n_states"] if not args.dry_run else "(dry run)",
            "wettest next 24 h": ", ".join(
                f"{r.district.district} ({r.precip_24h_mm} mm)" for r in wet),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
