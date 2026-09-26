# CLAUDE.md — Monsoon AI

Permanent project context. Read this before any work in this repo.

## 1. Project

Hackathon prototype: a **probabilistic 1–4 week monsoon outlook** at **sub-district level**
for **Uttar Pradesh, NCT of Delhi, Bihar, Madhya Pradesh, Maharashtra**.

Forecast targets (all probabilistic, per unit, per lead week 1–4):
- **Onset** — monsoon onset date / probability onset occurs in a given week.
- **Dry spells** — probability of a run of consecutive low-rain days.
- **Heavy rain** — probability of exceeding heavy-rain thresholds.

Deliverables:
- Color-coded risk maps (per unit, per hazard, per lead week).
- Rule-based crop advisories derived from the forecast probabilities.
- Advisory text in **Hindi / Marathi / English**.
- **Streamlit** app + **WhatsApp** delivery via **Twilio sandbox**.

## 2. Hardware constraints (non-negotiable)

Windows, Ryzen 7 7435HS, **RTX 4050 6 GB VRAM**, **16 GB RAM**.

- **Never load all-India data.** Clip to the project bounding box *first*, on read:
  `BBOX = lat 15.5–31, lon 72.5–88.5`.
- Process **year-by-year** or **state-by-state**. Never hold the full time series
  for all units in memory at once if it can be streamed.
- Store all numeric data as **float32** (never float64) in processed outputs.
- Small GPU batch sizes. Assume ~5 GB usable VRAM, not 6.
- Prefer Parquet (pyarrow) for tabular intermediates, NetCDF for gridded.

## 3. Stack

Python **3.11** in a venv at `.venv`. Versions are **pinned** in `requirements.txt`.

Core: `xarray`, `netCDF4`, `geopandas`, `shapely`, `scipy` (sparse), `imdlib`, `cdsapi`,
`pandas`, `pyarrow`.
Modeling: `lightgbm`, `scikit-learn`, `autogluon.timeseries` (**Chronos-2**), `torch` (CUDA 12.x).
Viz / app: `matplotlib`, `folium`, `streamlit`, `streamlit-folium`.
Infra: `python-dotenv`, `twilio`, `pyyaml`, `rapidfuzz`, `pytest`.

## 4. Layout

```
config/            YAML config (onset.yaml, zones, thresholds, advisories)
data/raw/          immutable downloads (IMD, ERA5, GADM boundaries)
data/processed/    derived artifacts (float32, Parquet/NetCDF)
src/               all pipeline code, run as modules
models/            trained model artifacts
app/               Streamlit app
outputs/figs/      maps and figures
outputs/metrics/   validation / test scores
tests/             pytest
```

## 5. Boundaries

GADM 4.1, already present:
- `data/raw/boundaries/gadm41_IND_3.shp` — **sub-districts**, `unit_type="subdistrict"`
- `data/raw/boundaries/gadm41_IND_2.shp` — **districts**

Verified against the files on 2026-09-26 — the exact `NAME_1` strings are
`Uttar Pradesh`, `NCT of Delhi`, `Bihar`, `Madhya Pradesh`, `Maharashtra`
(36 distinct `NAME_1` values in each file; level 2 has 676 features, level 3 has 2347;
CRS `EPSG:4326`).

Unit counts inside the 5 regions:

| Region | districts (L2) | sub-districts (L3) |
|---|---|---|
| Uttar Pradesh | 75 | 214 |
| Madhya Pradesh | 51 | 166 |
| Maharashtra | 36 | 305 |
| Bihar | 38 | 53 |
| NCT of Delhi | 1 | 1 |
| **total** | **201** | **739** |

**GADM level 3 is uneven.** Bihar has 53 L3 units for 38 districts (it has ~534 blocks),
Delhi has 1, several districts have a single L3 polygon. So "sub-district" resolution is
real for MP / Maharashtra / UP and effectively district-level for Bihar and Delhi. This is
exactly why the pipeline must accept **block polygons swapped in later** — report the
actual unit count per state in every run, and never present GADM L3 as uniform blocks.

