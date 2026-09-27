"""LightGBM-only inference from the committed assets in `app/assets/models/`.

This is the forecast path the hosted website runs. It is deliberately narrow: load a
booster, predict, apply the isotonic calibrator, done. No scikit-learn, no joblib, no
torch, and nothing read from `data/` or `models/` - those are gitignored and do not
exist on Streamlit Community Cloud.

Calibration is applied from the JSON breakpoints `scripts/export_models.py` writes.
`numpy.interp` over those breakpoints reproduces `IsotonicRegression.predict` exactly
for `out_of_bounds="clip"`, and the export script asserts that against the real
estimator before writing the file, so the hosted numbers match the laptop's.

Everything is cached at module level because a Streamlit rerun re-imports nothing but
re-executes the page: reloading 12 boosters per interaction would dominate the free
tier's ~2.7 GB budget and its CPU.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.common import ROOT
from src.report import DataError

ASSETS = ROOT / "app" / "assets" / "models"
MANIFEST = ASSETS / "manifest.json"
FEATURES = ASSETS / "features.parquet"

TARGETS = ("onset", "dry", "heavy")
HORIZONS = (7, 14, 21, 28)


def available() -> bool:
    """Whether the committed assets are present and complete."""
    if not MANIFEST.is_file() or not FEATURES.is_file():
        return False
    return len(list(ASSETS.glob("*.txt"))) == len(TARGETS) * len(HORIZONS)


def describe() -> str:
    if not MANIFEST.is_file():
        return f"missing {MANIFEST.relative_to(ROOT).as_posix()}"
    boosters = len(list(ASSETS.glob("*.txt")))
    calibrators = len(list(ASSETS.glob("*.calib.json")))
    info = manifest()
    return (f"{boosters}/12 boosters, {calibrators}/12 calibrators "
            f"({info.get('calibration', '?')}), {info.get('feature_rows', 0):,} "
            f"feature rows for {info.get('units', 0)} units")


@functools.lru_cache(maxsize=1)
def manifest() -> dict:
    if not MANIFEST.is_file():
        raise DataError(
            f"missing {MANIFEST}; run python scripts/export_models.py to write the "
            "committed model assets")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=16)
def _booster(stem: str):
    import lightgbm as lgb

    path = ASSETS / f"{stem}.txt"
    if not path.is_file():
        raise DataError(f"missing booster {path}; run scripts/export_models.py")
    return lgb.Booster(model_file=str(path))


@functools.lru_cache(maxsize=16)
def _calibrator(stem: str) -> dict | None:
    path = ASSETS / f"{stem}.calib.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "isotonic":
        raise DataError(f"{path.name}: unsupported calibrator "
                        f"{payload.get('kind')!r}")
    return payload


def calibrate(raw: np.ndarray, breakpoints: dict | None) -> np.ndarray:
    """Isotonic calibration from exported breakpoints, matching sklearn's 'clip'."""
    if breakpoints is None:
        return raw
    clipped = np.clip(raw, breakpoints["x_min"], breakpoints["x_max"])
    return np.interp(clipped, breakpoints["x"], breakpoints["y"])


@functools.lru_cache(maxsize=1)
def _feature_frame() -> pd.DataFrame:
    """The committed feature rows. Read once; ~31k x 33 is a few MB in memory."""
    if not FEATURES.is_file():
        raise DataError(
            f"missing {FEATURES}; run python scripts/export_models.py")
    frame = pd.read_parquet(FEATURES)
    frame["start_date"] = pd.to_datetime(frame["start_date"])
    for column in manifest().get("categorical_features", []):
        if column in frame.columns:
            frame[column] = frame[column].astype("category")
    return frame


def load_table() -> pd.DataFrame:
    """Same contract as `src.baseline_common.load_table`, from the committed asset."""
    return _feature_frame().copy()


def feature_columns(frame: pd.DataFrame | None = None) -> list[str]:
    """The exact column order the boosters were trained on."""
    columns = manifest()["feature_columns"]
    if frame is not None:
        missing = [c for c in columns if c not in frame.columns]
        if missing:
            raise DataError(
                f"the feature table is missing {len(missing)} trained column(s): "
                f"{', '.join(missing[:6])}. Re-run scripts/export_models.py.")
    return columns


def predict(frame: pd.DataFrame, features: list[str] | None = None) -> pd.DataFrame:
    """Calibrated probabilities per (unit, hazard, horizon) for one start date.

    Mirrors `src.pipeline.run.predict` so the pipeline can use either source.
    """
    columns = features or feature_columns(frame)
    matrix = frame[columns]

    rows = []
    for target in TARGETS:
        for horizon in HORIZONS:
            stem = f"{target}_{horizon}"
            raw = _booster(stem).predict(matrix)
            breakpoints = _calibrator(stem)
            probability = calibrate(np.asarray(raw, dtype=float), breakpoints)
            model = ("lightgbm+isotonic_cv" if breakpoints
                     else "lightgbm_uncalibrated")
            for unit_id, value in zip(frame["unit_id"], probability):
                rows.append({"unit_id": unit_id, "hazard": target,
                             "horizon": horizon, "probability": float(value),
                             "model": model})
    return pd.DataFrame(rows)


def clear_cache() -> None:
    """Drop the cached boosters, calibrators and feature table."""
    for cached in (manifest, _booster, _calibrator, _feature_frame):
        cached.cache_clear()


def main() -> int:
    """`python -m src.hosted_models` reports what is committed and smoke-tests it."""
    print("--- hosted_models ---")
    print(f"  assets: {ASSETS.relative_to(ROOT).as_posix()}")
    print(f"  state : {describe()}")
    if not available():
        raise DataError("the committed model assets are incomplete; "
                        "run python scripts/export_models.py")

    table = load_table()
    latest = table["start_date"].max()
    day = table[table["start_date"] == latest]
    result = predict(day)
    print(f"  smoke : {latest.date()} -> {len(result)} probabilities "
          f"for {day['unit_id'].nunique()} units")
    print(result.groupby("hazard")["probability"]
          .describe()[["mean", "min", "max"]].round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
