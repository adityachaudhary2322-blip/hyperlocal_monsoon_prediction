"""Export the trained LightGBM models into app/assets/models/ for the hosted site.

Run:  python scripts/export_models.py

Streamlit Community Cloud deploys straight from the git repo: there is no model registry
and no persistent disk, so whatever the site needs to forecast has to be committed. This
writes that set, small enough to live in git:

    <target>_<horizon>.txt          LightGBM booster, native text format
    <target>_<horizon>.calib.json   the isotonic_cv calibrator as plain numbers
    features.parquet                feature rows for the pilot units, no targets
    manifest.json                   feature order, categories, provenance

**The calibrators are converted, not copied.** `models/lgbm/*_isotonic_cv.joblib` are
pickled scikit-learn estimators, and unpickling one requires scikit-learn and joblib at
the exact versions that wrote it - two packages, tens of megabytes, and a silent
correctness risk if a pin ever drifts. An isotonic regression is just a monotone step
function, so the breakpoints are written as JSON and evaluated at runtime with
`numpy.interp`, which reproduces `IsotonicRegression.predict` exactly for
`out_of_bounds="clip"` (asserted below on real data, not assumed).

Calibration is not optional: Phase 3 measured that cross-fitted isotonic took mean test
BSS from -0.064 to -0.007 and fixed 11 of 12 target/horizon pairs (CLAUDE.md §13).
Shipping the raw boosters alone would deploy the worse model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.baseline_common import (CATEGORICAL_FEATURES, HORIZONS, ID_COLUMNS,  # noqa: E402
                                 LGBM_DIR, TARGETS, feature_columns, load_table)
from src.report import DataError, summarize  # noqa: E402

ASSETS = ROOT / "app" / "assets" / "models"
CALIBRATION = "isotonic_cv"        # the Phase 3 default; see CLAUDE.md §13

# Git is a poor home for large binaries and Streamlit Cloud clones the whole repo on
# every deploy, so the export is capped and the script fails rather than bloating it.
SIZE_LIMIT_MB = 20.0


def export_calibrator(path: Path, out: Path) -> dict:
    """Write an isotonic calibrator as JSON and prove numpy reproduces it."""
    import joblib

    model = joblib.load(path)
    if type(model).__name__ != "IsotonicRegression":
        raise DataError(f"{path.name} is a {type(model).__name__}, not isotonic; "
                        "only isotonic calibrators can be exported as breakpoints")
    if model.out_of_bounds != "clip":
        raise DataError(
            f"{path.name} was fitted with out_of_bounds={model.out_of_bounds!r}; "
            "the numpy.interp equivalence below only holds for 'clip'")

    payload = {
        "kind": "isotonic",
        "x": [float(v) for v in model.X_thresholds_],
        "y": [float(v) for v in model.y_thresholds_],
        "x_min": float(model.X_min_),
        "x_max": float(model.X_max_),
    }

    # The equivalence is checked here, once, against the estimator itself - so a
    # scikit-learn change can never quietly alter what the site computes.
    probe = np.linspace(0.0, 1.0, 101)
    expected = model.predict(probe)
    got = np.interp(np.clip(probe, payload["x_min"], payload["x_max"]),
                    payload["x"], payload["y"])
    largest = float(np.max(np.abs(expected - got)))
    if largest > 1e-12:
        raise DataError(
            f"{path.name}: numpy.interp differs from IsotonicRegression.predict by "
            f"{largest:.2e}; the JSON export would change the forecast")

    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {"breakpoints": len(payload["x"]), "max_error": largest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", default=CALIBRATION,
                        help=f"calibrator suffix to export (default {CALIBRATION})")
    args = parser.parse_args()

    print("--- export_models ---")
    if not LGBM_DIR.is_dir():
        raise DataError(f"missing {LGBM_DIR}; run python -m src.train_baselines first")

    ASSETS.mkdir(parents=True, exist_ok=True)

    # Read the feature table before anything else touches it, and take only the
    # columns an inference run needs: the ids and the features, never the targets.
    table = load_table()
    features = feature_columns(table)
    # `feature_columns` appends the categoricals, and ID_COLUMNS already holds
    # `state` and `zone_id`, so the two lists overlap - dedupe in order.
    keep: list[str] = []
    for column in list(ID_COLUMNS) + features:
        if column in table.columns and column not in keep:
            keep.append(column)
    slim = table[keep].copy()
    for column in CATEGORICAL_FEATURES:
        if column in slim.columns:
            slim[column] = slim[column].astype(str)

    table_path = ASSETS / "features.parquet"
    slim.to_parquet(table_path, index=False, compression="zstd")

    exported, calibrated = [], []
    for target in TARGETS:
        for horizon in HORIZONS:
            stem = f"{target}_{horizon}"
            booster = LGBM_DIR / f"{stem}.txt"
            if not booster.is_file():
                raise DataError(f"missing {booster}")
            shutil.copy2(booster, ASSETS / f"{stem}.txt")
            exported.append(stem)

            calibrator = LGBM_DIR / f"{stem}_{args.calibration}.joblib"
            if calibrator.is_file():
                info = export_calibrator(calibrator, ASSETS / f"{stem}.calib.json")
                calibrated.append(stem)
            else:
                print(f"  WARNING: no {calibrator.name}; {stem} will serve "
                      "uncalibrated probabilities")

    manifest = {
        "created_by": "scripts/export_models.py",
        "calibration": args.calibration,
        "targets": list(TARGETS),
        "horizons": list(HORIZONS),
        # Order matters: LightGBM predicts on positional columns.
        "feature_columns": features,
        "categorical_features": list(CATEGORICAL_FEATURES),
        "id_columns": [c for c in ID_COLUMNS if c in table.columns],
        "feature_rows": int(len(slim)),
        "start_dates": [str(slim["start_date"].min().date()),
                        str(slim["start_date"].max().date())],
        "units": int(slim["unit_id"].nunique()),
        "calibrated": calibrated,
        "note": ("Calibrators are isotonic breakpoints evaluated with numpy.interp; "
                 "see scripts/export_models.py for the equivalence check."),
    }
    (ASSETS / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    written = sorted(ASSETS.glob("*"))
    total_mb = sum(p.stat().st_size for p in written) / 1e6
    if total_mb > SIZE_LIMIT_MB:
        raise DataError(
            f"export is {total_mb:.1f} MB, over the {SIZE_LIMIT_MB:.0f} MB cap for a "
            "git-committed asset")

    summarize(
        "export_models",
        rows=len(slim),
        files=[ASSETS],
        date_range=tuple(manifest["start_dates"]),
        extra={
            "boosters": f"{len(exported)}/12",
            "calibrators": f"{len(calibrated)}/12 ({args.calibration})",
            "feature rows": f"{len(slim):,} x {len(features)} features",
            "units": manifest["units"],
            "total size": f"{total_mb:.2f} MB of {SIZE_LIMIT_MB:.0f} MB allowed",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
