"""Phase 1 data-quality report: outputs/data_quality.md plus its figures.

Run:  python -m src.data_quality [--force]

Every number here is descriptive of the *whole* record and is labelled with the years
it covers. The 1990-2018 training-only climatology (CLAUDE.md section 11) belongs to
the modelling phase, so nothing in this report is used to fit anything.
"""

from __future__ import annotations

import argparse
import datetime as dt

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from matplotlib.colors import PowerNorm
from matplotlib.cm import ScalarMappable

from src.common import (
    FIGS_DIR,
    INDICES_PARQUET,
    OUTPUTS_DIR,
    RAIN_GRID_NC,
    UNITS_GPKG,
    UNIT_RAIN_PARQUET,
    pilot_districts,
    splits,
)
from src.report import DataError, require_nonempty, summarize
from src.viz import NO_DATA, RAIN_CMAP, SERIES, TEXT_MUTED, apply_style, recessive_grid

REPORT_MD = OUTPUTS_DIR / "data_quality.md"
CHOROPLETH = FIGS_DIR / "jjas_mean_rainfall.png"
PILOT_TRENDS = FIGS_DIR / "pilot_jjas_trends.png"
JJAS = (6, 7, 8, 9)


def load() -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    for path in (UNIT_RAIN_PARQUET, UNITS_GPKG):
        if not path.exists():
            raise DataError(f"missing {path}; run the Phase 1 pipeline first")
    rain = pd.read_parquet(
        UNIT_RAIN_PARQUET, columns=["unit_id", "date", "rain_mm", "small_unit"]
    )
    units = gpd.read_file(UNITS_GPKG, layer="units")
    return rain, units


def missing_days(rain: pd.DataFrame, units: gpd.GeoDataFrame) -> pd.DataFrame:
    per_unit = rain.groupby("unit_id", observed=True).agg(
        days=("rain_mm", "size"),
        missing=("rain_mm", lambda s: int(s.isna().sum())),
    )
    per_unit["missing_pct"] = 100 * per_unit["missing"] / per_unit["days"]
    return units.drop(columns="geometry").merge(
        per_unit, left_on="unit_id", right_index=True, how="left"
    )


def jjas_by_unit(rain: pd.DataFrame) -> pd.Series:
    """Mean JJAS total per unit across all years present."""
    jjas = rain[rain["date"].dt.month.isin(JJAS)].copy()
    jjas["year"] = jjas["date"].dt.year
    per_year = jjas.groupby(["unit_id", "year"], observed=True)["rain_mm"].sum(
        min_count=1
    )
    return per_year.groupby("unit_id").mean()