Rules:
- Filter on `NAME_1` for the 5 regions. **Print the exact `NAME_1` values found — never
  guess or hardcode a spelling** you have not seen in the file. Use `rapidfuzz` to match
  user-supplied names to actual values, and report the match.
- **Do not trust fuzzy matching or `VARNAME_2` alone for districts.** `Beed` is `Bid` in
  GADM and fuzzy-matches `Nanded` at 60; the `Latur` row's `VARNAME_2` holds Raigad's
  variants. Pilot districts are resolved through the verified `gadm_name_2` table in
  `config/project.yaml`.
- Every unit carries a stable `unit_id` plus `unit_type`. The pipeline must keep working
  when **block polygons are swapped in later** for sub-districts: nothing downstream may
  assume GADM-specific columns beyond what the loader normalizes.

## 6. Pilot units

Develop **every phase on the pilots first**, 2 per state:

| State | Pilots |
|---|---|
| Uttar Pradesh | Jhansi, Gorakhpur |
| Bihar | Gaya, Purnia |
| Madhya Pradesh | Sehore, Indore |
| Maharashtra | Latur, Beed (GADM: `Bid`) |

**Delhi appears only in full runs.** Scale to all units **only after the user confirms.**

The 8 pilot districts contain **30 sub-district units**: Jhansi 4, Gorakhpur 2, Gaya 1,
Purnia 1, Sehore 5, Indore 4, Latur 5, Bid 8.

## 7. Agro-climatic zones

Attach the **NARP agro-climatic zone** per district for each state; **cite the source**
in `config/` alongside the mapping. `Delhi = "NCT"`.

`config/zones_narp.csv` is the reviewed artifact: one row per district with `zone_id`,
a clean `zone_name`, the **verbatim `zone_name_in_source`**, and the ICAR-CRIDA
contingency-plan URL it was transcribed from. `python -m src.build_zones` re-downloads
every cited PDF and asserts the verbatim wording still appears in it, so a rotted
citation fails the run.

Measured 2026-09-26: **52 of 201 districts** are sourced from 7 verified PDFs, covering
**194 of 739 units** and **all 8 pilot districts**. The other 149 districts are reported
as unzoned and fall back to the `defaults` block of `config/onset.yaml`.

Two traps this encodes:
- **The source PDFs contain typos** — "Western Maharastra", "Centeral Maharashatra",
  "Malawa plateau" — and two use broken font encodings that render spaces as `.` or `V`
  and hyphens as `0` or `U`. That is why the verbatim column exists.
- **Some districts straddle two zones** with neither holding a majority (Sehore is
  MP-10 46% / MP-5 42%; Latur and Bid are listed jointly under MH-6/MH-7). These carry
  a joint `zone_id` rather than an invented single assignment.

## 8. Languages

- **Hindi** — Uttar Pradesh, NCT of Delhi, Bihar, Madhya Pradesh
- **Marathi** — Maharashtra
- **English** — all states

## 9. Onset

Thresholds are **configurable per zone** in `config/onset.yaml`. Never hardcode an
onset rule in `src/`; `src/onset.py` reads the config and resolves zone -> state ->
defaults.

Default rule: onset is the first day `D` in the search window (15 May - 30 Sep) where

1. `rain[D : D+3]` sums to **>= 20 mm**, and
2. **no >= 7-day dry spell (each day < 1 mm) begins in the 30 days** after that
   trigger window.

Criterion 2 is the false-start filter. A late candidate that cannot be confirmed
because the record ends is **not** declared an onset.

Per-zone calibration evidence already in hand: the ICAR-CRIDA plans state a normal
onset week per zone (Gaya BI-3 "3rd week of June", Indore MP-10 "2nd week of June"),
which is what `zones:` in `onset.yaml` should be tuned against in Phase 3. Until then
every zone uses the same documented default rather than an invented one.

