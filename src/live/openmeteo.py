"""Batched Open-Meteo requests with their call weight counted.

Weight per location (Open-Meteo's rule; the ensemble docs' example is 40 members / 10):
    max(1, variables x members / 10) x max(1, days / 14)
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable

import numpy as np
import requests

from src.jobs.weather import _get
from src.report import DataError


def call_weight(n_series: int, n_days: int) -> float:
    return max(1.0, n_series / 10) * max(1.0, n_days / 14)


class Client:
    """Open-Meteo client that stays inside the free tier's rate limits by itself.

    The free API allows 600 calls/minute and 5,000/hour as well as 10,000/day. One live
    run weighs ~5,700, so without pacing it would break the HOURLY limit. Before each
    request the client waits until the request's weight fits in a rolling minute and a
    rolling hour (config/live.yaml `rate`), a little under the published limits.
    """

    def __init__(self, retry: dict, cache_dir: Path | None = None,
                 get: Callable = requests.get, sleep: Callable = time.sleep,
                 rate: dict | None = None, clock: Callable = time.monotonic):
        self.cfg = {"retries": retry["attempts"] - 1, "backoff_seconds": retry["backoff_seconds"],
                    "timeout_seconds": retry["timeout_seconds"]}
        self.cache_dir, self.get, self.sleep, self.clock = cache_dir, get, sleep, clock
        self.rate = rate or {"per_minute": 550, "per_hour": 4500}
        self.spent: list[tuple[float, float]] = []      # (time, weight) of past requests
        self.calls = 0.0
        self.requests = 0

    def _wait_for_budget(self, weight: float) -> None:
        for window, limit in ((60.0, self.rate["per_minute"]), (3600.0, self.rate["per_hour"])):
            if weight > limit:
                raise DataError(f"one request weighs {weight:.0f} calls, above the "
                                f"{limit}/{int(window)} s budget; lower the batch size")
            while True:
                now = self.clock()
                recent = [(t, w) for t, w in self.spent if now - t < window]
                used = sum(w for _, w in recent)
                if used + weight <= limit:
                    break
                oldest = min(t for t, _ in recent)
                self.sleep(max(1.0, window - (now - oldest) + 1.0))

    def fetch(self, endpoint: str, lat: np.ndarray, lon: np.ndarray, params: dict,
              batch: int, pause: float, weight_each: float, label: str) -> list[dict]:
        out: list[dict] = []
        n_batches = (len(lat) + batch - 1) // batch
        for b in range(n_batches):
            sl = slice(b * batch, (b + 1) * batch)
            query = {**params,
                     "latitude": ",".join(f"{v:.3f}" for v in lat[sl]),
                     "longitude": ",".join(f"{v:.3f}" for v in lon[sl])}
            items = self._cached(endpoint, query)
            if items is None:
                weight = weight_each * len(lat[sl])
                if self.requests:
                    self.sleep(pause)
                self._wait_for_budget(weight)
                items = _get(endpoint, query, self.cfg, get=self.get)
                self.spent.append((self.clock(), weight))
                self.requests += 1
                self.calls += weight
                self._store(endpoint, query, items)
            if len(items) != len(lat[sl]):
                raise DataError(f"{label}: batch {b + 1} asked {len(lat[sl])}, got {len(items)}")
            for item in items:
                if item.get("error"):
                    raise DataError(f"{label}: {item.get('reason')}")
            out.extend(items)
            print(f"    {label}: batch {b + 1}/{n_batches}")
        return out

    # A local cache (laptop only) so development reruns do not spend the daily budget.
    def _key(self, endpoint: str, query: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha1((endpoint + json.dumps(query, sort_keys=True)).encode()).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _cached(self, endpoint, query):
        path = self._key(endpoint, query)
        return json.loads(path.read_text()) if path and path.is_file() else None

    def _store(self, endpoint, query, items):
        path = self._key(endpoint, query)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(items))


def _matrix(items: list[dict], key: str) -> np.ndarray:
    """(days, points) float32 from one daily variable, None -> NaN."""
    return np.array([[np.nan if x is None else x for x in it["daily"][key]] for it in items],
                    dtype=np.float32).T


def ec46_members(client: Client, lat, lon, cfg: dict) -> tuple[np.ndarray, list[str]]:
    """Daily precipitation (members, days, points) and the date list."""
    params = {"models": cfg["model_members"], "daily": "precipitation_sum",
              "forecast_days": cfg["days"], "timezone": "GMT"}
    items = client.fetch(cfg["endpoint"], lat, lon, params, cfg["batch_size"],
                         cfg["pause_seconds"], call_weight(cfg["members"], cfg["days"]),
                         "EC46 members")
    dates = items[0]["daily"]["time"]
    keys = ["precipitation_sum"] + [f"precipitation_sum_member{m:02d}"
                                    for m in range(1, cfg["members"])]
    out = np.full((len(keys), len(dates), len(items)), np.nan, dtype=np.float32)
    for m, key in enumerate(keys):
        if all(key in it["daily"] for it in items):
            out[m] = _matrix(items, key)
    got = int(np.isfinite(out).any(axis=(1, 2)).sum())
    if got < len(keys) * 0.8:
        raise DataError(f"EC46: only {got} of {len(keys)} members returned")
    return out, dates


def ec46_mean(client: Client, lat, lon, cfg: dict) -> tuple[dict, list[str]]:
    params = {"models": cfg["model_mean"], "daily": ",".join(cfg["mean_variables"]),
              "forecast_days": cfg["days"], "timezone": "GMT"}
    items = client.fetch(cfg["endpoint"], lat, lon, params, cfg["batch_size"] * 5,
                         cfg["pause_seconds"] / 3,
                         call_weight(len(cfg["mean_variables"]), cfg["days"]), "EC46 mean")
    return {v: _matrix(items, v) for v in cfg["mean_variables"]}, items[0]["daily"]["time"]


def short_range(client: Client, lat, lon, cfg: dict) -> tuple[dict, list[str], dict]:
    params = {"daily": ",".join(cfg["daily"]), "current": ",".join(cfg["current"]),
              "past_days": cfg["past_days"], "forecast_days": cfg["forecast_days"],
              "timezone": "Asia/Kolkata"}
    days = cfg["past_days"] + cfg["forecast_days"]
    items = client.fetch(cfg["endpoint"], lat, lon, params, cfg["batch_size"],
                         cfg["pause_seconds"], call_weight(len(cfg["daily"]), days),
                         "0.5 deg forecast")
    daily = {v: _matrix(items, v) for v in cfg["daily"]}
    current = {v: np.array([np.nan if it["current"].get(v) is None else it["current"][v]
                            for it in items], dtype=np.float32) for v in cfg["current"]}
    current["time"] = items[0]["current"]["time"]
    return daily, items[0]["daily"]["time"], current