def draw_choropleth(units: gpd.GeoDataFrame, mean_jjas: pd.Series,
                    years: tuple[int, int]) -> None:
    apply_style()
    gdf = units.merge(mean_jjas.rename("jjas_mm"), left_on="unit_id",
                      right_index=True, how="left")

    fig, ax = plt.subplots(figsize=(8.2, 9.0))
    values = gdf["jjas_mm"]
    # Sequential magnitude: one hue, light to dark. Clip the top to the 98th
    # percentile so the Western Ghats extreme does not flatten the interior.
    vmax = float(np.nanpercentile(values, 98))
    vmin = float(np.nanmin(values))
    # Most units sit between 600 and 1500 mm while the Western Ghats reach ~4500, so
    # a linear ramp leaves the whole interior in the two palest steps. A power norm
    # keeps one hue light-to-dark but spends more of it where the data actually is.
    norm = PowerNorm(gamma=0.55, vmin=vmin, vmax=vmax)

    # Only draw the no-data layer when there is one: plotting an empty GeoDataFrame
    # makes geopandas derive the aspect ratio from NaN and raise.
    blank = gdf[values.isna()]
    if not blank.empty:
        blank.plot(ax=ax, color=NO_DATA, edgecolor="white", linewidth=0.15)
    gdf[values.notna()].plot(
        ax=ax, column="jjas_mm", cmap=RAIN_CMAP, norm=norm,
        edgecolor="white", linewidth=0.15,
    )
    # State outlines, so the sub-district mosaic stays readable.
    units.dissolve("state").boundary.plot(ax=ax, color="white", linewidth=1.1)
    units.dissolve("state").boundary.plot(ax=ax, color=TEXT_MUTED, linewidth=0.5)

    ax.set_title(
        f"Mean June-September rainfall by sub-district, {years[0]}-{years[1]}",
        loc="left", pad=12,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.set_axis_off()

    bar = fig.colorbar(
        ScalarMappable(norm=norm, cmap=RAIN_CMAP), ax=ax,
        orientation="horizontal", fraction=0.035, pad=0.02, aspect=36,
    )
    bar.set_label("mm per JJAS season", color=TEXT_MUTED)
    bar.outline.set_visible(False)
    bar.ax.tick_params(length=0, labelsize=8)
    top = bar.ax.get_xticklabels()
    if top:
        top[-1].set_text(f"{vmax:.0f}+")
    note = (f"Scale clipped at the 98th percentile ({vmax:.0f} mm), "
            f"power-scaled (gamma 0.55) so the 600-1500 mm interior is readable.")
    if not blank.empty:
        note = f"Grey = no IMD grid cell with observations. {note}"
    fig.text(0.01, 0.015, note, color=TEXT_MUTED, fontsize=7.5)

    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(CHOROPLETH, bbox_inches="tight")
    plt.close(fig)


def draw_pilot_trends(rain: pd.DataFrame, units: gpd.GeoDataFrame) -> pd.DataFrame:
    """Small multiples: one panel per pilot district.

    Eight lines on a single axis would be spaghetti and would need eight hues that
    separate against each other; faceting needs only one.
    """
    apply_style()
    pilots = pilot_districts()
    pairs = [(state, district) for state, ds in pilots.items() for district in ds]

    labelled = rain.merge(units[["unit_id", "state", "district", "area_km2"]],
                          on="unit_id", how="left")
    jjas = labelled[labelled["date"].dt.month.isin(JJAS)].copy()
    jjas["year"] = jjas["date"].dt.year

    # District = area-weighted mean of its sub-districts. Two steps, deliberately:
    # each unit's seasonal TOTAL first, then the area weighting across units. Doing
    # it in one groupby would divide by the area summed over every day of the season
    # (122x too much), which silently scales the answer down by two orders of
    # magnitude while still looking like a plausible curve.
    per_unit_year = jjas.groupby(
        ["state", "district", "unit_id", "year"], observed=True
    ).agg(total_mm=("rain_mm", "sum"), area_km2=("area_km2", "first")).reset_index()

    per_unit_year["weighted"] = per_unit_year["total_mm"] * per_unit_year["area_km2"]
    grouped = per_unit_year.groupby(["state", "district", "year"], observed=True)
    totals = (grouped["weighted"].sum(min_count=1) /
              grouped["area_km2"].sum()).rename("jjas_mm").reset_index()

    fig, axes = plt.subplots(4, 2, figsize=(9.4, 10.2), sharex=True)
    for ax, (state, district) in zip(axes.ravel(), pairs):
        series = totals[(totals["state"] == state) &
                        (totals["district"] == district)].sort_values("year")
        recessive_grid(ax)
        if series.empty:
            ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center",
                    color=TEXT_MUTED)
            ax.set_title(f"{district}, {state}", loc="left")
            continue
        median = series["jjas_mm"].median()
        ax.axhline(median, color=TEXT_MUTED, linewidth=1.0, linestyle=(0, (4, 3)),
                   zorder=1)
        ax.plot(series["year"], series["jjas_mm"], color=SERIES[0], zorder=3)
        # The median goes in the title, not on the plot: an in-plot label sits on
        # top of the line in half these panels whichever edge it is anchored to.
        ax.set_title(f"{district}, {state}   |   median {median:.0f} mm", loc="left")
        ax.set_ylim(0, max(series["jjas_mm"].max() * 1.15, median * 1.6))

    for ax in axes[-1]:
        ax.set_xlabel("year")
    for ax in axes[:, 0]:
        ax.set_ylabel("JJAS total (mm)")

    fig.suptitle("Yearly June-September rainfall, pilot districts",
                 x=0.01, ha="left", fontsize=11, fontweight="600")
    fig.text(0.01, 0.005,
             "Area-weighted mean of each district's sub-district units. "
             "Dashed line = that district's median across years.",
             color=TEXT_MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.015, 1, 0.985))
    FIGS_DIR.mkdir(parents=True, exist_ok=True)
    fig.savefig(PILOT_TRENDS, bbox_inches="tight")
    plt.close(fig)

    # Return only the pilots. `totals` covers every district because it is computed
    # from the whole unit table; the figure filters per panel, the report does not.
    keep = pd.MultiIndex.from_tuples(pairs)
    return totals[
        pd.MultiIndex.from_frame(totals[["state", "district"]]).isin(keep)
    ].reset_index(drop=True)


