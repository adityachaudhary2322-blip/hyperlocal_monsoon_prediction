"""Chronos-2 forecasts at every validation/test start date, aggregated to weeks.

Run:  python -m src.chronos_features [--variant zeroshot|finetuned|both] [--force]

For each forecast start date `s` the model sees the 730 days ending at `s-1` and
predicts 28 days from `s`. Daily quantiles (0.1/0.5/0.9) are summed into weekly totals
for weeks 1-4, which become stacker features.

Two leakage rules are load-bearing here:

  * The context window ends at `s-1`, exactly as in Phase 2. Day `s` is never seen.
  * **One predict call per start date.** Chronos-2's `cross_learning` makes joint
    predictions across the series in a batch, so batching several start dates together
    would let the model condition on another item's context that runs past this item's
    start date. Batching only same-start-date items keeps cross-learning legitimate
    (it shares information across the 30 units, all cut at the same instant).

Fine-tuning uses data up to 2018 only, and falls back on CUDA OOM: halve the batch
size, then shorten the context to 365, then fine-tune on a random 30% of units.

Note on the weekly quantiles: summing daily q10s is NOT the 10th percentile of the
weekly total. These are features for a downstream classifier, not calibrated weekly
intervals, and nothing here presents them as such.

IMPORT ORDER MATTERS. On this machine `pd.read_parquet(..., filters=...)` segfaults
once torch/AutoGluon has been imported - pyarrow and torch ship clashing native
libraries. Every parquet and GeoPackage read therefore happens at the top of `main()`,
and AutoGluon is imported lazily inside the functions that need it. Do not hoist those
imports to module scope.
"""

from __future__ import annotations

import argparse
import shutil
import time
import warnings

import geopandas as gpd
import numpy as np
import pandas as pd

from src.common import (
    DATA_PROCESSED,
    INDICES_PARQUET,
    MODELS_DIR,
    UNITS_GPKG,
    UNIT_RAIN_PARQUET,
    pilot_districts,
    splits,
)
from src.onset import forecast_start_dates
from src.report import DataError, require_nonempty, summarize

CHRONOS_FEATURES = DATA_PROCESSED / "chronos_features.parquet"
CHRONOS_DIR = MODELS_DIR / "chronos"

MODEL_PATH = "autogluon/chronos-2"
PREDICTION_LENGTH = 28
CONTEXT_DAYS = 730
QUANTILES = [0.1, 0.5, 0.9]
WEEKS = (1, 2, 3, 4)

# past-only covariates: available up to s-1, never into the horizon
PAST_COVARIATES = ["rmm1", "rmm2", "amplitude"]
# known covariates: persisted forward from the last value at s-1
KNOWN_COVARIATES = ["oni", "dmi"]

FINE_TUNE_END_YEAR = 2018  # "using data up to 2018 only"


def pilot_unit_ids() -> list[str]:
    units = gpd.read_file(UNITS_GPKG, layer="units", ignore_geometry=True)
    mask = np.zeros(len(units), dtype=bool)
    for state, districts in pilot_districts().items():
        mask |= (units["state"] == state) & units["district"].isin(districts)
    ids = units.loc[mask, "unit_id"].tolist()
    if not ids:
        raise DataError("no pilot units found; check config/project.yaml")
    return ids


def load_panel(unit_ids: list[str]) -> pd.DataFrame:
    """Daily rain plus covariates, in AutoGluon's (item_id, timestamp) shape."""
    rain = pd.read_parquet(
        UNIT_RAIN_PARQUET, columns=["unit_id", "date", "rain_mm"],
        filters=[("unit_id", "in", unit_ids)],
    )
    indices = pd.read_parquet(INDICES_PARQUET)
    keep = ["date"] + PAST_COVARIATES + KNOWN_COVARIATES
    panel = rain.merge(indices[keep], on="date", how="left")
    panel = panel.rename(columns={"unit_id": "item_id", "date": "timestamp"})

    # Chronos cannot consume NaN covariates; the indices start in 1990 and the MJO
    # series starts in 1974, so gaps here are only at the very beginning.
    for column in PAST_COVARIATES + KNOWN_COVARIATES:
        panel[column] = panel[column].astype("float32")
        panel[column] = panel.groupby("item_id", observed=True)[column].ffill()
        panel[column] = panel[column].fillna(0.0)

    return panel.sort_values(["item_id", "timestamp"]).reset_index(drop=True)


def start_dates(panel: pd.DataFrame) -> list[pd.Timestamp]:
    """Every forecast start date in the validation and test years."""
    cfg = splits()
    val_lo, val_hi = cfg["validate"]
    test_lo, _ = cfg["test"]
    last = panel["timestamp"].max()
    years = list(range(val_lo, val_hi + 1)) + list(range(test_lo, last.year + 1))

    out = []
    for year in years:
        for start in forecast_start_dates(year):
            # Need a full context before, and at least one observed day after.
            if start - pd.Timedelta(days=CONTEXT_DAYS) < panel["timestamp"].min():
                continue
            if start > last:
                continue
            out.append(start)
    return out


