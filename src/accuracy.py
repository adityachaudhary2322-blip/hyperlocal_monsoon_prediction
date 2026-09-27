"""Forecast scoring, in numbers and in plain words - one module for the page and terminal.

Run:  python -m src.accuracy              # build app/assets/accuracy_test_years.json + print
      python -m src.accuracy --live       # also score live verification from the DATABASE_URL

No single "overall accuracy %" is ever produced: a 90% "accuracy" is easy for a rare
event by always saying "no", so every figure here is per hazard and week, and set against
the historical average (the Brier skill score) and the event's base rate.

Numpy / pandas only (the website cannot import scikit-learn): AUC is the Mann-Whitney
statistic from average ranks.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from src.common import DATA_PROCESSED, ROOT, load_config
from src.report import DataError, summarize

TEST_YEARS_JSON = ROOT / "app" / "assets" / "accuracy_test_years.json"
HAZARD_NAME = {"onset": "Monsoon onset", "dry": "Dry spell", "heavy": "Heavy rain",
               "withdrawal": "Monsoon withdrawal", "late_heavy": "Heavy rain at harvest",
               "rabi_moisture": "Too dry for rabi sowing"}


def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    pos = y == 1
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if not n1 or not n0:
        return None
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def score(y: np.ndarray, p: np.ndarray, p_clim: np.ndarray | None = None) -> dict:
    """All the numbers for one set of forecasts. p in [0, 1]; y in {0, 1}."""
    cfg = load_config("accuracy")
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    if p_clim is None:
        p_clim = np.full_like(p, y.mean() if y.size else np.nan)   # sample base rate
    else:
        p_clim = np.asarray(p_clim, dtype=float)[ok]
    n, events = int(y.size), int(y.sum())
    if not n:
        return {"n": 0, "events": 0}
    brier = float(np.mean((p - y) ** 2))
    brier_ref = float(np.mean((p_clim - y) ** 2))
    warned = p >= cfg["warning_threshold"]
    bins = []
    for b in cfg["bins"]:
        sel = (p >= b["lo"]) & (p < b["hi"])
        bins.append({"name": b["name"], "label": b["label"], "n": int(sel.sum()),
                     "observed": float(y[sel].mean()) if sel.any() else None,
                     "forecast": float(p[sel].mean()) if sel.any() else None})
    return {
        "n": n, "events": events, "base_rate": events / n,
        "brier": brier, "brier_ref": brier_ref,
        "bss": None if brier_ref == 0 else 1 - brier / brier_ref,
        "auc": auc(y, p),
        "hit_rate": float(warned[y == 1].mean()) if events else None,
        "false_alarm_ratio": float((y[warned] == 0).mean()) if warned.any() else None,
        "warnings": int(warned.sum()), "bins": bins,
    }


def verdict(bss: float | None) -> str:
    if bss is None:
        return "not enough data"
    band = load_config("accuracy")["same_band"]
    return "better" if bss > band else "worse" if bss < -band else "about the same"


def sentences(s: dict) -> list[str]:
    """Plain reliability sentences, e.g. 'When we said high chance, it happened 7 in 10 times.'"""
    out = []
    for b in s.get("bins", []):
        if b["n"] >= 10 and b["observed"] is not None:
            out.append(f"When we said {b['label']}, it happened "
                       f"{round(b['observed'] * 10)} in 10 times ({b['n']:,} forecasts).")
    return out


# --------------------------------------------------------------------------
# Past seasons (test years) from the pilot model's saved predictions
# --------------------------------------------------------------------------
def test_years() -> dict:
    path = DATA_PROCESSED / "predictions.parquet"
    if not path.is_file():
        raise DataError(f"missing {path}; run python -m src.train_baselines")
    pred = pd.read_parquet(path)
    test = pred[pred["split"] == "test"]
    if test.empty:
        raise DataError("no test-split rows")
    years = f"{int(test['year'].min())}-{int(test['year'].max())}"
    rows = []
    for (target, horizon), g in test.groupby(["target", "horizon"]):
        for state, sub in [("All validated states", g)] + list(g.groupby("state")):
            s = score(sub["y_true"].to_numpy(), sub["p_lgbm"].to_numpy(), sub["p_clim"].to_numpy())
            rows.append({"hazard": target, "week": int(horizon // 7), "state": state,
                         "tier": "validated", **s, "verdict": verdict(s.get("bss")),
                         "sentences": sentences(s)})
    return {"years": years, "model": "LightGBM, cross-fitted isotonic calibration",
            "scope": "pilot sub-districts of the 5 validated states", "rows": rows}


def print_table(rows: list[dict], title: str) -> None:
    print(f"\n{title}")
    print(f"  {'hazard':<14}{'wk':>3}{'tier':>14}{'n':>8}{'events':>8}{'BSS':>8}{'AUC':>7}"
          f"{'hit':>7}{'FAR':>7}  vs history")
    for r in rows:
        if r.get("state") not in (None, "All validated states", "all"):
            continue

        def f(v, fmt):
            return format(v, fmt) if v is not None else "-"
        print(f"  {r['hazard']:<14}{r['week']:>3}{r['tier']:>14}{r['n']:>8,}{r['events']:>8,}"
              f"{f(r.get('bss'), '+.3f'):>8}{f(r.get('auc'), '.2f'):>7}"
              f"{f(r.get('hit_rate'), '.0%'):>7}{f(r.get('false_alarm_ratio'), '.0%'):>7}"
              f"  {r['verdict']}")
    better = [f"{r['hazard']} wk{r['week']}" for r in rows
              if r.get("state") in ("All validated states", "all") and r["verdict"] == "better"]
    print(f"  beats climatology: {', '.join(better) or 'none'}")


def live_scorecard(session) -> list[dict]:
    """Aggregate live_verification by hazard x horizon x tier."""
    from sqlalchemy import select

    from src.db.models import LiveVerification as V

    frame = pd.read_sql(select(V.hazard, V.horizon, V.tier, V.state, V.p_blend, V.p_ml,
                               V.p_ec46, V.outcome, V.run_date), session.connection())
    rows = []
    for (hazard, horizon, tier), g in frame.groupby(["hazard", "horizon", "tier"]):
        y = g["outcome"].astype(float).to_numpy()
        entry = {"hazard": hazard, "week": int(horizon // 7), "tier": tier, "state": "all"}
        for col in ("p_blend", "p_ml", "p_ec46"):
            p = g[col].to_numpy(dtype=float) / 1000
            entry[col] = score(y, p) if np.isfinite(p).any() else None
        s = entry["p_blend"] or {"n": 0, "events": 0}
        rows.append({**entry, **s, "verdict": verdict(s.get("bss")), "sentences": sentences(s)})
    return rows


def suggest_blend_weights(frame: pd.DataFrame) -> dict:
    """Per hazard x horizon: the ML weight w in 0.0..1.0 that would have minimised the
    Brier score on verified forecasts where both ML and EC46 exist. A SUGGESTION only:
    config/blend.yaml is never changed automatically (it is shown on the Models page)."""
    need = load_config("accuracy")["min_verified"]
    out = {}
    for (hazard, horizon), g in frame.groupby(["hazard", "horizon"]):
        both = g.dropna(subset=["p_ml", "p_ec46"])
        key = f"{hazard}_{int(horizon)}"
        if len(both) < need:
            out[key] = {"n": int(len(both)), "suggested_w": None,
                        "note": "needs ML and EC46 forecasts for the same verified days"}
            continue
        y = both["outcome"].astype(float).to_numpy()
        ml, ec = both["p_ml"].to_numpy() / 1000, both["p_ec46"].to_numpy() / 1000
        grid = np.round(np.arange(0, 1.01, 0.1), 1)
        briers = [float(np.mean((w * ml + (1 - w) * ec - y) ** 2)) for w in grid]
        best = int(np.argmin(briers))
        out[key] = {"n": int(len(both)), "suggested_w": float(grid[best]),
                    "brier_at_suggested": briers[best], "brier_ml_only": briers[-1],
                    "brier_ec46_only": briers[0]}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print("--- accuracy ---")
    data = test_years()
    TEST_YEARS_JSON.write_text(json.dumps(data, indent=1, default=float), encoding="utf-8")
    print_table(data["rows"], f"Past seasons {data['years']} ({data['scope']})")
    print("  experimental tier: no test-year scores yet - national models are coming soon")
    if args.live:
        from src.db.session import session_scope

        with session_scope() as session:
            rows = live_scorecard(session)
        if rows:
            print_table(rows, "Live verification (blend)")
        else:
            print("\nLive verification: nothing verified yet")
    summarize("accuracy", rows=len(data["rows"]), files=[TEST_YEARS_JSON],
              extra={"test years": data["years"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