def index_coverage() -> pd.DataFrame | None:
    if not INDICES_PARQUET.exists():
        return None
    indices = pd.read_parquet(INDICES_PARQUET)
    today = pd.Timestamp(dt.date.today())
    rows = []
    for name, column in [("ONI", "oni"), ("Nino3.4", "nino34"), ("IOD DMI", "dmi"),
                         ("MJO RMM1", "rmm1"), ("MJO RMM2", "rmm2"),
                         ("MJO amplitude", "amplitude")]:
        if column not in indices:
            continue
        valid = indices.loc[indices[column].notna(), "date"]
        age_col = {"oni": "oni_age_days", "nino34": "nino34_age_days",
                   "dmi": "dmi_age_days"}.get(column, "mjo_age_days")
        latest = indices.loc[indices["date"] <= today, age_col].dropna()
        rows.append({
            "index": name,
            "first": valid.min().date() if len(valid) else None,
            "last": valid.max().date() if len(valid) else None,
            "missing_days": int(indices[column].isna().sum()),
            "age_today_days": int(latest.iloc[-1]) if len(latest) else None,
        })
    return pd.DataFrame(rows)


def write_report(*, rain: pd.DataFrame, units: gpd.GeoDataFrame,
                 gaps: pd.DataFrame, mean_jjas: pd.Series,
                 pilot_totals: pd.DataFrame, indices: pd.DataFrame | None,
                 years: tuple[int, int]) -> None:
    train_lo, train_hi = splits()["train"]
    val = splits()["validate"]
    test = splits()["test"]
    last_test = years[1]

    state_jjas = (units.drop(columns="geometry")
                  .merge(mean_jjas.rename("jjas_mm"), left_on="unit_id",
                         right_index=True, how="left")
                  .groupby("state")["jjas_mm"]
                  .agg(["mean", "min", "max", "count"])
                  .sort_values("mean", ascending=False))

    worst = gaps.nlargest(12, "missing_pct")
    flagged = gaps[(gaps["missing_pct"] > 0) | gaps["name_is_placeholder"]]

    lines = [
        "# Phase 1 data-quality report",
        "",
        f"Generated by `python -m src.data_quality` on {dt.date.today().isoformat()}.",
        f"Rainfall record **{years[0]}-{years[1]}**, "
        f"{len(units)} sub-district units across {units['state'].nunique()} regions.",
        "",
        "Splits (no leakage): train "
        f"`{train_lo}-{train_hi}`, validate `{val[0]}-{val[1]}`, "
        f"test `{test[0]}-{last_test}`. Nothing in this report is fitted; the",
        "training-only climatology is computed in the modelling phase.",
        "",
        "## Coverage and missing days",
        "",
        f"- Units: **{len(units)}**",
        f"- Days per unit: **{int(gaps['days'].max()):,}**",
        f"- Units with at least one missing day: **{int((gaps['missing'] > 0).sum())}**",
        f"- Units with no observed grid cell at all: "
        f"**{int((gaps['missing'] == gaps['days']).sum())}**",
        f"- Units smaller than one grid cell (`small_unit`): "
        f"**{int(rain.groupby('unit_id', observed=True)['small_unit'].first().sum())}**",
        f"- Units with a placeholder GADM name: "
        f"**{int(gaps['name_is_placeholder'].sum())}**",
        "",
    ]

    if not worst.empty and worst["missing_pct"].max() > 0:
        lines += [
            "### Units with the most missing days",
            "",
            "| state | district | unit | area km2 | missing days | % |",
            "|---|---|---|---:|---:|---:|",
        ]
        for row in worst.itertuples():
            if row.missing == 0:
                continue
            lines.append(
                f"| {row.state} | {row.district} | {row.unit_name} | "
                f"{row.area_km2:.2f} | {row.missing:,} | {row.missing_pct:.1f}% |"
            )
        lines.append("")
        lines += [
            "These are units whose overlapping IMD cells carry no observations - "
            "small coastal polygons, mostly. They are flagged, never filled with a "
            "guess, and their rainfall is NaN rather than 0.",
            "",
        ]
    else:
        lines += ["No unit has a missing day.", ""]

    lines += [
        "## Mean June-September rainfall",
        "",
        f"![Mean JJAS rainfall by sub-district]({CHOROPLETH.relative_to(OUTPUTS_DIR).as_posix()})",
        "",
        "### Per-state mean JJAS rainfall",
        "",
        "| state | units | mean mm | driest unit | wettest unit |",
        "|---|---:|---:|---:|---:|",
    ]
    for state, row in state_jjas.iterrows():
        lines.append(
            f"| {state} | {int(row['count'])} | {row['mean']:.0f} | "
            f"{row['min']:.0f} | {row['max']:.0f} |"
        )
    lines += [
        "",
        "## Pilot districts",
        "",
        f"![Yearly JJAS totals, pilot districts]({PILOT_TRENDS.relative_to(OUTPUTS_DIR).as_posix()})",
        "",
        "| state | district | median JJAS mm | min | max | years |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (state, district), group in pilot_totals.groupby(["state", "district"],
                                                        observed=True):
        lines.append(
            f"| {state} | {district} | {group['jjas_mm'].median():.0f} | "
            f"{group['jjas_mm'].min():.0f} | {group['jjas_mm'].max():.0f} | "
            f"{group['year'].nunique()} |"
        )

    if indices is not None:
        lines += [
            "",
            "## Climate index coverage and staleness",
            "",
            "Monthly indices are forward-filled to daily, so each carries an",
            "`age_days` column. The ages below are why that matters: a plain",
            "forward-fill would present a months-old ENSO or IOD value as today's.",
            "",
            "| index | first | last | missing days | age today |",
            "|---|---|---|---:|---:|",
        ]
        for row in indices.itertuples():
            age = f"{row.age_today_days} d" if row.age_today_days is not None else "n/a"
            lines.append(
                f"| {row.index} | {row.first} | {row.last} | "
                f"{row.missing_days:,} | {age} |"
            )
        lines.append("")

    lines += [
        "",
        "## Known limitations",
        "",
        "- **GADM level 3 is not uniformly sub-district.** Bihar has 53 level-3 "
        "polygons for 38 districts and Delhi has 1, so Gaya and Purnia have no "
        "sub-district resolution until block polygons are supplied.",
        "- **25 units have placeholder GADM names** (`n.a. ( 1681 )`). They are real "
        "polygons and are kept, but must never be named in an advisory.",
        "- **149 of 201 districts have no sourced NARP zone** and fall back to the "
        "documented default onset rule. See `config/zones_narp.csv`.",
        "- **The PSL IOD DMI lags several months.** See the staleness table above.",
        "",
        "Sources for every dataset: [`data/SOURCES.md`](../data/SOURCES.md).",
        "",
    ]

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.parse_args()

    print("--- data_quality ---")
    rain, units = load()
    require_nonempty(rain, "unit rainfall")

    years = (int(rain["date"].dt.year.min()), int(rain["date"].dt.year.max()))
    gaps = missing_days(rain, units)
    mean_jjas = jjas_by_unit(rain)
    require_nonempty(mean_jjas.dropna(), "mean JJAS rainfall")

    print(f"  {len(units)} units, {years[0]}-{years[1]}")
    print(f"  units with missing days: {int((gaps['missing'] > 0).sum())}")
    print("  drawing choropleth...")
    draw_choropleth(units, mean_jjas, years)
    print("  drawing pilot trends...")
    pilot_totals = draw_pilot_trends(rain, units)
    indices = index_coverage()

    write_report(rain=rain, units=units, gaps=gaps, mean_jjas=mean_jjas,
                 pilot_totals=pilot_totals, indices=indices, years=years)
    require_nonempty(REPORT_MD, "data_quality.md")

    summarize(
        "data_quality",
        rows=len(gaps),
        files=[REPORT_MD, CHOROPLETH, PILOT_TRENDS],
        date_range=(f"{rain['date'].min():%Y-%m-%d}", f"{rain['date'].max():%Y-%m-%d}"),
        extra={
            "units": len(units),
            "units w/ gaps": int((gaps["missing"] > 0).sum()),
            "mean JJAS": f"{mean_jjas.mean():.0f} mm",
            "driest unit": f"{mean_jjas.min():.0f} mm",
            "wettest unit": f"{mean_jjas.max():.0f} mm",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
