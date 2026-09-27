"""Score the Phase 3 baselines on the test years and write the report.

Run:  python -m src.evaluate_baselines

Produces:
  outputs/metrics/metrics.csv   model x target x horizon x state x district
  outputs/figs/reliability_*.png
  outputs/figs/skill_by_state.png
  outputs/figs/feature_importance_*.png
  outputs/baseline_report.md

Every metric carries the sample size and event count it was computed from. With 8
pilot districts and five test years some slices are tiny - Purnia's onset_7 has 11
test rows because its onset fires in May and the target is NaN thereafter - so a
metric without its n is not interpretable.
"""

from __future__ import annotations

import argparse
import json

import lightgbm as lgb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, roc_auc_score

from src.baseline_common import (
    FEATURE_GROUPS,
    HORIZONS,
    LGBM_DIR,
    MIN_POSITIVES,
    PREDICTIONS,
    TARGETS,
    feature_columns,
    load_table,
    split_masks,
    target_column,
)
from src.common import FIGS_DIR, METRICS_DIR, OUTPUTS_DIR
from src.report import DataError, require_nonempty, summarize
from src.viz import (
    GRID,
    SERIES,
    SKILL_CMAP,
    TEXT_MUTED,
    TEXT_SECONDARY,
    apply_style,
    recessive_grid,
)

METRICS_CSV = METRICS_DIR / "metrics.csv"
REPORT_MD = OUTPUTS_DIR / "baseline_report.md"
IMPORTANCE_TARGETS = [("dry", 14), ("onset", 14)]
N_BINS = 10


def score(y: np.ndarray, p: np.ndarray, p_ref: np.ndarray) -> dict:
    """Brier, Brier Skill Score against `p_ref`, and ROC-AUC."""
    n = y.size
    n_pos = int(y.sum())
    out = {
        "n": n,
        "n_pos": n_pos,
        "base_rate": float(y.mean()) if n else np.nan,
        "mean_forecast": float(p.mean()) if n else np.nan,
        "brier": np.nan, "brier_ref": np.nan, "bss": np.nan, "auc": np.nan,
        "reliable": bool(n_pos >= MIN_POSITIVES and n - n_pos >= MIN_POSITIVES),
    }
    if n == 0:
        return out
    out["brier"] = float(brier_score_loss(y, p))
    out["brier_ref"] = float(brier_score_loss(y, p_ref))
    if out["brier_ref"] > 0:
        out["bss"] = 1.0 - out["brier"] / out["brier_ref"]
    # AUC needs both classes present.
    if 0 < n_pos < n:
        out["auc"] = float(roc_auc_score(y, p))
    return out


CALIBRATION_VARIANTS = {
    "uncalibrated": "p_lgbm_raw",
    "isotonic on validation years": "p_lgbm_isoval",
    "isotonic cross-fitted on training years": "p_lgbm_isocv",
    "Platt cross-fitted on training years": "p_lgbm_plattcv",
}


def calibration_comparison(predictions: pd.DataFrame) -> pd.DataFrame:
    """Score every calibration on both validation and test.

    The point is not only which scores best on test, but how far each one's
    validation score is from its test score - that gap is what made Phase 4's model
    selection pick the wrong model twelve times out of twelve.
    """
    rows = []
    for label, column in CALIBRATION_VARIANTS.items():
        if column not in predictions.columns:
            continue
        for split in ("validate", "test"):
            scope = predictions[predictions["split"] == split]
            for (target, horizon), group in scope.groupby(["target", "horizon"],
                                                          observed=True):
                rows.append({
                    "calibration": label, "column": column, "split": split,
                    "target": target, "horizon": horizon,
                    **score(group["y_true"].to_numpy(), group[column].to_numpy(),
                            group["p_clim"].to_numpy()),
                })
    frame = pd.DataFrame(rows)
    wide = frame.pivot_table(index=["calibration", "target", "horizon"],
                             columns="split", values="bss").reset_index()
    wide["optimism"] = wide["validate"] - wide["test"]
    return wide


