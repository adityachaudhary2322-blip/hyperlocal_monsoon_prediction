"""Monsoon-advance replay data for the public map: local onset date per unit per season.

Run:  python scripts/build_onset_replay.py [--years 2022 2023 2024 2025]

Onset is detected with exactly the rule the forecasts are trained on - src.onset.detect_onset
with each unit's zone -> state -> default config from config/onset.yaml (CLAUDE.md §9) -
applied to our own IMD-derived unit rainfall. Nothing here is a forecast: these are the
observed onsets of past seasons, which is what the replay claims to show.

Output app/static/onset_replay.json (committed, ~30 kB):

    {"years": [...], "window": ["05-15", "08-15"],
     "units": [unit_id, ...],                    # all 739, fixed order
     "doy": {"2024": [172, null, ...], ...}}     # day of year, null = no confirmed onset

A unit with no confirmed onset stays null rather than being given a date: criterion 2
(the false-start filter) refuses onsets that the record cannot confirm.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.build_features import load_units  # noqa: E402
from src.common import UNIT_RAIN_PARQUET  # noqa: E402
from src.onset import detect_onset, onset_config  # noqa: E402
from src.report import DataError, summarize  # noqa: E402

OUT = ROOT / "app" / "static" / "onset_replay.json"
WINDOW = ("05-15", "08-15")        # the replay's animated span, per the brief


def season_rain(year: int, unit_ids: list[str]) -> pd.DataFrame:
    """(date x unit) rainfall for one calendar year - one year in memory at a time."""
    table = pd.read_parquet(
        UNIT_RAIN_PARQUET, columns=["unit_id", "date", "rain_mm"],
        filters=[("date", ">=", pd.Timestamp(f"{year}-01-01")),
                 ("date", "<=", pd.Timestamp(f"{year}-12-31"))],
    )
    if table.empty:
        raise DataError(f"no unit rainfall for {year}")
    wide = table.pivot(index="date", columns="unit_id", values="rain_mm")
    full = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    return wide.reindex(index=full, columns=unit_ids).astype("float32")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--years", type=int, nargs="+", default=[2022, 2023, 2024, 2025])
    args = parser.parse_args()
    print("--- build_onset_replay ---")

    units = load_units(all_units=True)
    unit_ids = units["unit_id"].tolist()
    cfgs = {r.unit_id: onset_config(zone_id=r.zone_id, state=r.state)
            for r in units.itertuples()}
    doy: dict[str, list[int | None]] = {}
    per_state: dict[str, dict[int, int]] = {}
    for year in args.years:
        rain = season_rain(year, unit_ids)
        values: list[int | None] = []
        for unit_id in unit_ids:
            series = rain[unit_id]
            if series.isna().all():
                values.append(None)
                continue
            onset = detect_onset(series.to_numpy(dtype="float64"),
                                 pd.DatetimeIndex(series.index), cfgs[unit_id])
            values.append(None if onset is None else int(onset.dayofyear))
        found = sum(v is not None for v in values)
        if not found:
            raise DataError(f"{year}: no onset detected in any unit")
        doy[str(year)] = values
        onset_doys = [v for v in values if v is not None]
        print(f"  {year}: onset in {found}/{len(values)} units, median day "
              f"{int(np.median(onset_doys))} "
              f"({(pd.Timestamp(f'{year}-01-01') + pd.Timedelta(days=int(np.median(onset_doys)) - 1)):%d %b})")
        for state, ok in zip(units["state"], values):
            per_state.setdefault(state, {}).setdefault(year, 0)
            per_state[state][year] += ok is not None

    payload = {"years": args.years, "window": list(WINDOW), "units": unit_ids, "doy": doy,
               "source": "src.onset.detect_onset on IMD gridded rainfall (unit means)"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    print("  units with a confirmed onset, per state:")
    counts = units["state"].value_counts()
    for state, by_year in per_state.items():
        print(f"    {state:<16} " + "  ".join(f"{y}: {n}/{counts[state]}"
                                                for y, n in by_year.items()))
    summarize("build_onset_replay", rows=len(unit_ids) * len(args.years), files=[OUT],
              date_range=(f"{min(args.years)}-{WINDOW[0]}", f"{max(args.years)}-{WINDOW[1]}"),
              extra={"size": f"{OUT.stat().st_size / 1e3:.0f} kB"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
