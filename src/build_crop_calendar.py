"""Crop calendar per state from the DES "Crop Calendar of Major Crops" (Appendix IV).

Run:  python -m src.build_crop_calendar

Source: Directorate of Economics & Statistics, Ministry of Agriculture and Farmers
Welfare - https://desagri.gov.in/document-report/4-crop-calendar-of-major-crops/
(file https://desagri.gov.in/wp-content/uploads/2021/04/Appendix-IV-1.xls, saved to
data/raw/crops/des_crop_calendar_appendix_IV.xls).

What is SOURCED: the sowing and harvesting windows per state, crop and season, exactly
as DES gives them (B/M/E = beginning / middle / end of the month; "L" = late). The
verbatim cell text is kept next to every parsed window so a parsing mistake is visible.

What is DERIVED: vegetative, flowering and maturity windows. DES gives only sowing and
harvesting, so the gap between them is split by the per-crop fractions in
config/crop_stages.yaml. Those windows are written with `basis: derived`.

The sheets differ in layout and header spelling, so the column meaning is spelled out in
SHEETS below rather than guessed from the headers.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re

import pandas as pd
import yaml
from rapidfuzz import fuzz, process

from src.common import BOUNDARIES_RAW, CONFIG_DIR, DATA_RAW, load_config
from src.report import DataError, summarize

SOURCE_FILE = DATA_RAW / "crops" / "des_crop_calendar_appendix_IV.xls"
OUT = CONFIG_DIR / "crop_calendar.yaml"
SOURCE = {
    "name": "DES, Ministry of Agriculture & Farmers Welfare - Crop Calendar of Major Crops "
            "(Appendix IV)",
    "url": "https://desagri.gov.in/document-report/4-crop-calendar-of-major-crops/",
    "file": "https://desagri.gov.in/wp-content/uploads/2021/04/Appendix-IV-1.xls",
    "upstream": "Indian Council of Agricultural Research (Crop Science Division), as "
                "credited in the DES sheets",
}

# sheet -> {column index: (crop, season)}; layout "rows" = a Sowing row then a
# Harvesting row per state; "pair" = sowing and harvesting in columns of one row.
SHEETS: dict[str, dict] = {
    "Sheet1": {"layout": "rows", "cols": {2: ("paddy", "kharif"), 3: ("paddy", "rabi"),
                                          4: ("paddy", "summer"), 5: ("bajra", "kharif"),
                                          6: ("bajra", "summer"), 7: ("wheat", "rabi")}},
    "Sheet2": {"layout": "rows", "cols": {2: ("tur", "early_kharif"), 3: ("tur", "kharif")}},
    "Sheet3": {"layout": "rows", "cols": {2: ("moong", "kharif"), 3: ("moong", "rabi"),
                                          4: ("moong", "summer"),
                                          # DES gives one column for "Mungbean/Urdbean"
                                          102: ("urad", "kharif"), 103: ("urad", "rabi"),
                                          104: ("urad", "summer")}},
    "Sheet4": {"layout": "rows", "cols": {2: ("soybean", "kharif")}},
    "Sheet5": {"layout": "rows", "cols": {2: ("gram", "rabi")}},
    "Sheet6": {"layout": "rows", "cols": {2: ("groundnut", "kharif"), 3: ("groundnut", "rabi"),
                                          4: ("groundnut", "summer")}},
    "Sheet10": {"layout": "pair", "crop": ("mustard", "rabi"), "sow": 2, "harvest": 3},
    "Sheet11": {"layout": "rows", "cols": {2: ("cotton", "kharif"), 3: ("maize", "kharif"),
                                           4: ("maize", "rabi")}},
    "Sheet12": {"layout": "rows", "cols": {2: ("sugarcane", "kharif"),
                                           3: ("sugarcane", "rabi")}},
}
# DES spellings -> GADM NAME_1. Anything else must match a GADM name above 90.
STATE_ALIASES = {"orissa": "Odisha", "m.p.": "Madhya Pradesh", "mp": "Madhya Pradesh",
                 "uttaranchal": "Uttarakhand", "chattisgadh": "Chhattisgarh",
                 "chhattisgarh": "Chhattisgarh", "jammu & kashmir": "Jammu and Kashmir",
                 "j&k": "Jammu and Kashmir", "delhi": "NCT of Delhi",
                 "pondicherry": "Puducherry", "tamilnadu": "Tamil Nadu",
                 "w.bengal": "West Bengal"}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTHS.update({"sept": 9, "june": 6, "july": 7})


# --------------------------------------------------------------------------
# Parsing "Jun(B)-Jul(M)", "May-June", "Nov (L)- Dec (E)", "Feb(M)Apr(E)"
# --------------------------------------------------------------------------
TOKEN = re.compile(r"([A-Za-z]{3,9})\.?\s*(?:\(?\s*([BMEL])\s*\)?)?")


def parse_window(text) -> tuple[str, str] | None:
    """-> ("MM-DD", "MM-DD") or None if the cell holds no month."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return None
    s = str(text).replace("�", "-").replace("–", "-").replace("_", "-").replace(";", "")
    marks = []
    for word, mark in TOKEN.findall(s):
        month = MONTHS.get(word.lower()) or MONTHS.get(word.lower()[:3])
        if month:
            marks.append((month, (mark or "").upper()))
    if not marks:
        return None
    (m1, q1), (m2, q2) = marks[0], marks[-1]
    start_day = {"B": 1, "M": 11, "E": 21, "L": 21}.get(q1, 1)
    end_day = {"B": 10, "M": 20}.get(q2, calendar.monthrange(2001, m2)[1])
    return f"{m1:02d}-{start_day:02d}", f"{m2:02d}-{end_day:02d}"