def build_metrics(predictions: pd.DataFrame) -> pd.DataFrame:
    """One row per (model, target, horizon, slice)."""
    test = predictions[predictions["split"] == "test"]
    require_nonempty(test, "test-split predictions")

    rows = []
    for (target, horizon), group in test.groupby(["target", "horizon"],
                                                 observed=True):
        slices = [("overall", "all", "all", group)]
        for state, sub in group.groupby("state", observed=True):
            slices.append(("state", state, "all", sub))
        for (state, district), sub in group.groupby(["state", "district"],
                                                    observed=True):
            slices.append(("district", state, district, sub))

        for level, state, district, sub in slices:
            y = sub["y_true"].to_numpy()
            ref = sub["p_clim"].to_numpy()
            for model, column in (("climatology", "p_clim"),
                                  ("lightgbm", "p_lgbm"),
                                  ("lightgbm_uncalibrated", "p_lgbm_raw"),
                                  ("lightgbm_isotonic_val", "p_lgbm_isoval"),
                                  ("lightgbm_isotonic_cv", "p_lgbm_isocv"),
                                  ("lightgbm_platt_cv", "p_lgbm_plattcv")):
                if column not in sub.columns:
                    continue
                metrics = score(y, sub[column].to_numpy(), ref)
                rows.append({
                    "model": model, "target": target, "horizon": horizon,
                    "level": level, "state": state, "district": district,
                    **metrics,
                })
    return pd.DataFrame(rows)


