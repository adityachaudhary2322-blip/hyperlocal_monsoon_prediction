"""Build the Phase 2 training table: one row per pilot unit x forecast start date.

Run:  python -m src.build_features [--all-units] [--force]

Timing convention (this is the whole no-leakage story)
-----------------------------------------------------
For a forecast issued on start date `s`:

  * rainfall features use days  ... s-1   (strictly before s: day s is not over yet)
  * climate indices use the value published as of day s (fetch_indices already models
    each provider's publication lag, so this is genuinely knowable on day s)
  * horizon h covers days      s ... s+h-1   (exactly h days, starting on s)

So features and targets never overlap, and no feature can see a day inside any
horizon. tests/test_features.py asserts this directly.

Climatology comes from the training years only (CLAUDE.md section 11), keyed by
(unit, calendar month-day, window length) so a start date is compared against the same
calendar window in other years - no smoothing, no leakage from validate/test years.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import pandas as pd

from src.common import (
    DATA_PROCESSED,
    EQUAL_AREA_CRS,
    INDICES_PARQUET,
    UNITS_GPKG,
    UNIT_RAIN_PARQUET,
    load_config,
    pilot_districts,
    splits,
)
from src.onset import forecast_start_dates, onset_config, _dry_spell_starts
from src.report import DataError, require_nonempty, summarize

TRAIN_TABLE = DATA_PROCESSED / "train_table.parquet"

HORIZONS = (7, 14, 21, 28)
TRAILING_WINDOWS = (7, 14, 30)

HEAVY_RAIN_MM = 64.5      # IMD "heavy rainfall" threshold for a single day
DRY_DAY_MM = 2.5          # a day below this counts as dry for the dry-spell target
DRY_SPELL_DAYS = 10       # a run this long is the dry-spell event
WET_DAY_MM = 10.0         # "days since last >=10 mm day"
SEASON_ANCHOR = "05-15"   # season-to-date is measured from here (= onset search start)
MJO_PHASES = tuple(range(1, 9))


def load_units(all_units: bool) -> gpd.GeoDataFrame:
    units = gpd.read_file(UNITS_GPKG, layer="units")
    if "zone_id" not in units.columns:
        raise DataError(
            f"{UNITS_GPKG.name} has no zone_id column; run python -m src.build_zones"
        )
    if not all_units:
        pilots = pilot_districts()
        mask = np.zeros(len(units), dtype=bool)
        for state, districts in pilots.items():
            mask |= (units["state"] == state) & units["district"].isin(districts)
        units = units[mask].copy()
        if units.empty:
            raise DataError("no pilot units matched; check config/project.yaml pilots")
    centroid = units.to_crs(EQUAL_AREA_CRS).geometry.centroid.to_crs("EPSG:4326")
    units["centroid_lat"] = centroid.y.to_numpy()
    units["centroid_lon"] = centroid.x.to_numpy()
    return units.reset_index(drop=True)


def load_rain_matrix(unit_ids: list[str]) -> pd.DataFrame:
    """(date x unit) rainfall, reindexed to a gap-free daily calendar."""
    table = pd.read_parquet(
        UNIT_RAIN_PARQUET, columns=["unit_id", "date", "rain_mm"],
        filters=[("unit_id", "in", unit_ids)],
    )
    if table.empty:
        raise DataError("unit_rain.parquet yielded no rows for the requested units")
    wide = table.pivot(index="date", columns="unit_id", values="rain_mm")
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    wide = wide.reindex(full).reindex(columns=unit_ids)
    wide.index.name = "date"
    return wide.astype("float32")


def window_sums(cum: np.ndarray, starts: np.ndarray, length: int) -> np.ndarray:
    """Sum over [start, start+length) for each start index; NaN where out of range."""
    n = cum.shape[0] - 1
    ok = (starts >= 0) & (starts + length <= n)
    out = np.full((starts.size, cum.shape[1]), np.nan, dtype="float64")
    idx = np.nonzero(ok)[0]
    if idx.size:
        s = starts[idx]
        out[idx] = cum[s + length] - cum[s]
    return out


def build_onset_dates(rain: pd.DataFrame, units: gpd.GeoDataFrame) -> pd.DataFrame:
    """Onset date per (unit_id, year), using each unit's zone-resolved config."""
    cfgs = {
        row.unit_id: onset_config(zone_id=row.zone_id, state=row.state)
        for row in units.itertuples()
    }
    from src.onset import detect_onset

    rows = []
    years = sorted(set(rain.index.year))
    for unit_id in rain.columns:
        cfg = cfgs[unit_id]
        for year in years:
            # Give the detector the confirmation window's worth of days past the
            # search end, or a late candidate can never be confirmed.
            lo = pd.Timestamp(f"{year}-01-01")
            hi = pd.Timestamp(f"{year}-12-31")
            sel = rain.loc[lo:hi, unit_id]
            if sel.isna().all():
                rows.append({"unit_id": unit_id, "year": year, "onset_date": pd.NaT})
                continue
            onset = detect_onset(
                sel.to_numpy(dtype="float64"), pd.DatetimeIndex(sel.index), cfg
            )
            rows.append({
                "unit_id": unit_id, "year": year,
                "onset_date": pd.Timestamp(onset) if onset is not None else pd.NaT,
            })
    return pd.DataFrame(rows)