def gadm_states() -> list[str]:
    import geopandas as gpd

    path = BOUNDARIES_RAW / load_config("project")["boundaries"]["subdistrict"].split("/")[-1]
    return sorted(gpd.read_file(path, ignore_geometry=True, columns=["NAME_1"])["NAME_1"].unique())


def resolve_state(raw: str, states: list[str], log: dict) -> str | None:
    name = re.sub(r"\s+", " ", str(raw)).strip()
    if not name or name.lower() == "nan" or name.isdigit():
        return None
    if name.lower().startswith("all india"):
        return "All India"
    if name.lower() in STATE_ALIASES:
        log[name] = (STATE_ALIASES[name.lower()], "alias")
        return STATE_ALIASES[name.lower()]
    hit = process.extractOne(name, states, scorer=fuzz.WRatio)
    if hit and hit[1] >= 90:
        log[name] = (hit[0], f"{hit[1]:.0f}")
        return hit[0]
    log[name] = (None, f"best {hit[0]!r} {hit[1]:.0f}" if hit else "no match")
    return None


def read_sheet(xls: pd.ExcelFile, sheet: str, spec: dict, states: list[str], log: dict):
    df = pd.read_excel(xls, sheet, header=None)
    rows = []
    if spec["layout"] == "pair":
        for _, r in df.iterrows():
            state = resolve_state(r[0], states, log)
            sow, harvest = parse_window(r[spec["sow"]]), parse_window(r[spec["harvest"]])
            if state and sow and harvest:
                crop, season = spec["crop"]
                rows.append((crop, season, state, sow, harvest,
                             str(r[spec["sow"]]).strip(), str(r[spec["harvest"]]).strip()))
        return rows
    state = None
    for i in range(len(df)):
        first = df.iat[i, 0]
        period = str(df.iat[i, 1]).lower()
        if isinstance(first, str) and first.strip():
            state = resolve_state(first, states, log)
        if not state or "sowing" not in period or i + 1 >= len(df):
            continue
        for col, (crop, season) in spec["cols"].items():
            c = col % 100                        # 1xx re-reads a shared column
            sow, harvest = parse_window(df.iat[i, c]), parse_window(df.iat[i + 1, c])
            if sow and harvest:
                rows.append((crop, season, state, sow, harvest,
                             str(df.iat[i, c]).strip(), str(df.iat[i + 1, c]).strip()))
    return rows


def _doy(md: str) -> int:
    return dt.date(2001, int(md[:2]), int(md[3:])).timetuple().tm_yday


def _md(doy: int) -> str:
    d = dt.date(2001, 1, 1) + dt.timedelta(days=(doy - 1) % 365)
    return f"{d:%m-%d}"


def derive_stages(sow: tuple[str, str], harvest: tuple[str, str], fractions: dict) -> dict:
    """Vegetative / flowering / maturity windows spanning early- to late-sown fields.

    A field sown on the first sowing day is harvested around the first harvest day; one
    sown on the last sowing day around the last harvest day. Each stage window therefore
    runs from the earliest field entering it to the latest field leaving it, so stage
    windows overlap - as they do on the ground within a district.
    """
    s0, s1 = _doy(sow[0]), _doy(sow[1])
    h0, h1 = _doy(harvest[0]), _doy(harvest[1])
    early = (h0 - s0) % 365 or 1          # growing length, earliest field
    late = (h1 - s1) % 365 or 1           # growing length, latest field
    out, done = {}, 0.0
    for stage in ("vegetative", "flowering", "maturity"):
        start = s0 + round(early * done)
        done += fractions[stage]
        end = s1 + round(late * done)
        out[stage] = {"window": [_md(start), _md(end)], "basis": "derived"}
    return out


