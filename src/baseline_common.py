"""Shared pieces for the Phase 3 baselines: splits, feature lists, climatology.

Kept separate so training and evaluation agree on exactly what a feature is, what a
target is, and how the climatology baseline is defined.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.common import DATA_PROCESSED, MODELS_DIR, splits

TRAIN_TABLE = DATA_PROCESSED / "train_table.parquet"
PREDICTIONS = DATA_PROCESSED / "predictions.parquet"
LGBM_DIR = MODELS_DIR / "lgbm"

TARGETS = ("onset", "dry", "heavy")
HORIZONS = (7, 14, 21, 28)

# Identifiers and targets - everything else in the table is a feature.
ID_COLUMNS = ["unit_id", "unit_name", "district", "state", "zone_id",
              "start_date", "start_md", "year"]
TARGET_PREFIXES = ("onset_", "dry_", "heavy_", "total_", "anomaly_")

# `state` and `zone_id` are identifiers AND features (the spec asks for both), so
# they are added back explicitly as categoricals.
CATEGORICAL_FEATURES = ["state", "zone_id"]

# Climatology shrinkage: how many "virtual years" of the pooled seasonal rate to mix
# into each unit's own rate. With ~29 training years per cell, 10 pulls a noisy cell
# most of the way back without erasing genuine unit-to-unit differences.
CLIM_ALPHA = 10.0

#: A metric computed on fewer positives than this is reported but flagged: with a
#: handful of events, Brier and AUC are dominated by which years happened to land in
#: the test split.
MIN_POSITIVES = 10

FEATURE_GROUPS = {
    "ENSO": ["oni", "oni_age_days", "nino34", "nino34_age_days"],
    "IOD": ["dmi", "dmi_age_days"],
    "MJO": ["rmm1", "rmm2", "amplitude", "mjo_age_days"] +
           [f"mjo_phase_{i}" for i in range(1, 9)],
    "local rainfall": ["rain_prev_7", "rain_prev_14", "rain_prev_30",
                       "rain_prev_7_anom", "rain_prev_14_anom", "rain_prev_30_anom",
                       "season_to_date", "days_since_wet", "onset_happened"],
    "seasonality": ["doy_sin", "doy_cos"],
    "unit identity": ["centroid_lat", "centroid_lon", "small_unit",
                      "state", "zone_id"],
}


def target_column(target: str, horizon: int) -> str:
    return f"{target}_{horizon}"


def feature_columns(frame: pd.DataFrame) -> list[str]:
    """Every column that is neither an identifier nor a target, plus the two
    categorical unit attributes."""
    features = [
        c for c in frame.columns
        if c not in ID_COLUMNS and not c.startswith(TARGET_PREFIXES)
    ]
    return features + CATEGORICAL_FEATURES


def load_table() -> pd.DataFrame:
    if not TRAIN_TABLE.is_file():
        from src.report import DataError
        raise DataError(
            f"missing {TRAIN_TABLE}; run python -m src.build_features first"
        )
    frame = pd.read_parquet(TRAIN_TABLE)
    for column in CATEGORICAL_FEATURES:
        frame[column] = frame[column].astype("category")
    return frame


def split_masks(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """train / validate / test row masks straight from config (no leakage)."""
    cfg = splits()
    train_lo, train_hi = cfg["train"]
    val_lo, val_hi = cfg["validate"]
    test_lo, _ = cfg["test"]
    return {
        "train": frame["year"].between(train_lo, train_hi),
        "validate": frame["year"].between(val_lo, val_hi),
        "test": frame["year"] >= test_lo,
    }


def climatology(frame: pd.DataFrame, column: str,
                train_mask: pd.Series) -> pd.Series:
    """P(target = 1 | unit, 5-day window), from TRAINING YEARS ONLY.

    Each forecast start date is its own 5-day window (they are 5 days apart), so the
    climatology cell is (unit_id, start_md). With ~29 training years per cell that is
    noisy - a cell that saw 2 events in 29 years would otherwise read 6.9% exactly -
    so each cell is shrunk toward the same window pooled across all units:

        p = (sum + alpha * p_pooled) / (n + alpha)

    Rows where the target is NaN contribute nothing (onset goes NaN once it has
    happened), and a cell with no training observations falls back to the pooled rate.
    """
    train = frame.loc[train_mask, ["unit_id", "start_md", column]].dropna(
        subset=[column]
    )

    # Pooled over units, per window: the seasonal shape.
    pooled = train.groupby("start_md", observed=True)[column].mean()
    overall = float(train[column].mean())

    cell = train.groupby(["unit_id", "start_md"], observed=True)[column].agg(
        ["sum", "count"]
    )
    prior = cell.index.get_level_values("start_md").map(pooled).to_numpy()
    prior = np.where(np.isfinite(prior), prior, overall)
    smoothed = (cell["sum"].to_numpy() + CLIM_ALPHA * prior) / (
        cell["count"].to_numpy() + CLIM_ALPHA
    )
    lookup = pd.Series(smoothed, index=cell.index, name="p_clim")

    keys = pd.MultiIndex.from_frame(frame[["unit_id", "start_md"]])
    values = lookup.reindex(keys).to_numpy()

    # Unseen (unit, window) cells fall back to the pooled seasonal rate, then to the
    # overall rate - never to NaN, or the baseline would be undefined where it is
    # most needed.
    fallback = frame["start_md"].map(pooled).to_numpy()
    values = np.where(np.isfinite(values), values, fallback)
    values = np.where(np.isfinite(values), values, overall)
    return pd.Series(values, index=frame.index, name="p_clim")