def _build_predictor(path, fine_tune: bool, context_length: int,
                     batch_size: int, fine_tune_batch_size: int):
    from autogluon.timeseries import TimeSeriesPredictor

    hyperparameters = {
        "Chronos2": {
            "model_path": MODEL_PATH,
            "context_length": context_length,
            "batch_size": batch_size,
        }
    }
    if fine_tune:
        hyperparameters["Chronos2"].update({
            "fine_tune": True,
            "fine_tune_mode": "lora",
            "fine_tune_batch_size": fine_tune_batch_size,
            "fine_tune_context_length": min(context_length, 2048),
        })
    predictor = TimeSeriesPredictor(
        target="rain_mm",
        prediction_length=PREDICTION_LENGTH,
        freq="D",
        known_covariates_names=KNOWN_COVARIATES,
        quantile_levels=QUANTILES,
        path=str(path),
        verbosity=0,
    )
    return predictor, hyperparameters


def fit_predictor(panel: pd.DataFrame, variant: str, unit_ids: list[str]):
    """Fit (or load) the predictor for a variant, degrading gracefully on OOM."""
    from autogluon.timeseries import TimeSeriesDataFrame

    path = CHRONOS_DIR / variant
    fine_tune = variant == "finetuned"

    if fine_tune:
        # "using data up to 2018 only"
        fit_data = panel[panel["timestamp"] <= pd.Timestamp(f"{FINE_TUNE_END_YEAR}-12-31")]
    else:
        # Zero-shot still needs a frame to fit(); nothing is learned from it.
        fit_data = panel[panel["timestamp"] <= pd.Timestamp(f"{FINE_TUNE_END_YEAR}-12-31")]

    # OOM ladder, in the order the spec asks for.
    attempts = [
        dict(context_length=CONTEXT_DAYS, batch_size=32, fine_tune_batch_size=32,
             unit_fraction=1.0, why="full settings"),
        dict(context_length=CONTEXT_DAYS, batch_size=16, fine_tune_batch_size=16,
             unit_fraction=1.0, why="halved batch size"),
        dict(context_length=365, batch_size=16, fine_tune_batch_size=8,
             unit_fraction=1.0, why="context shortened to 365"),
        dict(context_length=365, batch_size=8, fine_tune_batch_size=4,
             unit_fraction=0.3, why="30% of units"),
    ]

    last_error: Exception | None = None
    for attempt in attempts:
        if path.exists():
            shutil.rmtree(path)
        subset = fit_data
        if attempt["unit_fraction"] < 1.0:
            rng = np.random.default_rng(42)
            chosen = rng.choice(unit_ids,
                                size=max(1, int(len(unit_ids) * attempt["unit_fraction"])),
                                replace=False)
            subset = fit_data[fit_data["item_id"].isin(chosen)]

        predictor, hyperparameters = _build_predictor(
            path, fine_tune, attempt["context_length"],
            attempt["batch_size"], attempt["fine_tune_batch_size"],
        )
        try:
            print(f"    fitting ({attempt['why']}, context={attempt['context_length']}, "
                  f"batch={attempt['batch_size']})...", flush=True)
            ts = TimeSeriesDataFrame.from_data_frame(
                subset, id_column="item_id", timestamp_column="timestamp"
            )
            predictor.fit(ts, hyperparameters=hyperparameters,
                          skip_model_selection=True, enable_ensemble=False)
            return predictor, attempt
        except Exception as exc:  # noqa: BLE001 - we re-raise below if all fail
            message = str(exc).lower()
            if "out of memory" not in message and "cuda" not in message:
                raise
            last_error = exc
            print(f"    CUDA OOM: {attempt['why']} failed, degrading", flush=True)
            import torch
            torch.cuda.empty_cache()

    raise DataError(f"Chronos-2 {variant} ran out of memory at every setting: "
                    f"{last_error}")