#: Columns produced by build_raw_rows that are features (known at the start date).
#: Kept explicit so tests/test_features.py can assert none of them moves when future
#: rainfall changes.
RAW_FEATURE_COLUMNS = (
    [f"rain_prev_{w}" for w in TRAILING_WINDOWS]
    + ["season_to_date", "days_since_wet", "onset_happened", "doy_sin", "doy_cos"]
)
RAW_TARGET_COLUMNS = [
    f"{t}_{h}" for t in ("total", "heavy", "dry", "onset") for h in HORIZONS
]


def build_raw_rows(rain: pd.DataFrame, onset_lookup: dict,
                   last_observed: pd.Timestamp) -> pd.DataFrame:
    """One row per (unit, start date) with raw features and targets.

    Features read only days strictly before the start date; targets read only days
    from the start date onward. Separated out so the leakage test can rebuild with
    future rainfall perturbed and assert the feature columns are untouched.
    """
    dates = pd.DatetimeIndex(rain.index)
    unit_ids = list(rain.columns)
    values = rain.to_numpy(dtype="float64")

    filled = np.nan_to_num(values, nan=0.0)
    cum = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(filled, axis=0)])
    heavy_day = np.nan_to_num(values, nan=-1.0) >= HEAVY_RAIN_MM
    wet_day = np.nan_to_num(values, nan=-1.0) >= WET_DAY_MM
    dry_start = np.column_stack([
        _dry_spell_starts(values[:, j], DRY_DAY_MM, DRY_SPELL_DAYS)
        for j in range(values.shape[1])
    ])
    # Index of the most recent wet day at or before each row, per unit.
    order = np.arange(dates.size)[:, None]
    last_wet = np.maximum.accumulate(np.where(wet_day, order, -1), axis=0)

    cfg0 = onset_config()
    confirm_lag = pd.Timedelta(days=cfg0.accum_days + cfg0.confirm_window_days)
    pos = {d: i for i, d in enumerate(dates)}
    n = len(unit_ids)

    records: list[pd.DataFrame] = []
    for year in sorted(set(dates.year)):
        for start in forecast_start_dates(year):
            if start not in pos:
                continue
            i = pos[start]
            row: dict[str, np.ndarray] = {
                "unit_id": np.array(unit_ids, dtype=object),
                "start_date": np.repeat(start, n),
                "year": np.full(n, year, dtype="int16"),
            }

            # --- targets: days s .. s+h-1 -------------------------------------
            for h in HORIZONS:
                end = i + h
                horizon_end = start + pd.Timedelta(days=h - 1)
                if end <= dates.size:
                    observed = np.isfinite(values[i:end]).all(axis=0)
                    row[f"total_{h}"] = np.where(observed, cum[end] - cum[i], np.nan)
                    row[f"heavy_{h}"] = np.where(
                        observed, heavy_day[i:end].any(axis=0).astype(float), np.nan)
                    row[f"dry_{h}"] = np.where(
                        observed, dry_start[i:end].any(axis=0).astype(float), np.nan)
                else:
                    nan = np.full(n, np.nan)
                    row[f"total_{h}"] = nan.copy()
                    row[f"heavy_{h}"] = nan.copy()
                    row[f"dry_{h}"] = nan.copy()

                flags = np.empty(n, dtype="float64")
                for j, unit_id in enumerate(unit_ids):
                    od = onset_lookup.get((unit_id, year), pd.NaT)
                    if pd.notna(od) and od < start:
                        flags[j] = np.nan          # onset already happened
                    elif horizon_end > last_observed:
                        flags[j] = np.nan          # outcome not yet knowable
                    elif pd.notna(od) and start <= od <= horizon_end:
                        flags[j] = 1.0
                    else:
                        flags[j] = 0.0
                row[f"onset_{h}"] = flags

            # --- features: days ... s-1 ---------------------------------------
            for w in TRAILING_WINDOWS:
                row[f"rain_prev_{w}"] = window_sums(cum, np.array([i - w]), w)[0]

            anchor = pd.Timestamp(f"{year}-{SEASON_ANCHOR}")
            a = pos.get(anchor)
            row["season_to_date"] = (
                cum[i] - cum[a] if a is not None and i >= a else np.zeros(n)
            )

            if i > 0:
                previous = last_wet[i - 1]
                row["days_since_wet"] = np.where(
                    previous < 0, np.nan, (i - 1) - previous).astype("float64")
            else:
                row["days_since_wet"] = np.full(n, np.nan)

            happened = np.empty(n, dtype="float64")
            for j, unit_id in enumerate(unit_ids):
                od = onset_lookup.get((unit_id, year), pd.NaT)
                # Knowable only once the confirmation window has closed, otherwise
                # this feature would depend on days at or after the start date.
                happened[j] = 1.0 if pd.notna(od) and od + confirm_lag <= start else 0.0
            row["onset_happened"] = happened

            doy = start.dayofyear
            days_in_year = 366 if start.is_leap_year else 365
            row["doy_sin"] = np.full(n, np.sin(2 * np.pi * doy / days_in_year))
            row["doy_cos"] = np.full(n, np.cos(2 * np.pi * doy / days_in_year))

            records.append(pd.DataFrame(row))

    if not records:
        raise DataError("no forecast start dates fell inside the rainfall record")
    return pd.concat(records, ignore_index=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-units", action="store_true",
                        help="build for all units, not just the pilot districts")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if TRAIN_TABLE.is_file() and not args.force:
        print(f"{TRAIN_TABLE} exists; pass --force to rebuild")
        return 0

    print("--- build_features ---")
    units = load_units(args.all_units)
    unit_ids = units["unit_id"].tolist()
    print(f"  {len(units)} units "
          f"({'all' if args.all_units else 'pilot districts only'})")

    rain = load_rain_matrix(unit_ids)
    dates = pd.DatetimeIndex(rain.index)
    last_observed = dates[np.isfinite(rain.to_numpy()).any(axis=1)][-1]
    print(f"  rainfall {dates[0]:%Y-%m-%d} -> {dates[-1]:%Y-%m-%d} "
          f"(last observed {last_observed:%Y-%m-%d})")

    onset = build_onset_dates(rain, units)
    onset_lookup = {(r.unit_id, r.year): r.onset_date for r in onset.itertuples()}

    frame = build_raw_rows(rain, onset_lookup, last_observed)
    require_nonempty(frame, "training rows")
    years = sorted(set(dates.year))
    print(f"  {len(frame):,} rows ({len(unit_ids)} units x "
          f"{len(forecast_start_dates(years[0]))} start dates x {len(years)} years)")

    # --- climatology and anomalies, from training years only -----------------
    train_lo, train_hi = splits()["train"]
    frame["start_md"] = frame["start_date"].dt.strftime("%m-%d")
    is_train = frame["year"].between(train_lo, train_hi)
    print(f"  climatology from training years {train_lo}-{train_hi} only "
          f"({int(is_train.sum()):,} of {len(frame):,} rows)")

    anomaly_cols = [f"total_{h}" for h in HORIZONS] + \
                   [f"rain_prev_{w}" for w in TRAILING_WINDOWS]
    clim = (frame[is_train]
            .groupby(["unit_id", "start_md"], observed=True)[anomaly_cols]
            .mean()
            .rename(columns={c: f"{c}_clim" for c in anomaly_cols})
            .reset_index())
    frame = frame.merge(clim, on=["unit_id", "start_md"], how="left")
    for h in HORIZONS:
        frame[f"anomaly_{h}"] = frame[f"total_{h}"] - frame[f"total_{h}_clim"]
    for w in TRAILING_WINDOWS:
        frame[f"rain_prev_{w}_anom"] = (
            frame[f"rain_prev_{w}"] - frame[f"rain_prev_{w}_clim"]
        )
    frame = frame.drop(columns=[f"{c}_clim" for c in anomaly_cols])

    # --- climate indices as published on the start date ----------------------
    indices = pd.read_parquet(INDICES_PARQUET)
    keep = ["date", "oni", "oni_age_days", "dmi", "dmi_age_days", "nino34",
            "nino34_age_days", "rmm1", "rmm2", "amplitude", "phase", "mjo_age_days"]
    indices = indices[keep].rename(columns={"date": "start_date"})
    frame = frame.merge(indices, on="start_date", how="left")
    for phase in MJO_PHASES:
        frame[f"mjo_phase_{phase}"] = (frame["phase"] == phase).astype("float32")
    frame = frame.drop(columns=["phase"])

    # --- static unit attributes ---------------------------------------------
    frame = frame.merge(
        units[["unit_id", "unit_name", "district", "state", "zone_id",
               "centroid_lat", "centroid_lon"]],
        on="unit_id", how="left",
    )
    weights = np.load(DATA_PROCESSED / "weights.npz", allow_pickle=False)
    small = pd.Series(weights["small_unit"], index=weights["unit_id"])
    frame["small_unit"] = frame["unit_id"].map(small).astype("float32")

    # float32 for every numeric column (CLAUDE.md section 2); identifiers and
    # categoricals stay as strings because they cannot be float32.
    id_cols = ["unit_id", "unit_name", "district", "state", "zone_id",
               "start_date", "start_md", "year"]
    for column in frame.columns:
        if column not in id_cols:
            frame[column] = frame[column].astype("float32")
    frame["zone_id"] = frame["zone_id"].fillna("UNZONED")

    report(frame, onset, units)

    TRAIN_TABLE.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(TRAIN_TABLE, index=False)
    require_nonempty(TRAIN_TABLE, "train_table.parquet")

    # anomaly_h is a target too (observed total minus training climatology), so it
    # must not be counted as a feature.
    label_cols = [f"{t}_{h}"
                  for t in ("onset", "dry", "heavy", "total", "anomaly")
                  for h in HORIZONS]
    summarize(
        "build_features",
        rows=len(frame),
        files=[TRAIN_TABLE],
        date_range=(f"{frame['start_date'].min():%Y-%m-%d}",
                    f"{frame['start_date'].max():%Y-%m-%d}"),
        extra={
            "units": len(unit_ids),
            "start dates/year": len(forecast_start_dates(years[0])),
            "years": f"{years[0]}-{years[-1]}",
            "columns": len(frame.columns),
            "feature columns": len(frame.columns) - len(label_cols) - len(id_cols),
            "horizons": ", ".join(map(str, HORIZONS)),
            "rows with any label": int(frame[label_cols].notna().any(axis=1).sum()),
            "last observed day": f"{last_observed:%Y-%m-%d}",
        },
    )
    return 0