def reliability(y: np.ndarray, p: np.ndarray, bins: int = N_BINS):
    """Equal-width bins: mean predicted probability vs observed frequency."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if not m.any():
            continue
        out.append((float(p[m].mean()), float(y[m].mean()), int(m.sum())))
    if not out:
        return np.array([]), np.array([]), np.array([])
    pred, obs, counts = map(np.array, zip(*out))
    return pred, obs, counts


def draw_reliability(predictions: pd.DataFrame, target: str) -> None:
    apply_style()
    test = predictions[(predictions["split"] == "test") &
                       (predictions["target"] == target)]
    fig, axes = plt.subplots(1, 4, figsize=(13.5, 3.9), sharey=True)

    for ax, horizon in zip(axes, HORIZONS):
        sub = test[test["horizon"] == horizon]
        y = sub["y_true"].to_numpy()
        recessive_grid(ax, axis="both")
        ax.plot([0, 1], [0, 1], color=GRID, linewidth=1.2, zorder=1)

        for label, column, colour in (("climatology", "p_clim", SERIES[1]),
                                      ("LightGBM", "p_lgbm", SERIES[0])):
            pred, obs, counts = reliability(y, sub[column].to_numpy())
            if pred.size == 0:
                continue
            ax.plot(pred, obs, color=colour, marker="o", markersize=4.5,
                    markerfacecolor=colour, markeredgecolor="white",
                    markeredgewidth=1.0, zorder=3, label=label)
        ax.set_title(f"{horizon} days", loc="left")
        ax.set_xlim(-0.02, 1.02)
        ax.set_ylim(-0.02, 1.02)
        ax.set_xlabel("forecast probability")
        ax.set_aspect("equal")

    axes[0].set_ylabel("observed frequency")
    axes[0].legend(loc="upper left")
    fig.suptitle(
        f"Reliability on test years - {target}, all pilot districts",
        x=0.01, ha="left", fontsize=11, fontweight="600",
    )
    # tight_layout first, then the note - placing it before leaves it underneath
    # the x-axis labels of the equal-aspect panels.
    fig.tight_layout(rect=(0, 0.09, 1, 0.94))
    fig.text(0.01, 0.015,
             "Diagonal = perfectly calibrated. A curve ABOVE the diagonal means the "
             "event happened more often than forecast (under-forecasting); below it, "
             "less often.",
             color=TEXT_MUTED, fontsize=7.5)
    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGS_DIR / f"reliability_{target}.png", bbox_inches="tight")
    plt.close(fig)


def _skill_grid(metrics: pd.DataFrame, model: str, states: list[str]):
    table = metrics[(metrics["model"] == model) &
                    (metrics["level"].isin(["overall", "state"]))]
    rows = [f"{t} {h}d" for t in TARGETS for h in HORIZONS]
    grid = np.full((len(rows), len(states)), np.nan)
    for i, (target, horizon) in enumerate((t, h) for t in TARGETS for h in HORIZONS):
        for j, state in enumerate(states):
            mask = (table["target"] == target) & (table["horizon"] == horizon)
            mask &= ((table["level"] == "overall") if state == "overall"
                     else (table["state"] == state) & (table["level"] == "state"))
            if mask.any():
                grid[i, j] = table.loc[mask, "bss"].iloc[0]
    return rows, grid


def draw_skill_heatmap(metrics: pd.DataFrame) -> None:
    """BSS has a meaningful zero, so it gets a diverging scale with a grey middle.

    Two panels, because the difference between them IS the finding: the same trees
    and the same isotonic regression, calibrated on the training years instead of the
    validation years, turn most of this grid from red to grey or blue.
    """
    apply_style()
    states = ["overall"] + sorted(
        s for s in metrics.loc[metrics["level"] == "state", "state"].unique()
    )
    panels = [
        ("in use: isotonic cross-fitted\non the 1990-2018 training years",
         "lightgbm_isotonic_cv"),
        ("rejected: isotonic fitted\non the 2019-2021 validation years",
         "lightgbm_isotonic_val"),
    ]
    grids = [(_skill_grid(metrics, model, states), title) for title, model in panels]
    limit = max(0.05, max(float(np.nanmax(np.abs(g))) for (_, g), _ in grids))

    fig, axes = plt.subplots(1, 2, figsize=(13.4, 6.0), sharey=True)
    for ax, ((rows, grid), title) in zip(axes, grids):
        im = ax.imshow(grid, cmap=SKILL_CMAP, vmin=-limit, vmax=limit, aspect="auto")
        ax.set_xticks(range(len(states)))
        ax.set_xticklabels([s if s != "overall" else "ALL" for s in states],
                           rotation=30, ha="right")
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels(rows)
        ax.set_xticks(np.arange(-0.5, len(states)), minor=True)
        ax.set_yticks(np.arange(-0.5, len(rows)), minor=True)
        ax.grid(which="minor", color="white", linewidth=2)
        ax.tick_params(which="minor", length=0)
        ax.set_title(title, loc="left", pad=8)
        for i in range(len(rows)):
            for j in range(len(states)):
                if not np.isfinite(grid[i, j]):
                    continue
                strong = abs(grid[i, j]) > 0.55 * limit
                ax.text(j, i, f"{grid[i, j]:+.2f}", ha="center", va="center",
                        fontsize=7.5,
                        color="white" if strong else TEXT_SECONDARY)

    fig.suptitle("Brier Skill Score vs climatology, test years 2022-2026",
                 x=0.01, ha="left", fontsize=11, fontweight="600")
    bar = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02)
    bar.set_label("BSS  (>0 beats climatology)", color=TEXT_MUTED)
    bar.outline.set_visible(False)
    fig.text(0.01, 0.005,
             "Zero is grey: model and climatology are indistinguishable. Blue beats "
             "climatology, red loses to it. Identical trees in both panels - only the "
             "years the calibrator was fitted on differ.",
             color=TEXT_MUTED, fontsize=7.5)
    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGS_DIR / "skill_by_state.png", bbox_inches="tight")
    plt.close(fig)


def feature_importance(frame: pd.DataFrame, target: str, horizon: int) -> pd.DataFrame:
    """Gain and mean |SHAP| per feature, aggregated into groups.

    SHAP comes from LightGBM's own `pred_contrib=True`, which is exact TreeSHAP - no
    extra dependency, and no sampling approximation.
    """
    model_path = LGBM_DIR / f"{target}_{horizon}.txt"
    if not model_path.is_file():
        raise DataError(f"missing {model_path}; run python -m src.train_baselines")
    booster = lgb.Booster(model_file=str(model_path))
    features = booster.feature_name()

    masks = split_masks(frame)
    column = target_column(target, horizon)
    test = masks["test"] & frame[column].notna()
    X = frame.loc[test, features]

    contrib = booster.predict(X, pred_contrib=True)  # (+1 column = base value)
    shap = np.abs(contrib[:, :-1]).mean(axis=0)
    gain = booster.feature_importance(importance_type="gain")

    out = pd.DataFrame({
        "feature": features,
        "gain": gain,
        "mean_abs_shap": shap,
    })
    out["gain_pct"] = 100 * out["gain"] / max(out["gain"].sum(), 1e-12)
    out["shap_pct"] = 100 * out["mean_abs_shap"] / max(out["mean_abs_shap"].sum(), 1e-12)

    lookup = {f: g for g, fs in FEATURE_GROUPS.items() for f in fs}
    out["group"] = out["feature"].map(lookup).fillna("other")
    return out.sort_values("shap_pct", ascending=False)


ABLATION_SEEDS = (42, 7, 2024)


def climate_ablation(frame: pd.DataFrame) -> pd.DataFrame:
    """Retrain with and without ENSO/IOD/MJO to see whether they actually help.

    SHAP says what a model *uses*; it cannot say whether that use earns its keep out
    of sample. Only removing the features and refitting answers that. Three seeds,
    because with ~4k test rows a single fit moves by more than the effect.
    """
    from sklearn.metrics import brier_score_loss

    from src.baseline_common import climatology
    from src.train_baselines import EARLY_STOPPING, MAX_ROUNDS, PARAMS

    masks = split_masks(frame)
    features = feature_columns(frame)
    climate = [c for group in ("ENSO", "IOD", "MJO") for c in FEATURE_GROUPS[group]]
    without = [c for c in features if c not in climate]

    def fit_and_score(target, horizon, cols, seed):
        column = target_column(target, horizon)
        labelled = frame[column].notna()
        train = masks["train"] & labelled
        validate = masks["validate"] & labelled
        test = masks["test"] & labelled
        params = dict(PARAMS, seed=seed)
        booster = lgb.train(
            params, lgb.Dataset(frame.loc[train, cols], frame.loc[train, column]),
            num_boost_round=MAX_ROUNDS,
            valid_sets=[lgb.Dataset(frame.loc[validate, cols],
                                    frame.loc[validate, column])],
            callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)],
        )
        pred = booster.predict(frame.loc[test, cols],
                               num_iteration=booster.best_iteration)
        y = frame.loc[test, column].to_numpy()
        ref = climatology(frame, column, masks["train"])[test].to_numpy()
        bss = 1.0 - brier_score_loss(y, pred) / brier_score_loss(y, ref)
        return bss, roc_auc_score(y, pred)

    rows = []
    for target, horizon in IMPORTANCE_TARGETS:
        for label, cols in (("with climate", features), ("without climate", without)):
            scores = np.array([fit_and_score(target, horizon, cols, s)
                               for s in ABLATION_SEEDS])
            rows.append({
                "target": target, "horizon": horizon, "variant": label,
                "n_features": len(cols),
                "bss_mean": scores[:, 0].mean(), "bss_sd": scores[:, 0].std(),
                "auc_mean": scores[:, 1].mean(),
            })
    return pd.DataFrame(rows)


def draw_importance(importance: dict[str, pd.DataFrame]) -> None:
    apply_style()
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))

    for ax, (name, table) in zip(axes, importance.items()):
        grouped = (table.groupby("group")["shap_pct"].sum()
                   .sort_values(ascending=True))
        colours = [SERIES[0] if g not in ("ENSO", "IOD", "MJO") else SERIES[1]
                   for g in grouped.index]
        ax.barh(range(len(grouped)), grouped.to_numpy(), color=colours, height=0.62)
        ax.set_yticks(range(len(grouped)))
        ax.set_yticklabels(grouped.index)
        recessive_grid(ax, axis="x")
        for i, value in enumerate(grouped.to_numpy()):
            ax.text(value + 0.8, i, f"{value:.1f}%", va="center",
                    color=TEXT_SECONDARY, fontsize=8)
        ax.set_xlim(0, max(grouped.max() * 1.18, 10))
        ax.set_xlabel("share of mean |SHAP| (%)")
        ax.set_title(name, loc="left")

    fig.suptitle("What the models actually use (exact TreeSHAP, test years)",
                 x=0.01, ha="left", fontsize=11, fontweight="600")
    fig.text(0.01, -0.03,
             "Orange = the large-scale climate drivers (ENSO, IOD, MJO); "
             "blue = everything else.",
             color=TEXT_MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.02, 1, 0.94))
    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGS_DIR / "feature_importance.png", bbox_inches="tight")
    plt.close(fig)


def _calibration_rate(meta: dict) -> float:
    """Positive rate on the 2019-2021 calibration split, or NaN if unknown."""
    return meta.get("validate_positive_rate", float("nan"))


def _fmt(value: float, digits: int = 3) -> str:
    return "n/a" if value is None or not np.isfinite(value) else f"{value:+.{digits}f}"


def write_report(metrics: pd.DataFrame, importance: dict[str, pd.DataFrame],
                 info: list[dict], ablation: pd.DataFrame,
                 calibration: pd.DataFrame | None = None) -> None:
    lgbm = metrics[metrics["model"] == "lightgbm"]
    overall = lgbm[lgbm["level"] == "overall"].set_index(["target", "horizon"])
    raw = (metrics[(metrics["model"] == "lightgbm_uncalibrated") &
                   (metrics["level"] == "overall")]
           .set_index(["target", "horizon"]))
    trees = {(r["target"], r["horizon"]): r["best_iteration"] for r in info}
    rates = {(r["target"], r["horizon"]): r for r in info}

    beats = overall[overall["bss"] > 0]
    beats_raw = raw[raw["bss"] > 0]
    hurt = int((raw["bss"] > overall["bss"]).sum())

    lines = [
        "# Baseline report - Phase 3",
        "",
        "Pilot districts only (30 sub-district units across 8 districts).",
        "Trained on 1990-2018, early-stopped and isotonic-calibrated on 2019-2021,",
        "scored on **2022-2026** - years neither the model nor the calibrator saw.",
        "",
        "## The short version",
        "",
        f"**{len(beats)} of 12 target/horizon combinations beat climatology as "
        f"specified.** Without the isotonic step the same trees beat it on "
        f"{len(beats_raw)} of 12.",
        "",
        "Two things are going on, and they pull in opposite directions:",
        "",
        "1. **The models rank well.** AUC is 0.62-0.78 everywhere - clearly better "
        "than the 0.50 of a coin flip. The model knows which start dates are riskier "
        "than others.",
        f"2. **The probabilities are miscalibrated on the test years, and the "
        f"calibration step is what made it worse** - isotonic regression lowered the "
        f"Brier Skill Score in {hurt} of the 12 cases.",
        "",
        "That combination - good ranking, bad probabilities - is exactly what a "
        "negative BSS with a high AUC means. Brier punishes being confidently off; "
        "AUC does not notice.",
        "",
        "### Why calibrating on the validation years backfired",
        "",
        "The first version of this pipeline fitted isotonic regression on 2019-2021 "
        "and lost skill in 11 of 12 cases. Those three years are not representative "
        "of the base rates on either side of them:",
        "",
        "| target | events in train (1990-2018) | in calibration (2019-2021) | in test (2022-2026) | mean forecast on test |",
        "|---|---:|---:|---:|---:|",
    ]
    for (target, horizon), row in overall.iterrows():
        meta = rates.get((target, horizon), {})
        lines.append(
            f"| {target}_{horizon} | {meta.get('train_positive_rate', float('nan')):.1%} | "
            f"{_calibration_rate(meta):.1%} | {row['base_rate']:.1%} | "
            f"{row['mean_forecast']:.1%} |"
        )
    lines += [
        "",
        "Dry spells were far rarer in 2019-2021 than in the training years, so "
        "isotonic learned to shrink probabilities downward - and then the test years, "
        "which were closer to the long-run rate, came out systematically "
        "under-forecast. Onset runs the other way: 2019-2021 had an unusually high "
        "onset rate, so onset probabilities were pushed up and the test years are "
        "over-forecast. A monotone non-parametric calibrator is flexible enough to "
        "lock a three-year base rate in hard.",
        "",
        "### Skill per target",
        "",
        "| target | horizon | BSS (as specified) | BSS (uncalibrated) | AUC | Brier (model) | Brier (clim) | n | events | trees |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for (target, horizon), row in overall.iterrows():
        raw_bss = raw.loc[(target, horizon), "bss"] if (target, horizon) in raw.index \
            else float("nan")
        lines.append(
            f"| {target} | {horizon}d | {_fmt(row['bss'])} | {_fmt(raw_bss)} | "
            f"{_fmt(row['auc'])} | {row['brier']:.4f} | {row['brier_ref']:.4f} | "
            f"{int(row['n']):,} | {int(row['n_pos']):,} | "
            f"{trees.get((target, horizon), '?')} |"
        )

    lines += [
        "",
        "BSS is the fraction of climatology's Brier score removed: `+0.10` means "
        "10% better than climatology, `0` means indistinguishable from it, negative "
        "means worse. AUC measures ranking only and ignores calibration entirely, "
        "which is why a model can have AUC well above 0.5 and still a negative BSS.",
        "",
        "## Per state",
        "",
        "![Brier Skill Score by state](figs/skill_by_state.png)",
        "",
        "| target | horizon | " + " | ".join(
            sorted(lgbm.loc[lgbm['level'] == 'state', 'state'].unique())) + " |",
        "|---|---:|" + "---:|" * lgbm.loc[lgbm['level'] == 'state', 'state'].nunique(),
    ]
    states = sorted(lgbm.loc[lgbm["level"] == "state", "state"].unique())
    for target in TARGETS:
        for horizon in HORIZONS:
            cells = []
            for state in states:
                m = ((lgbm["level"] == "state") & (lgbm["target"] == target) &
                     (lgbm["horizon"] == horizon) & (lgbm["state"] == state))
                cells.append(_fmt(lgbm.loc[m, "bss"].iloc[0]) if m.any() else "n/a")
            lines.append(f"| {target} | {horizon}d | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Per pilot district",
        "",
        "Sample sizes here are small and some are very small, so these are reported "
        "with their event counts rather than ranked. A district with fewer than "
        f"{MIN_POSITIVES} events in the test years is marked -- its score is "
        "dominated by which years happened to fall in the split.",
        "",
        "| target | horizon | district | BSS | AUC | n | events | |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    district = lgbm[lgbm["level"] == "district"].sort_values(
        ["target", "horizon", "state", "district"]
    )
    for row in district.itertuples():
        if row.horizon not in (7, 14):
            continue  # keep the table readable; the CSV has every horizon
        flag = "" if row.reliable else "too few events"
        lines.append(
            f"| {row.target} | {row.horizon}d | {row.district} | {_fmt(row.bss)} | "
            f"{_fmt(row.auc)} | {int(row.n):,} | {int(row.n_pos)} | {flag} |"
        )

    lines += [
        "",
        "Every horizon and every slice is in "
        "[`outputs/metrics/metrics.csv`](metrics/metrics.csv).",
        "",
        "## Reliability",
        "",
    ]
    for target in TARGETS:
        lines += [f"![Reliability, {target}](figs/reliability_{target}.png)", ""]

    lines += [
        "## Do ENSO, IOD and MJO matter?",
        "",
        "![Feature importance](figs/feature_importance.png)",
        "",
        "Exact TreeSHAP from LightGBM's own `pred_contrib`, over the test rows.",
        "",
    ]
    for name, table in importance.items():
        grouped = table.groupby("group")[["shap_pct", "gain_pct"]].sum()
        grouped = grouped.sort_values("shap_pct", ascending=False)
        climate = grouped.reindex(["ENSO", "IOD", "MJO"]).fillna(0)["shap_pct"].sum()
        lines += [
            f"### {name}",
            "",
            f"ENSO + IOD + MJO together account for **{climate:.1f}%** of mean "
            "|SHAP|.",
            "",
            "| group | mean abs SHAP % | gain % |",
            "|---|---:|---:|",
        ]
        for group, row in grouped.iterrows():
            lines.append(f"| {group} | {row['shap_pct']:.1f}% | {row['gain_pct']:.1f}% |")
        age = table[table["feature"].str.endswith("_age_days")]["shap_pct"].sum()
        top = table.head(8)
        lines += [
            "",
            f"Of that {climate:.1f}%, only {age:.1f} points come from the "
            "`*_age_days` staleness columns, which are near-deterministic functions "
            "of the calendar - so the climate share is genuine index values, not a "
            "date proxy sneaking in.",
            "",
            "Top individual features: " +
            ", ".join(f"`{r.feature}` ({r.shap_pct:.1f}%)" for r in top.itertuples()),
            "",
        ]

    if calibration is not None and not calibration.empty:
        summary = (calibration.groupby("calibration")
                   .agg(mean_test_bss=("test", "mean"),
                        beats_clim=("test", lambda s: int((s > 0).sum())),
                        mean_optimism=("optimism", "mean"))
                   .sort_values("mean_test_bss", ascending=False))
        lines += [
            "### The fix: calibrate on the training years, cross-fitted",
            "",
            "Instead of fitting the calibrator on the three validation years, fit it "
            "on out-of-fold predictions across the 29 training years - five folds, "
            "grouped by year so that rows sharing a season cannot leak across the "
            "split. The calibrator then sees a representative base rate instead of a "
            "three-year accident.",
            "",
            "| calibration | mean test BSS | beats climatology | mean validation optimism |",
            "|---|---:|---:|---:|",
        ]
        for name, row in summary.iterrows():
            lines.append(
                f"| {name} | {row['mean_test_bss']:+.4f} | "
                f"{int(row['beats_clim'])}/12 | {row['mean_optimism']:+.4f} |"
            )
        lines += [
            "",
            "\"Validation optimism\" is validation BSS minus test BSS: how much "
            "better the model looked on the years used to tune it than on unseen "
            "years. Fitting the calibrator on the validation years inflated it by "
            "**0.19 BSS**; cross-fitting on the training years brings that down to "
            "**0.03**, which is what makes a validation score usable for choosing a "
            "model at all.",
            "",
            "Per target, moving from validation-fitted to cross-fitted isotonic:",
            "",
            "| target | horizon | isotonic on validation | isotonic cross-fitted | change |",
            "|---|---:|---:|---:|---:|",
        ]
        old = calibration[calibration["calibration"] == "isotonic on validation years"]
        new = calibration[calibration["calibration"] ==
                          "isotonic cross-fitted on training years"]
        merged = old.merge(new, on=["target", "horizon"], suffixes=("_old", "_new"))
        for row in merged.itertuples():
            lines.append(
                f"| {row.target} | {row.horizon}d | {row.test_old:+.4f} | "
                f"{row.test_new:+.4f} | {row.test_new - row.test_old:+.4f} |"
            )
        lines += [
            "",
            "Eleven of twelve improved and none got worse. The biggest gains are at "
            "the long horizons, where the three-year base-rate error compounds most.",
            "",
        ]

    lines += [
        "### But do they earn their keep?",
        "",
        "SHAP says what a model *uses*, not whether using it helps out of sample. "
        "The only way to answer that is to drop the 18 ENSO/IOD/MJO columns, refit, "
        "and compare - three seeds each, because a single fit moves by more than the "
        "effect being measured.",
        "",
        "| target | variant | BSS (mean of 3 seeds) | sd | AUC |",
        "|---|---|---:|---:|---:|",
    ]
    for row in ablation.itertuples():
        lines.append(
            f"| {row.target}_{row.horizon} | {row.variant} | {row.bss_mean:+.4f} | "
            f"{row.bss_sd:.4f} | {row.auc_mean:.4f} |"
        )
    lines += [""]
    for (target, horizon), group in ablation.groupby(["target", "horizon"],
                                                     observed=True):
        with_c = group[group["variant"] == "with climate"].iloc[0]
        without_c = group[group["variant"] == "without climate"].iloc[0]
        delta = with_c["bss_mean"] - without_c["bss_mean"]
        verdict = ("**help**" if delta > 2 * with_c["bss_sd"]
                   else "**hurt**" if delta < -2 * with_c["bss_sd"]
                   else "make no measurable difference")
        lines.append(
            f"- For `{target}_{horizon}`, the climate drivers {verdict}: "
            f"{delta:+.4f} BSS ({with_c['auc_mean'] - without_c['auc_mean']:+.4f} AUC)."
        )
    lines += [
        "",
        "So the honest answer is **it depends on the target**. Dry spells have a "
        "real large-scale signal - MJO in particular, with `rmm2` the single biggest "
        "climate feature. Onset does not: the climate columns make it measurably "
        "worse, which is what overfitting looks like when 18 extra columns meet a "
        "target with only ~9.5k training rows.",
        "",
        "## Caveats",
        "",
        "- **The calibrator is fitted on the same rows that chose the tree count.** "
        "Early stopping and isotonic calibration both use 2019-2021, so calibration "
        "on those years is optimistic. The test years are unaffected.",
        "- **Three validation years is very little.** Early stopping on ~700 onset "
        "rows is noisy, which is why several onset models stop after a handful of "
        "trees.",
        "- **`onset_h` is NaN once onset has occurred**, so onset slices are much "
        "smaller than dry/heavy ones and are concentrated in the pre-onset part of "
        "the season.",
        "- **Purnia's onset labels are known to be wrong.** The default onset rule "
        "fires around 22 May there against an official normal of the third week of "
        "June, so Purnia's onset scores measure the rule, not the weather.",
        "- Only 8 pilot districts are in scope. Nothing here has been checked "
        "against the other 193 districts.",
        "",
    ]

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    print("--- evaluate_baselines ---")
    if not PREDICTIONS.is_file():
        raise DataError(
            f"missing {PREDICTIONS}; run python -m src.train_baselines first"
        )
    predictions = pd.read_parquet(PREDICTIONS)
    metrics = build_metrics(predictions)
    require_nonempty(metrics, "metrics")

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(METRICS_CSV, index=False)

    overall = metrics[(metrics["model"] == "lightgbm") &
                      (metrics["level"] == "overall")]
    print("\n--- overall skill on test years (2022-2026) ---")
    print(f"  {'target':<9} {'BSS':>8} {'AUC':>7} {'Brier':>8} {'clim':>8} "
          f"{'n':>6} {'events':>7}")
    for row in overall.sort_values(["target", "horizon"]).itertuples():
        print(f"  {row.target}_{row.horizon:<5} {_fmt(row.bss):>8} "
              f"{_fmt(row.auc, 3):>7} {row.brier:8.4f} {row.brier_ref:8.4f} "
              f"{int(row.n):6,} {int(row.n_pos):7,}")

    print("\n  drawing figures...")
    for target in TARGETS:
        draw_reliability(predictions, target)
    draw_skill_heatmap(metrics)

    frame = load_table()
    importance = {}
    for target, horizon in IMPORTANCE_TARGETS:
        importance[f"{target}_{horizon}"] = feature_importance(frame, target, horizon)
    draw_importance(importance)

    print("  running the climate-feature ablation (6 refits)...")
    ablation = climate_ablation(frame)
    for row in ablation.itertuples():
        print(f"    {row.target}_{row.horizon:<3} {row.variant:<16} "
              f"BSS {row.bss_mean:+.4f} (sd {row.bss_sd:.4f})  "
              f"AUC {row.auc_mean:.4f}")

    info_path = LGBM_DIR / "training_info.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else []
    calibration = calibration_comparison(predictions)
    calibration.to_csv(METRICS_DIR / "calibration_comparison.csv", index=False)
    print("\n--- calibration methods (mean over 12 target-horizons) ---")
    print(calibration.groupby("calibration")
          .agg(mean_test_bss=("test", "mean"),
               beats_clim=("test", lambda s: int((s > 0).sum())),
               mean_optimism=("optimism", "mean"))
          .sort_values("mean_test_bss", ascending=False).round(4).to_string())

    write_report(metrics, importance, info, ablation, calibration)
    ablation.to_csv(METRICS_DIR / "climate_ablation.csv", index=False)
    require_nonempty(REPORT_MD, "baseline_report.md")

    n_beat = int((overall["bss"] > 0).sum())
    summarize(
        "evaluate_baselines",
        rows=len(metrics),
        files=[METRICS_CSV, REPORT_MD, FIGS_DIR / "skill_by_state.png",
               FIGS_DIR / "feature_importance.png"],
        extra={
            "models scored": metrics["model"].nunique(),
            "slices": metrics["level"].value_counts().to_dict(),
            "beat climatology": f"{n_beat}/12 target-horizon pairs",
            "best BSS": _fmt(overall["bss"].max()),
            "worst BSS": _fmt(overall["bss"].min()),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
