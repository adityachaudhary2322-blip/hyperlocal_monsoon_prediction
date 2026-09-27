"""SQLite cache and call log for the language layer.

Two tables:

* `text_cache`  - keyed on (action, crop, language, probability bucket, kind, mode).
  Probabilities are bucketed (default 0.05) so 0.71 and 0.73 share one entry instead
  of causing two paid calls for text a farmer could not tell apart.
* `llm_calls`   - one row per outbound request: provider, latency, characters,
  tokens, estimated cost, and whether it succeeded. This is what the Models page
  totals by day.

The cache is keyed on advisory *content* only. Nothing subscriber-specific is
written here, for the same reason nothing subscriber-specific is sent to a provider.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src.common import ROOT, load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS text_cache (
    key           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    language      TEXT NOT NULL,
    action        TEXT,
    crop          TEXT,
    prob_bucket   REAL,
    mode          TEXT,
    text          TEXT NOT NULL,
    provider      TEXT NOT NULL,
    source        TEXT NOT NULL,
    created_utc   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc   TEXT NOT NULL,
    day           TEXT NOT NULL,
    provider      TEXT NOT NULL,
    operation     TEXT NOT NULL,
    language      TEXT,
    ok            INTEGER NOT NULL,
    cache_hit     INTEGER NOT NULL DEFAULT 0,
    latency_ms    REAL,
    input_chars   INTEGER,
    output_chars  INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_inr      REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON llm_calls(day);
"""


def cache_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config("llm")
    return ROOT / cfg["cache"]["path"]


@contextmanager
def connect(path: Path | None = None, cfg: dict | None = None):
    cfg = cfg or load_config("llm")
    path = path or cache_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        yield connection
        connection.commit()
    finally:
        connection.close()


def bucket(probability: float | None, cfg: dict | None = None) -> float | None:
    """Round a probability down to its cache bucket."""
    if probability is None:
        return None
    cfg = cfg or load_config("llm")
    size = cfg["cache"]["probability_bucket"]
    if size <= 0:
        return float(probability)
    return round(int(float(probability) / size) * size, 6)


def make_key(*, kind: str, language: str, action: str | None = None,
             crop: str | None = None, probability: float | None = None,
             mode: str | None = None, extra: str | None = None,
             cfg: dict | None = None) -> tuple[str, float | None]:
    cfg = cfg or load_config("llm")
    prob = bucket(probability, cfg)
    payload = json.dumps(
        {"kind": kind, "language": language, "action": action, "crop": crop,
         "prob": prob, "mode": mode, "extra": extra},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), prob


def get(key: str, *, path: Path | None = None, cfg: dict | None = None):
    cfg = cfg or load_config("llm")
    if not cfg["cache"].get("enabled", True):
        return None
    with connect(path, cfg) as connection:
        row = connection.execute(
            "SELECT text, provider, source, created_utc FROM text_cache WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    text, provider, source, created = row
    ttl = cfg["cache"].get("ttl_days")
    if ttl:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(created)
        if age.days > ttl:
            return None
    return {"text": text, "provider": provider, "source": source,
            "created_utc": created}


def put(key: str, *, kind: str, language: str, text: str, provider: str,
        source: str, action: str | None = None, crop: str | None = None,
        prob_bucket: float | None = None, mode: str | None = None,
        path: Path | None = None, cfg: dict | None = None) -> None:
    cfg = cfg or load_config("llm")
    if not cfg["cache"].get("enabled", True):
        return
    with connect(path, cfg) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO text_cache "
            "(key, kind, language, action, crop, prob_bucket, mode, text, provider, "
            " source, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, kind, language, action, crop, prob_bucket, mode, text, provider,
             source, dt.datetime.now(dt.timezone.utc).isoformat()),
        )


def log_call(*, provider: str, operation: str, ok: bool, language: str | None = None,
             cache_hit: bool = False, latency_ms: float | None = None,
             input_chars: int | None = None, output_chars: int | None = None,
             input_tokens: int | None = None, output_tokens: int | None = None,
             cost_inr: float | None = None, error: str | None = None,
             path: Path | None = None, cfg: dict | None = None) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with connect(path, cfg) as connection:
        connection.execute(
            "INSERT INTO llm_calls (created_utc, day, provider, operation, language, "
            "ok, cache_hit, latency_ms, input_chars, output_chars, input_tokens, "
            "output_tokens, cost_inr, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), now.date().isoformat(), provider, operation, language,
             int(ok), int(cache_hit), latency_ms, input_chars, output_chars,
             input_tokens, output_tokens, cost_inr, error),
        )


def daily_totals(days: int = 14, *, path: Path | None = None,
                 cfg: dict | None = None) -> list[dict]:
    """Per-day, per-provider totals for the Models page."""
    with connect(path, cfg) as connection:
        rows = connection.execute(
            "SELECT day, provider, COUNT(*) AS calls, "
            "       SUM(ok) AS ok, SUM(cache_hit) AS cache_hits, "
            "       SUM(COALESCE(input_chars,0)+COALESCE(output_chars,0)) AS chars, "
            "       SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS tokens, "
            "       ROUND(SUM(COALESCE(cost_inr,0)), 4) AS cost_inr, "
            "       ROUND(AVG(latency_ms), 1) AS avg_latency_ms "
            "FROM llm_calls GROUP BY day, provider "
            "ORDER BY day DESC, provider LIMIT ?",
            (days * 8,),
        ).fetchall()
        columns = [d[0] for d in connection.execute(
            "SELECT day, provider, 1 AS calls, 1 AS ok, 1 AS cache_hits, 1 AS chars, "
            "1 AS tokens, 1 AS cost_inr, 1 AS avg_latency_ms FROM llm_calls LIMIT 0"
        ).description]
    return [dict(zip(columns, row)) for row in rows]


def calls_today(*, path: Path | None = None, cfg: dict | None = None) -> int:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with connect(path, cfg) as connection:
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE day = ? AND cache_hit = 0",
            (today,),
        ).fetchone()
    return int(count)
