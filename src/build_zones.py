"""Attach the NARP agro-climatic zone to every district and report the gaps.

Run:  python -m src.build_zones [--no-verify]

Sourcing, and why it works this way
-----------------------------------
`config/zones_narp.csv` is the reviewed artifact: one row per district, each carrying
the ICAR-CRIDA contingency-plan URL it was transcribed from. Each of those PDFs states
its own district's NARP zone *and* lists every district in that zone, so a handful of
PDFs cover far more than the pilot districts.

This module does not parse the rosters blind. Auto-parsing them is not safe: the PDFs
use three different word separators (spaces, dots, the letter V), list Uttar Pradesh's
roster by *division* rather than district, and some districts straddle two zones with
neither holding a majority. Instead the CSV is hand-transcribed and this module
*verifies* it against the sources - it re-downloads each cited PDF and asserts the
claimed zone name actually appears in it. A citation that has rotted fails the run.

District names are resolved by exact match against the unit layer only. Fuzzy matching
is used solely to *suggest* fixes for names that fail, never to accept one: on this
data rapidfuzz maps `Bhabhua` to `Banka` (50) and `Beed` to `Nanded` (60), both wrong
(CLAUDE.md section 5).

Districts with no sourced zone get zone_id = None, are listed by name, and fall back to
the `defaults` block of config/onset.yaml - a working default rather than a wrong
threshold.
"""

from __future__ import annotations

import argparse
import io
import re

import geopandas as gpd
import pandas as pd
import requests

from src.common import CONFIG_DIR, UNITS_GPKG
from src.report import DataError, print_list, require_nonempty, summarize

ZONES_CSV = CONFIG_DIR / "zones_narp.csv"
FUZZY_SUGGEST_FLOOR = 80
TIMEOUT = 120


def _alnum(text: str) -> str:
    """Collapse to uppercase alphanumerics.

    The CRIDA PDFs substitute '.' or 'V' for spaces and '0' or 'U' for hyphens
    depending on how each was typeset, so containment checks only survive if every
    non-alphanumeric character is dropped first.
    """
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def load_zone_table() -> pd.DataFrame:
    if not ZONES_CSV.is_file():
        raise DataError(f"missing zone table: {ZONES_CSV}")
    table = pd.read_csv(ZONES_CSV, dtype=str).fillna("")
    required = {"state", "district", "zone_id", "zone_name", "zone_name_in_source",
                "source_url", "retrieved"}
    missing = required - set(table.columns)
    if missing:
        raise DataError(f"{ZONES_CSV.name} is missing columns: {sorted(missing)}")
    dupes = table[table.duplicated(["state", "district"], keep=False)]
    if not dupes.empty:
        raise DataError(
            "a district is assigned more than one zone: "
            f"{dupes[['state', 'district']].to_dict('records')}"
        )
    print(f"  loaded {len(table)} district-zone rows from {ZONES_CSV.name}")
    return table


def verify_sources(table: pd.DataFrame) -> list[str]:
    """Re-download each cited PDF and confirm the claimed zone name appears in it.

    Returns the list of problems found; an empty list means every citation held.
    """
    import pypdf

    problems: list[str] = []
    urls = sorted({u for u in table["source_url"] if u})
    print(f"\n--- verifying {len(urls)} cited sources ---")

    for url in urls:
        claims = table[table["source_url"] == url]
        try:
            response = requests.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            reader = pypdf.PdfReader(io.BytesIO(response.content))
            # The profile table is on page 1, or page 2 behind a cover page.
            text = "".join(p.extract_text() or "" for p in reader.pages[:3])
        except Exception as exc:
            problems.append(f"{url}: unreachable ({type(exc).__name__}: {exc})")
            print(f"  UNREACHABLE {url.rsplit('/', 1)[-1]}")
            continue

        haystack = _alnum(text)
        if "NARP" not in haystack:
            problems.append(f"{url}: no NARP zone section found in the first 3 pages")

        # Verify against the source's own wording, not our display name: these PDFs
        # contain typos ("Maharastra", "Centeral Maharashatra", "Malawa") that the
        # clean zone_name deliberately corrects.
        for wording in sorted(set(claims["zone_name_in_source"])):
            if not wording:
                continue
            # A joint zone like "A / B" is stored as one label; check each part.
            for part in (p.strip() for p in wording.split("/")):
                if _alnum(part) not in haystack:
                    problems.append(
                        f"{url}: claimed source wording {part!r} is not in the PDF"
                    )
        ok = not any(url in p for p in problems)
        print(f"  {'OK  ' if ok else 'FAIL'} {url.rsplit('/', 1)[-1]} "
              f"({len(claims)} districts)")
    return problems