def forecast_all(predictor, panel: pd.DataFrame, dates: list[pd.Timestamp],
                 variant: str) -> pd.DataFrame:
    """One predict call per start date; weekly-aggregated quantiles come back."""
    from autogluon.timeseries import TimeSeriesDataFrame

    quantile_columns = [str(q) for q in QUANTILES]
    rows: list[pd.DataFrame] = []
    started = time.time()

    for n, start in enumerate(dates, 1):
        lo = start - pd.Timedelta(days=CONTEXT_DAYS)
        history = panel[(panel["timestamp"] >= lo) & (panel["timestamp"] < start)]
        horizon = panel[(panel["timestamp"] >= start) &
                        (panel["timestamp"] < start + pd.Timedelta(days=PREDICTION_LENGTH))]
        if horizon.empty:
            continue

        ts = TimeSeriesDataFrame.from_data_frame(
            history, id_column="item_id", timestamp_column="timestamp"
        )

        # Known covariates over the horizon, persisted from the last observed value
        # at s-1 - the spec's rule, and the only honest one: ONI and DMI for the
        # future are not knowable at s.
        #
        # The index has to be exactly the next `prediction_length` steps for every
        # item, so it comes from AutoGluon rather than from observed rows: near the
        # end of the record the horizon runs past the data and slicing the panel
        # would yield a short frame that predict() rejects.
        last = (history.sort_values("timestamp")
                .groupby("item_id", observed=True)[KNOWN_COVARIATES].last())
        future = pd.DataFrame(predictor.make_future_data_frame(ts))
        for column in KNOWN_COVARIATES:
            future[column] = future["item_id"].map(last[column]).astype("float32")
        fk = TimeSeriesDataFrame.from_data_frame(
            future, id_column="item_id", timestamp_column="timestamp"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            prediction = predictor.predict(ts, known_covariates=fk)

        # Drop back to a plain DataFrame: TimeSeriesDataFrame re-validates on every
        # constructed result, so an intermediate comparison (as inside .clip) raises
        # "data must have a item_id column".
        frame = pd.DataFrame(prediction.reset_index())
        # Chronos is unconstrained and returns small negative values for the low
        # quantile of a near-zero series. Rainfall cannot be negative.
        frame[quantile_columns] = frame[quantile_columns].clip(lower=0.0)
        frame["week"] = ((frame["timestamp"] - start).dt.days // 7) + 1
        frame = frame[frame["week"].between(1, 4)]
        weekly = frame.groupby(["item_id", "week"], observed=True)[
            quantile_columns
        ].sum().reset_index()
        weekly["start_date"] = start
        rows.append(weekly)

        if n % 20 == 0 or n == len(dates):
            rate = (time.time() - started) / n
            print(f"    {n}/{len(dates)} start dates "
                  f"({rate:.2f}s each, ~{rate * (len(dates) - n) / 60:.1f} min left)",
                  flush=True)

    if not rows:
        raise DataError(f"{variant}: no forecasts were produced")

    long = pd.concat(rows, ignore_index=True)
    wide = long.pivot_table(index=["item_id", "start_date"], columns="week",
                            values=quantile_columns)
    wide.columns = [f"w{int(week)}_q{int(float(q) * 100):02d}"
                    for q, week in wide.columns]
    wide = wide.reset_index().rename(columns={"item_id": "unit_id"})
    wide["variant"] = variant
    return wide


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["zeroshot", "finetuned", "both"],
                        default="both")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N start dates (for smoke tests)")
    args = parser.parse_args()

    if CHRONOS_FEATURES.is_file() and not args.force:
        print(f"{CHRONOS_FEATURES} exists; pass --force to rebuild")
        return 0

    print("--- chronos_features ---")
    # All file reads happen here, BEFORE AutoGluon/torch is imported anywhere.
    # See the module docstring: the reverse order segfaults.
    import sys
    if "torch" in sys.modules:
        raise DataError(
            "torch is already imported; parquet reads must happen first "
            "(see the import-order note in this module's docstring)"
        )
    unit_ids = pilot_unit_ids()
    panel = load_panel(unit_ids)
    dates = start_dates(panel)
    if args.limit:
        dates = dates[: args.limit]
    print(f"  {len(unit_ids)} units, {len(panel):,} rows, "
          f"{panel['timestamp'].min():%Y-%m-%d} -> {panel['timestamp'].max():%Y-%m-%d}")
    print(f"  {len(dates)} start dates "
          f"({dates[0]:%Y-%m-%d} -> {dates[-1]:%Y-%m-%d})")

    variants = ["zeroshot", "finetuned"] if args.variant == "both" else [args.variant]
    frames = []
    settings = {}
    for variant in variants:
        print(f"\n  [{variant}]")
        predictor, attempt = fit_predictor(panel, variant, unit_ids)
        settings[variant] = attempt
        frames.append(forecast_all(predictor, panel, dates, variant))
        del predictor
        import torch
        torch.cuda.empty_cache()

    features = pd.concat(frames, ignore_index=True)
    require_nonempty(features, "chronos features")
    value_columns = [c for c in features.columns
                     if c not in ("unit_id", "start_date", "variant")]
    for column in value_columns:
        features[column] = features[column].astype("float32")

    CHRONOS_FEATURES.parent.mkdir(parents=True, exist_ok=True)
    features.to_parquet(CHRONOS_FEATURES, index=False)

    summarize(
        "chronos_features",
        rows=len(features),
        files=[CHRONOS_FEATURES, CHRONOS_DIR],
        date_range=(f"{features['start_date'].min():%Y-%m-%d}",
                    f"{features['start_date'].max():%Y-%m-%d}"),
        extra={
            "variants": ", ".join(variants),
            "units": features["unit_id"].nunique(),
            "start dates": features["start_date"].nunique(),
            "feature columns": len(value_columns),
            **{f"{v} settings": s["why"] for v, s in settings.items()},
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
