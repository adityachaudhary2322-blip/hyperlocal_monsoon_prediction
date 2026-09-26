"""Fetch ENSO / IOD / MJO indices onto a daily grid.

Run:  python -m src.fetch_indices [--start 1990] [--force]

Sources (all recorded in data/SOURCES.md):
  ONI          NOAA CPC, 3-month centred seasonal values
  Nino3.4      NOAA CPC, monthly anomaly
  IOD DMI      NOAA PSL, monthly, HadISST-based
  MJO RMM      Australian BoM RMM series, served by the IRI Data Library

Why BoM is read through IRI: bom.gov.au blocks automated access to
rmm.74toRealtime.txt outright. IRI serves the same BoM RMM1/RMM2/phase/amplitude
series over DAP, daily and gapless.

Staleness
---------
Monthly indices are forward-filled to daily as required, but they lag badly and by
different amounts - as of Sept 2026 MJO was current to within days while the PSL DMI
ended in May. A plain forward-fill would present a four-month-old IOD value as today's
state, so every index also gets an `<index>_age_days` column: the age of the underlying
observation on that date. Models and reports can then see staleness instead of
inferring it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io

import numpy as np
import pandas as pd
import requests
import xarray as xr

from src.common import INDICES_PARQUET, SOURCES_MD
from src.report import DataError, require_nonempty, summarize

TIMEOUT = 120
MISSING = -9999.0

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
NINO34_URL = ("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
              "ensostuff/detrend.nino34.ascii.txt")
DMI_URL = "https://psl.noaa.gov/gcos_wgsp/Timeseries/Data/dmi.had.long.data"
RMM_BASE = "https://iridl.ldeo.columbia.edu/SOURCES/.BoM/.MJO/.RMM"
RMM_VARS = ("RMM1", "RMM2", "phase", "amplitude")

# CPC labels each 3-month season by its three initials; the value is centred on the
# middle month, so DJF 1950 is the January 1950 value.
SEASON_CENTRE = {
    "DJF": 1, "JFM": 2, "FMA": 3, "MAM": 4, "AMJ": 5, "MJJ": 6,
    "JJA": 7, "JAS": 8, "ASO": 9, "SON": 10, "OND": 11, "NDJ": 12,
}


def _get(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.text.strip():
        raise DataError(f"{url} returned an empty body")
    return response.text


def fetch_oni() -> pd.Series:
    """Monthly ONI, indexed by the centre month of each 3-month season."""
    text = _get(ONI_URL)
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) != 4 or parts[0] not in SEASON_CENTRE:
            continue
        season, year, _total, anom = parts
        rows.append((pd.Timestamp(int(year), SEASON_CENTRE[season], 1), float(anom)))
    if not rows:
        raise DataError(f"parsed no ONI rows from {ONI_URL}")
    series = pd.Series(dict(rows), name="oni").sort_index()
    print(f"  oni      : {len(series):>5} months, {series.index[0]:%Y-%m} -> "
          f"{series.index[-1]:%Y-%m}")
    return series


def fetch_nino34() -> pd.Series:
    """Monthly Nino3.4 anomaly (the ANOM column of the CPC detrended file)."""
    text = _get(NINO34_URL)
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) != 5:
            continue
        try:
            year, month, _total, _clim, anom = (
                int(parts[0]), int(parts[1]), *map(float, parts[2:])
            )
        except ValueError:
            continue
        rows.append((pd.Timestamp(year, month, 1), anom))
    if not rows:
        raise DataError(f"parsed no Nino3.4 rows from {NINO34_URL}")
    series = pd.Series(dict(rows), name="nino34").sort_index()
    print(f"  nino34   : {len(series):>5} months, {series.index[0]:%Y-%m} -> "
          f"{series.index[-1]:%Y-%m}")
    return series


def fetch_dmi() -> pd.Series:
    """Monthly IOD Dipole Mode Index. The PSL file uses -9999 for missing months."""
    text = _get(DMI_URL)
    lines = text.splitlines()
    header = lines[0].split()
    if len(header) != 2:
        raise DataError(f"unexpected DMI header: {lines[0]!r}")
    rows = {}
    for line in lines[1:]:
        parts = line.split()
        if len(parts) != 13:
            continue  # trailer lines: the -9999 sentinel, provenance text
        try:
            year = int(parts[0])
            values = [float(v) for v in parts[1:]]
        except ValueError:
            continue
        for month, value in enumerate(values, start=1):
            rows[pd.Timestamp(year, month, 1)] = np.nan if value == MISSING else value
    if not rows:
        raise DataError(f"parsed no DMI rows from {DMI_URL}")
    series = pd.Series(rows, name="dmi").sort_index().dropna()
    print(f"  dmi      : {len(series):>5} months, {series.index[0]:%Y-%m} -> "
          f"{series.index[-1]:%Y-%m}")
    return series


def fetch_rmm() -> pd.DataFrame:
    """Daily MJO RMM1, RMM2, phase and amplitude from the IRI Data Library.

    The IRI T axis is in julian_day, which xarray will not decode on its own.
    """
    frames = []
    for var in RMM_VARS:
        url = f"{RMM_BASE}/.{var}/dods"
        with xr.open_dataset(url, decode_times=False) as ds:
            if ds["T"].attrs.get("units") != "julian_day":
                raise DataError(
                    f"{url}: expected T in julian_day, got "
                    f"{ds['T'].attrs.get('units')!r}"
                )
            dates = pd.to_datetime(ds["T"].values, unit="D", origin="julian")
            values = ds[var].values.astype("float64")
        # IRI encodes missing as ~1e36 rather than NaN.
        values = np.where(np.abs(values) > 1e30, np.nan, values)
        frames.append(pd.Series(values, index=dates.normalize(), name=var.lower()))

    rmm = pd.concat(frames, axis=1).sort_index()
    rmm["phase"] = rmm["phase"].round().astype("Int64")
    print(f"  mjo rmm  : {len(rmm):>5} days,   {rmm.index[0]:%Y-%m-%d} -> "
          f"{rmm.index[-1]:%Y-%m-%d}")
    return rmm


def to_daily(series: pd.Series, days: pd.DatetimeIndex,
             *, months_until_published: int) -> pd.DataFrame:
    """Forward-fill a monthly series onto `days` without looking ahead.

    A monthly value is *dated* by the month it describes but is not *knowable* until
    that month is over and the provider has published it. Stamping June's mean at
    1 June and reading it on 20 June would leak the rest of June into a feature, so
    each observation is carried forward only from its publication date:

        publication = first day of (observation month + months_until_published)

    `months_until_published=1` suits a plain monthly mean (June is publishable once
    June ends). ONI needs 2: the value centred on June is a mean over May-July, so it
    is not complete until July ends.

    `age_days` is measured from the month the value *describes*, which is what "how
    old is this information" means to a forecaster. A date before the first
    publication gets NaN, never the first value.
    """
    published = series.index + pd.DateOffset(months=months_until_published)
    published = pd.DatetimeIndex(published)

    by_publication = pd.Series(series.to_numpy(), index=published, name=series.name)
    by_publication = by_publication[~by_publication.index.duplicated(keep="last")]
    filled = (by_publication.reindex(by_publication.index.union(days))
              .sort_index().ffill().reindex(days))

    # Age is relative to the observation's own month, not its publication date.
    nominal = pd.Series(series.index, index=published)
    nominal = nominal[~nominal.index.duplicated(keep="last")]
    last_nominal = (nominal.reindex(nominal.index.union(days))
                    .sort_index().ffill().reindex(days))

    age = (days - pd.DatetimeIndex(last_nominal.values)).days
    return pd.DataFrame(
        {series.name: filled.to_numpy(),
         f"{series.name}_age_days": np.asarray(age, dtype="float64")},
        index=days,
    )


def write_sources(meta: dict[str, dict]) -> None:
    today = dt.date.today().isoformat()
    lines = [
        "# Data sources",
        "",
        f"Generated by `python -m src.fetch_indices` on {today}. Every external",
        "dataset used by this project is recorded here with its access date.",
        "",
        "## Climate indices",
        "",
        "| index | provider | URL | series | coverage retrieved |",
        "|---|---|---|---|---|",
    ]
    for name, info in meta.items():
        lines.append(
            f"| {name} | {info['provider']} | <{info['url']}> | {info['cadence']} | "
            f"{info['coverage']} |"
        )
    lines += [
        "",
        "### Notes and citations",
        "",
        "- **ONI / Nino3.4** - NOAA Climate Prediction Center. ONI is a 3-month",
        "  running mean of Nino3.4 SST anomalies (ERSSTv5); CPC labels each value by",
        "  its season initials, and this pipeline maps that label to the season's",
        "  centre month (`DJF 1950` -> January 1950).",
        "- **IOD DMI** - NOAA Physical Sciences Laboratory, computed from HadISST 1.1",
        "  as the SST anomaly difference between 10S-10N, 50E-70E and 10S-0, 90E-110E.",
        "  Missing months are coded `-9999`. This series lags real time by several",
        "  months; see the `dmi_age_days` column.",
        "- **MJO RMM** - Wheeler & Hendon (2004) Real-time Multivariate MJO index,",
        "  produced by the Australian Bureau of Meteorology. bom.gov.au blocks",
        "  automated access to the text file, so the identical BoM series is read from",
        "  the IRI Data Library (Columbia University) over DAP.",
        "  Wheeler, M. C. and H. H. Hendon (2004), *An All-Season Real-Time",
        "  Multivariate MJO Index*, Mon. Wea. Rev., 132, 1917-1932.",
        "",
        "## Rainfall",
        "",
        "| dataset | provider | URL | resolution |",
        "|---|---|---|---|",
        "| Daily gridded rainfall (archive) | India Meteorological Department, Pune | "
        "<https://imdpune.gov.in/cmpg/Griddata/rainfall.php> | 0.25 deg, 1901- |",
        "| Daily gridded rainfall (realtime) | India Meteorological Department, Pune | "
        "<https://imdpune.gov.in/cmpg/Realtimedata/Rainfall/rain.php> | 0.25 deg, "
        "current year |",
        "",
        "## Boundaries",
        "",
        "| dataset | provider | URL |",
        "|---|---|---|",
        "| GADM 4.1 India levels 2 and 3 | GADM | <https://gadm.org/download_country.html> |",
        "",
        "GADM data are free for academic and other non-commercial use;",
        "redistribution is not allowed. See <https://gadm.org/license.html>.",
        "",
        "## Agro-climatic zones",
        "",
        "District NARP zone assignments are transcribed from ICAR-CRIDA district",
        "Agriculture Contingency Plans; the exact source URL for each district is in",
        "`config/zones_narp.csv`, and `python -m src.build_zones` re-verifies every",
        "citation against its PDF. Index: <https://www.icar-crida.res.in/Crop_Contingency_Plan.html>",
        "",
    ]
    SOURCES_MD.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1990)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if INDICES_PARQUET.is_file() and not args.force:
        print(f"{INDICES_PARQUET} exists; pass --force to rebuild")
        return 0

    print("--- fetch_indices ---")
    oni = fetch_oni()
    nino34 = fetch_nino34()
    dmi = fetch_dmi()
    rmm = fetch_rmm()

    end = max(oni.index[-1], nino34.index[-1], dmi.index[-1], rmm.index[-1])
    end = max(end, pd.Timestamp(dt.date.today()))
    days = pd.date_range(f"{args.start}-01-01", end, freq="D", name="date")

    frame = pd.concat(
        [
            # ONI centred on month M spans M-1..M+1, so it is only complete once
            # M+1 has ended; a plain monthly mean is publishable once M has ended.
            to_daily(oni, days, months_until_published=2),
            to_daily(nino34, days, months_until_published=1),
            to_daily(dmi, days, months_until_published=1),
        ],
        axis=1,
    )

    # MJO is already daily, and BoM publishes with a one-day lag, so day D is only
    # readable from D+1. Shifting by a day then forward-filling keeps genuine gaps
    # visible without interpolating across them.
    rmm_published = rmm.copy()
    rmm_published.index = rmm_published.index + pd.Timedelta(days=1)
    rmm_daily = (rmm_published.reindex(rmm_published.index.union(days))
                 .sort_index().ffill().reindex(days))
    rmm_daily["phase"] = rmm_daily["phase"].astype("Float64").round().astype("Int64")
    frame = frame.join(rmm_daily)

    nominal = pd.Series(rmm.index, index=rmm_published.index)
    last_nominal = (nominal.reindex(nominal.index.union(days))
                    .sort_index().ffill().reindex(days))
    frame["mjo_age_days"] = (days - pd.DatetimeIndex(last_nominal.values)).days

    for column in frame.columns:
        if column == "phase":
            continue
        frame[column] = frame[column].astype("float32")
    frame["phase"] = frame["phase"].astype("Int8")

    frame = frame.reset_index()
    require_nonempty(frame, "daily indices")

    coverage = {}
    for name, series, cadence, provider, url in [
        ("ONI", oni, "monthly (3-month centred)", "NOAA CPC", ONI_URL),
        ("Nino3.4", nino34, "monthly", "NOAA CPC", NINO34_URL),
        ("IOD DMI", dmi, "monthly", "NOAA PSL", DMI_URL),
        ("MJO RMM1/RMM2/phase/amplitude", rmm, "daily",
         "Australian BoM via IRI Data Library", f"{RMM_BASE}/"),
    ]:
        coverage[name] = {
            "provider": provider,
            "url": url,
            "cadence": cadence,
            "coverage": f"{series.index[0]:%Y-%m-%d} to {series.index[-1]:%Y-%m-%d}",
        }
    write_sources(coverage)

    INDICES_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(INDICES_PARQUET, index=False)
    require_nonempty(INDICES_PARQUET, "indices.parquet")

    today = pd.Timestamp(dt.date.today())
    tail = frame[frame["date"] == today]
    staleness = {}
    if not tail.empty:
        row = tail.iloc[0]
        for name in ("oni", "nino34", "dmi", "mjo"):
            staleness[f"{name} age today"] = f"{row[f'{name}_age_days']:.0f} days"

    summarize(
        "fetch_indices",
        rows=len(frame),
        files=[INDICES_PARQUET, SOURCES_MD],
        date_range=(f"{frame['date'].iloc[0]:%Y-%m-%d}",
                    f"{frame['date'].iloc[-1]:%Y-%m-%d}"),
        extra={
            "columns": len(frame.columns),
            "oni latest": f"{oni.index[-1]:%Y-%m}",
            "nino34 latest": f"{nino34.index[-1]:%Y-%m}",
            "dmi latest": f"{dmi.index[-1]:%Y-%m}",
            "mjo latest": f"{rmm.index[-1]:%Y-%m-%d}",
            **staleness,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