def suspect(window: tuple[str, str], max_days: int = 150) -> bool:
    """A sowing or harvest window longer than ~5 months is almost certainly a typo in
    the source (e.g. "Apr(B)-Mar(E)"); it is kept verbatim but flagged."""
    return (_doy(window[1]) - _doy(window[0])) % 365 > max_days


def main() -> int:
    print("--- build_crop_calendar ---")
    if not SOURCE_FILE.is_file():
        raise DataError(f"missing {SOURCE_FILE}; download {SOURCE['file']}")
    stages_cfg = load_config("crop_stages")
    states = gadm_states()
    xls = pd.ExcelFile(SOURCE_FILE)
    log: dict = {}
    rows = []
    for sheet, spec in SHEETS.items():
        got = read_sheet(xls, sheet, spec, states, log)
        print(f"  {sheet}: {len(got)} crop-season-state windows")
        rows += got
    if not rows:
        raise DataError("no windows parsed from the DES calendar")

    calendar_out: dict = {}
    suspects: list[str] = []
    for crop, season, state, sow, harvest, raw_sow, raw_harvest in rows:
        entry = {
            "sowing": {"window": list(sow), "basis": "sourced", "verbatim": raw_sow},
            "harvest": {"window": list(harvest), "basis": "sourced", "verbatim": raw_harvest},
            **derive_stages(sow, harvest, stages_cfg["fractions"][crop]),
        }
        flagged = [k for k, w in (("sowing", sow), ("harvest", harvest)) if suspect(w)]
        if flagged:
            entry["suspect"] = flagged
            suspects.append(f"{crop}/{season}/{state}: {flagged} "
                            f"({raw_sow!r} / {raw_harvest!r})")
        calendar_out.setdefault(crop, {}).setdefault(season, {})[state] = entry

    # Crops the brief needs that DES does not cover: a documented default, never
    # presented as sourced.
    for crop, spec in (stages_cfg.get("unsourced_defaults") or {}).items():
        for season, win in spec.items():
            if crop in calendar_out and season in calendar_out[crop]:
                continue
            entry = {"sowing": {"window": win["sowing"], "basis": "default"},
                     "harvest": {"window": win["harvest"], "basis": "default"},
                     **derive_stages(tuple(win["sowing"]), tuple(win["harvest"]),
                                     stages_cfg["fractions"][crop])}
            calendar_out.setdefault(crop, {}).setdefault(season, {})["All India"] = entry

    unmatched = sorted(k for k, (v, _) in log.items() if v is None)
    header = (
        "# GENERATED by python -m src.build_crop_calendar - do not hand-edit; rerun.\n"
        f"# Source: {SOURCE['name']}\n#   {SOURCE['url']}\n#   {SOURCE['file']}\n"
        "# basis: sourced = DES sowing/harvest window; derived = split by crop_stages.yaml;\n"
        "#        default = crop absent from DES (see crop_stages.yaml unsourced_defaults).\n"
        "# A state without its own entry uses the crop's 'All India' entry.\n")
    OUT.write_text(header + yaml.safe_dump({"source": SOURCE, "crops": calendar_out},
                                           sort_keys=True, allow_unicode=True, width=100),
                   encoding="utf-8")

    print("  state names (DES -> GADM):")
    for raw, (name, how) in sorted(log.items()):
        print(f"    {raw!r:<26} -> {name!r} ({how})")
    print("  windows per crop:")
    for crop, seasons in sorted(calendar_out.items()):
        parts = ", ".join(f"{s}: {len(v)}" for s, v in sorted(seasons.items()))
        print(f"    {crop:<10} {parts}")
    if unmatched:
        print(f"  UNMATCHED DES state names (dropped): {unmatched}")
    if suspects:
        print(f"  SUSPECT windows kept verbatim but flagged ({len(suspects)}):")
        for line in suspects:
            print(f"    {line}")
    summarize("build_crop_calendar", rows=len(rows), files=[OUT],
              extra={"crops": len(calendar_out), "unmatched states": len(unmatched)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