def resolve(table: pd.DataFrame, units: gpd.GeoDataFrame) -> pd.DataFrame:
    """Exact-match the zone table onto the (state, district) pairs in the unit layer."""
    actual = units[["state", "district"]].drop_duplicates()
    merged = actual.merge(
        table[["state", "district", "zone_id", "zone_name", "source_url"]],
        on=["state", "district"], how="left",
    )

    claimed = set(map(tuple, table[["state", "district"]].values))
    real = set(map(tuple, actual.values))
    unmatched = sorted(claimed - real)
    if unmatched:
        from rapidfuzz import fuzz, process

        print("\n--- zone-table districts that match no unit (NOT auto-corrected) ---")
        for state, district in unmatched:
            pool = sorted(units.loc[units["state"] == state, "district"].unique())
            suggestion = ""
            if pool:
                name, score, _ = process.extractOne(district, pool, scorer=fuzz.WRatio)
                if score >= FUZZY_SUGGEST_FLOOR:
                    suggestion = f"  did you mean {name!r}? (score {score:.0f})"
            print(f"  {state}/{district}{suggestion}")
        raise DataError(
            f"{len(unmatched)} row(s) in {ZONES_CSV.name} name a district that does not "
            "exist in the unit layer; fix the CSV rather than fuzzy-matching past it"
        )
    return merged


def report(merged: pd.DataFrame, units: gpd.GeoDataFrame) -> None:
    zoned = merged[merged["zone_id"].notna()]
    print("\n--- zone coverage by state ---")
    for state in sorted(merged["state"].unique()):
        rows = merged[merged["state"] == state]
        have = int(rows["zone_id"].notna().sum())
        n_units = int((units["state"] == state).sum())
        covered = int(units.loc[units["state"] == state, "district"].isin(
            rows.loc[rows["zone_id"].notna(), "district"]).sum())
        print(f"  {state:<16}: {have:>3}/{len(rows):<3} districts zoned, "
              f"{covered:>3}/{n_units:<3} units")

    print("\n--- zones found ---")
    for (zone_id, zone_name), rows in zoned.groupby(["zone_id", "zone_name"]):
        print(f"  {zone_id:<12} {zone_name}")
        print(f"      districts ({len(rows)}): {', '.join(sorted(rows['district']))}")

    unzoned = merged[merged["zone_id"].isna()]
    print()
    print_list(
        "districts WITHOUT a sourced zone (fall back to onset.yaml defaults)",
        [f"{r.state}/{r.district}" for r in unzoned.itertuples()],
        limit=25,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-verify", action="store_true",
                        help="skip re-downloading the cited PDFs")
    args = parser.parse_args()

    print("--- build_zones ---")
    if not UNITS_GPKG.is_file():
        raise DataError(f"missing {UNITS_GPKG}; run python -m src.build_units first")
    units = gpd.read_file(UNITS_GPKG, layer="units")
    table = load_zone_table()

    if not args.no_verify:
        problems = verify_sources(table)
        if problems:
            for problem in problems:
                print(f"  PROBLEM: {problem}")
            raise DataError(
                f"{len(problems)} source citation(s) could not be verified; the zone "
                "table must not be trusted until they are"
            )
        print("  all citations verified against their source PDFs")

    merged = resolve(table, units)
    require_nonempty(merged[merged["zone_id"].notna()], "zoned districts")
    report(merged, units)

    # Join onto the unit layer so downstream code reads zones from units.gpkg.
    units = units.drop(columns=[c for c in ("zone_id", "zone_name") if c in units])
    units = units.merge(
        merged[["state", "district", "zone_id", "zone_name"]],
        on=["state", "district"], how="left",
    )
    units.to_file(UNITS_GPKG, layer="units", driver="GPKG")

    n_zoned = int(merged["zone_id"].notna().sum())
    summarize(
        "build_zones",
        rows=len(merged),
        files=[ZONES_CSV, UNITS_GPKG],
        extra={
            "districts": len(merged),
            "zoned": n_zoned,
            "unzoned": len(merged) - n_zoned,
            "distinct zones": merged["zone_id"].nunique(),
            "units zoned": f"{int(units['zone_id'].notna().sum())}/{len(units)}",
            "sources cited": table.loc[table['source_url'] != '', 'source_url'].nunique(),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