def report(frame: pd.DataFrame, onset: pd.DataFrame,
           units: gpd.GeoDataFrame) -> None:
    print("\n--- class balance: positive rate per target x horizon x state ---")
    header = f"  {'state':<16} {'target':<7}" + "".join(f"{h:>12}" for h in HORIZONS)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for state in sorted(frame["state"].unique()):
        sub = frame[frame["state"] == state]
        for target in ("onset", "dry", "heavy"):
            cells = []
            for h in HORIZONS:
                col = sub[f"{target}_{h}"].dropna()
                cells.append(
                    f"{100 * col.mean():6.1f}% n={len(col):<5}" if len(col)
                    else "        n/a"
                )
            print(f"  {state:<16} {target:<7}" + "".join(f"{c:>12}" for c in cells))

    print("\n--- median onset date per pilot district (across years) ---")
    labelled = onset.merge(
        units[["unit_id", "district", "state"]], on="unit_id", how="left"
    )
    found = labelled[labelled["onset_date"].notna()].copy()
    found["doy"] = found["onset_date"].dt.dayofyear
    for (state, district), group in found.groupby(["state", "district"], observed=True):
        median_doy = float(group["doy"].median())
        label = (pd.Timestamp("2001-01-01") +
                 pd.Timedelta(days=median_doy - 1)).strftime("%d %b")
        n_years = group["year"].nunique()
        total_years = labelled[labelled["district"] == district]["year"].nunique()
        print(f"  {state:<16} {district:<10} median {label}   "
              f"(detected in {n_years}/{total_years} years, "
              f"IQR {group['doy'].quantile(.25):.0f}-{group['doy'].quantile(.75):.0f} doy)")

    never = labelled.groupby(["state", "district"], observed=True)["onset_date"].apply(
        lambda s: int(s.isna().sum())
    )
    if never.any():
        print("\n  unit-years with NO detected onset:")
        for (state, district), count in never[never > 0].items():
            print(f"      {state}/{district}: {count}")


if __name__ == "__main__":
    raise SystemExit(main())