## 10. Efficiency: area-weight matrix

Compute unit rainfall with a **sparse unit × grid-cell area-weight matrix**, built **once**
and cached, then **matrix-multiply the daily grids**:

```
unit_rain[T, U] = daily_grid[T, C] @ W[C, U]     # W sparse, rows sum to 1 per unit
```

Measured: **739 x 4095, 4947 non-zeros (0.16% dense), built in ~1 s**, every row summing
to 1.000000000.

Two details that make it correct rather than merely plausible:
- Areas are computed in **EPSG:6933** (equal-area). In degrees a cell at 31 N would be
  weighted as if it were the size of one at 15.5 N.
- Cells with no observations (the -999 sentinel) are **zeroed before rows are
  renormalised**, so a coastal unit is averaged only over cells that hold data.

`build_weights` also caches the raw intersection areas (`unit_area_valid`,
`cell_coverage`), which makes an exact conservation identity testable:
`sum_u rain_u * area_u == sum_c rain_c * coverage_c`. `build_unit_rain` checks it every
run and currently agrees to **0.0000%**. Do **not** compare the unit mean against a raw
BBOX cell mean - the BBOX includes the Thar desert and other states, so that comparison
is ~47% off and means nothing.

**Never clip polygons per day.** If the weight matrix is missing, build it, persist it to
`data/processed/`, and reuse it.

## 11. Rules

- **Time splits, no leakage.** train `1990–2018`, validate `2019–2021`, test `2022 onward`
  (include 2025 if IMD data exists). **Climatology is computed from training years only.**
  Never fit, tune, or standardize using validate/test years.
- **Every script runs as `python -m src.<name>`**, prints a summary (**rows, files written,
  date range**), and **fails loudly on empty output** (raise, non-zero exit — never a
  silent empty file).
- **Secrets only from `.env`** via `python-dotenv`. Never print, log, or commit a secret.
  Keep `.env.example` in sync with every new key.
- **After each working phase**: summarize results and suggest a git commit message.

## 12. Phase 2: targets and features

`python -m src.build_features` writes `data/processed/train_table.parquet`: one row per
unit x forecast start date, pilot districts only (`--all-units` to widen).

**Timing convention - the whole no-leakage story.** For a forecast issued on `s`:

| | reads |
|---|---|
| rainfall features | days `... s-1` (day `s` is not over yet) |
| climate indices | the value *published* as of day `s` |
| horizon `h` targets | days `s ... s+h-1` |

Features and targets never overlap. `tests/test_features.py` proves it causally: it
rebuilds with every rainfall value from `s` onward replaced by noise and asserts the
feature columns are bit-identical.

**Publication lag is modelled, not assumed.** A monthly index describes month `M` but
is not knowable until `M` ends, so `fetch_indices` carries each value forward only from
its publication date - `M+1` for a plain monthly mean, `M+2` for ONI (a 3-month mean
centred on `M` is not complete until `M+1` ends), `D+1` for MJO. Every index also
carries `<index>_age_days`. Measured today: MJO 4 days old, ONI 87, Nino3.4 117,
DMI 148. Never forward-fill an index without that age column.

**`onset_happened`** is the *knowable* version: it turns on only once the onset's
confirmation window has closed (`onset_date + accum_days + confirm_window_days <= s`),
not when the onset actually occurred. The `onset_h` target separately goes NaN once
onset has occurred, because there is nothing left to forecast.

Targets: `onset_h`, `dry_h` (a >=10-day run under 2.5 mm starting in the horizon),
`heavy_h` (any day >= 64.5 mm), `total_h`, `anomaly_h` (vs training climatology).

## 13. Conventions

- Configs are YAML in `config/`; code reads config, code does not embed magic numbers.
- Long-running steps are idempotent and skip work whose output already exists, unless
  `--force`.
- `data/raw/` is read-only once downloaded.
