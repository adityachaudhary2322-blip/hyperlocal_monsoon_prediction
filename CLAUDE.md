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

- **Never load all-India data at once.** The 5-state pipeline clips to the project
  bounding box *first*, on read: `BBOX = lat 15.5–31, lon 72.5–88.5`. The national
  pipeline (`src.build_national_rain`, since 2026-09-27) reads the full IMD grid **one
  year at a time** - grid[days, 17,415] @ W.T per year, never all years in memory.
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

## 13. Phase 3: baselines

`src.train_baselines` then `src.evaluate_baselines`. Measured on test years 2022-2026,
pilot districts only.

**The headline: good ranking, poor probabilities.** AUC 0.62-0.78 everywhere, but only
2 of 12 target/horizon pairs beat climatology on Brier Skill Score.

**Calibrate on the training years, cross-fitted. Never on the validation years.**
Fitting isotonic on 2019-2021 cost skill in 11 of 12 cases: those three years are not
representative (dry spells 35.8% there against 53.5% in training and 47.7% in test), and
isotonic is flexible enough to lock that base rate in. The fix is five folds **grouped
by year** across 1990-2018, calibrating on the out-of-fold predictions.

Measured effect of that one change:

| | validation-fitted | cross-fitted |
|---|---:|---:|
| beats climatology (test) | 2/12 | **6/12** |
| mean test BSS | -0.064 | **-0.007** |
| validation optimism (val BSS - test BSS) | 0.190 | **0.030** |

Test BSS improved in 11 of 12 and got worse in none; the biggest gains are at long
horizons (dry_28 -0.153 -> -0.010, onset_21 -0.141 -> -0.014). All four calibrations
(`none`, `isotonic_val`, `isotonic_cv`, `platt_cv`) stay in `predictions.parquet` so
the choice is auditable; `--calibration` switches which feeds `p_lgbm`.

**ENSO/IOD/MJO earn their keep for dry spells, not for onset.** Ablation over 3 seeds:
dry_14 +0.027 BSS with the climate columns, onset_14 **-0.031** (overfitting on ~9.5k
rows). `rmm2` is the single largest climate feature. SHAP alone cannot answer this -
always ablate.

Always report `n` and `n_pos` beside any metric: Purnia's `onset_7` has 11 test rows
and Gaya's `heavy_7` has 2 events.

## 14. Phase 4: Chronos-2 stacking

`src.chronos_features` then `src.stack_models`. Chronos-2 forecasts daily rainfall at
every validation/test start date (223 of them); weekly q10/q50/q90 totals for weeks 1-4
feed a logistic regression that also takes the LightGBM log-odds.

**Chronos helps heavy rain and hurts onset.** Test BSS, LightGBM -> best stack:
heavy_7 +0.007 -> **+0.038**, heavy_14 -0.007 -> **+0.021**, heavy_28 -0.099 -> -0.049.
Onset goes the other way: onset_28 -0.138 -> **-0.331**. That is the expected shape -
Chronos's upper quantile is close to "will any day exceed 64.5 mm", while onset gives
the stacker only 705 training rows for 13 features.

**Fine-tuning bought almost nothing** for ~2 hours of GPU: within +/-0.03 BSS of
zero-shot everywhere. Zero-shot first, always.

**Validation-based model selection agreed with the test years in 0 of 12 cases before
the calibration fix, 3 of 12 after.** The remaining failures are no longer LightGBM -
they are the Chronos stackers, which are trained on the three validation years and
scored leave-one-year-out on the same three, so their validation scores still overstate
(dry_7: +0.092 validation, +0.014 test). Fixing that properly means cross-fitting the
stacker over the training years too, which needs Chronos features back to 1990 (~800
extra start dates, about an hour of GPU). Until then, trust `test_best_model` in
`config/model_choice.yaml` over `model`.

Two practical traps, both fixed in code and worth remembering:
- **`pd.read_parquet(filters=...)` segfaults once torch is imported** (pyarrow/torch
  native clash). Read every file before importing AutoGluon.
- **Batch one start date per predict call.** Chronos-2's `cross_learning` predicts
  jointly across a batch, so mixing start dates lets it see context past another item's
  cut-off.

## 15. Phase 5: expert system

`config/rules.yaml` + `src/advisory.py`. Every threshold, sowing window, action text
and tie-break is config; the module only evaluates them.

**`notes_agronomy.txt` does not exist in this repo.** The Phase 5 brief told me to read
it. Rather than invent sowing windows and irrigation triggers, the rule base is
transcribed from the **ICAR-CRIDA district contingency plans** already cited in
`config/zones_narp.csv`, with a `source` id on every action and every window. If the
notes turn up, replace `rules.yaml`; no code changes.

Three engine guarantees worth preserving:
- **One field operation per advisory.** A farmer cannot irrigate and drain the same
  plot. `tie_break.priority` picks exactly one; the loser becomes a watch note.
- **Conflicting hazards are resolved BEFORE the action is chosen.** Detecting the
  conflict afterwards let the engine announce "dry wins" while issuing the drainage
  instruction. Both red -> act on the higher probability, tie goes to heavy (drainage
  is cheap and reversible), confidence drops to low.
- **The three sowing rules require `onset_happened == False`.** Once onset occurs the
  onset targets go NaN or near-zero, which is indistinguishable from "monsoon still
  late" - without the guard the engine tells a farmer with a standing crop to switch
  variety. Unknown is not a match: it stays silent.

Marathi is `None` everywhere until Phase 6 and `render("mr")` raises rather than
falling back to English.

## 16. Conventions

- Configs are YAML in `config/`; code reads config, code does not embed magic numbers.
- Long-running steps are idempotent and skip work whose output already exists, unless
  `--force`.
- `data/raw/` is read-only once downloaded.

## 17. Hosting: Streamlit Community Cloud

Entry file `app/main.py`. Free tier: ~2.7 GB RAM for the whole container, no persistent
disk, secrets through `st.secrets`. The website serves the control panel only.

**Two requirements files, and they must not drift.** `requirements.txt` is the *website*
file; `requirements-dev.txt` is the laptop and begins `-r requirements.txt`.
`tests/test_deployment.py` fails if a package is pinned at two versions across them, if
the website file grows `torch`/`autogluon`/`xarray`/`imdlib`/`sklearn`/`matplotlib`, or
if an app module imports one of those at module scope.

**Settings come from one lookup.** `src/config.py`: environment variable ->
`st.secrets` -> `.env`, identical names in all three, so no code branches on where it
runs. Touching `st.secrets` *raises* when there is no secrets file, so it is read inside
a `try` and a missing file reads as "unset". `src.runtime.env()` delegates here;
`src.runtime._load_env_once` is still the single dotenv entry point because the tests
monkeypatch it. `python -m src.config` prints which settings are set, never a value.

**What `HOSTED_MODE=true` changes**: models come from `app/assets/models/`, the forecast
runs in-process instead of forking a second Python, Chronos-2 controls are hidden, and
the Risk Map loads one state at a time.

### The committed assets (`app/assets/`, ~6.1 MB, 28 files)

Streamlit Cloud deploys from git and has no model registry, so whatever the site
forecasts with has to be committed. `.gitignore` ignores `data/` and `models/` and then
re-includes `app/assets/**` - necessary, because the blanket `*.parquet` rule would
otherwise drop the feature table.

| Asset | Built by | Size |
|---|---|---|
| `models/*.txt` (12 boosters) | `scripts/export_models.py` | 3.4 MB |
| `models/*.calib.json` (12 isotonic calibrators) | same | ~30 kB |
| `models/features.parquet` (31,050 x 33, 30 units, 1990-2026) | same | 0.5 MB |
| `map_layers_simplified.gpkg` (739 units) | `scripts/build_map_assets.py` | 2.1 MB |
| `pilot_units.geojson` (30 units) | same | 0.16 MB |

**Calibrators are converted, never copied.** `models/lgbm/*_isotonic_cv.joblib` are
pickled scikit-learn estimators; unpickling one needs scikit-learn *and* joblib at the
versions that wrote it. An isotonic regression is a monotone step function, so the
breakpoints ship as JSON and are evaluated with `numpy.interp`, which reproduces
`IsotonicRegression.predict` **exactly** for `out_of_bounds="clip"`.
`scripts/export_models.py` asserts that against the real estimator before writing, and
refuses any calibrator fitted with a different `out_of_bounds`. Dropping calibration
instead was never an option: cross-fitted isotonic is worth 0.057 mean test BSS (§13).

**Committed and trained models agree bit for bit** - 360 probabilities on 2024-06-19,
maximum difference 0.0. Re-checked by `tests/test_deployment.py`, so hosting cannot
silently change a number.

**Map layers are read one state at a time.** `gpd.read_file(..., where="state = '...'")`
against the GeoPackage returns Bihar's 53 shapes in 0.01 s against 0.09 s for all 739 -
11x - and `@st.cache_data(max_entries=2)` keeps at most two states alive. Geometry is
simplified at **300 m in EPSG:6933**, not in degrees, for the same reason the weight
matrix is (§10). `src/db/init.py` seeds from the GeoJSON, not the GeoPackage, so seeding
needs no GDAL.

### Postgres

`DATABASE_URL`, normalised in `src/db/session.py` - providers hand out `postgres://`,
which SQLAlchemy 2.x rejects. Four portability details:

- `PRAGMA foreign_keys=ON` is SQLite-only and errors on Postgres, so the listener is
  attached per dialect.
- `pool_pre_ping=True` and `pool_recycle=280`: a sleeping container's connections die
  quietly behind the proxy.
- `UTCDateTime` (`src/db/models.py`) converts aware datetimes to naive UTC on bind.
  SQLite's driver drops the offset while formatting and Postgres discards it during the
  cast to `timestamp`; both are right only while every caller passes UTC.
- `src/db/migrate_to_postgres.py` copies in `metadata.sorted_tables` order and then
  **resynchronises every identity sequence** past `max(id)`. Without that the first row
  the app inserts collides with an imported one. Verified: 1,168 rows copied to Neon,
  6 sequences advanced, next `audit_log.id` = 4 where the import ended at 3.

**Never put a connection string on a command line.** `--target-env` /`--db-url-env` take
the *name* of a variable; a URL passed literally lands in the shell history and the
process list. `migrate_to_postgres.scrub()` also strips the password, user and host from
any driver error before it reaches the console.

### Chronos-2 is off, and cost is not the reason

Measured 2026-09-27, CUDA disabled, 2 threads, 30 pilot units, 730-day context:
**5 s to load the predictor + 20 s to predict one start date = 25 s** against a 300 s
budget. The blocker is that `src/stack_models.py` fits the logistic-regression stacker
**in memory and never saves it**, so an online run has no artifact to turn Chronos
quantiles into hazard probabilities. Until it is persisted the app says "Chronos-2 runs
on the forecast engine" and forecasts with LightGBM. `requirements-chronos.txt` layers
CPU-only torch and autogluon for when that changes.

A full Chronos run reaches the cloud database from the laptop instead:
`python -m src.pipeline.run --pilot-only --as-of ... --db-url-env DATABASE_URL_CLOUD`.

### Also still valid: the Docker path

`Dockerfile` builds the same app as a Hugging Face Docker Space on port 7860, installing
`requirements.txt`, adding `libgomp1` (python:3.11-slim has no OpenMP runtime and
`import lightgbm` fails without it), and pointing `HF_HOME`/`MPLCONFIGDIR` at `$HOME`
because only `$HOME` and `/tmp` are writable there. That target uses
`scripts/upload_models.py` + `src/artifacts.py` (a private HF model repo) instead of
committed assets. Docker was not available on this machine, so the image is unbuilt and
untested; the dependency set was verified in a clean venv instead.

## 18. Public dashboard (Home + About), officer portal, branding

`app/main.py` registers **Home and About for everyone**, **Sign in** when signed out,
and the six officer pages only after sign-in (`st.navigation(position="hidden")`, our
own top bar in `app/theme.py`). Each officer page still starts with `require_login()`.

**Public data rule.** Public pages read the database only through `app/public_data.py`:
`units, forecast_runs, forecasts, advisories (status approved/sent only), weather_now,
weather_state, weather_fetch, weather_grid`. `tests/test_public_access.py` checks it three
ways - imports (AST), every SQL statement a Home/About run issues, and planted marker
strings in pending/rejected advisories, subscribers, users and audit rows. A mutation
check (letting pending advisories through) fails the test, verified 2026-09-27. An
officer-edited advisory is shown as its `edited_text` only - that is what was approved.

**Boundaries.** `scripts/build_public_map_assets.py` prefers
`data/raw/official_boundary/` (Survey of India OVSF/1M/7, sign-in download, not yet
present) and otherwise uses the SoI layers from the india-geodata mirror in
`data/raw/boundary_mirror/` - 40 state polygons (incl. 4 `DISPUTED (...)` inter-state
tracts), 742 districts, outline to 37.09 N. The map then says "Boundaries indicative, not
official" (`app/static/boundary_source.json`). Every basemap `boundary` layer and
country/state label is stripped before MapLibre sees the style; place labels wait for
zoom 5.5. Forecast polygons stay GADM L3 (unit ids, §5, §10).

Two traps, both fixed in the build:
- `make_valid` can return GeometryCollections, and GDAL's `COORDINATE_PRECISION` together
  with `RFC7946=YES` *creates* them while rounding. Round with `shapely.set_precision`
  and keep only polygonal parts; `tests/test_deployment.py` asserts plain polygons.
- `st.html` strips inline `<svg>` - brand marks are `<img>` of the committed SVGs. And
  the public CSS hides every `<footer>` (Streamlit's), so ours is a `div`.

**Map component** (`app/components/public_map.{py,js,css}`): `st.components.v2`, not
v1 - v1 recreates its iframe when the HTML changes, v2 keeps the DOM across reruns
(verified: same parent element, no cleanup between reruns), so the globe, wind and any
playback survive week/hazard changes. The control panel lives inside the component and
reports `hazard/week/weather` back with `setStateValue`. MapLibre GL 5.24.0 from
jsDelivr with SRI hashes; `transformStyle` is a `setStyle` option only, so the style is
fetched and filtered first.

**Animations.** Wind flow (850 hPa, particles advected in lon/lat, drawn with
`map.project`; screen-space speed ~0.12 px/frame per m/s; <=3000 desktop / 800 phone;
paused while moving or hidden; only from India zoom). Rain playback (72 hourly + days
4-7 as daily means, values bilinearly upsampled on Mercator-spaced rows, crossfaded
image sources). Monsoon advance (`scripts/build_onset_replay.py`, the same
`detect_onset` rule on unit rainfall; 2022-2025 onsets found in 724/636/736/716 of 739
units, median 15-23 Jun). One animation at a time; reduced motion turns wind off.

**Weather jobs.** `src/jobs/weather.py` (734 SoI districts, 3-hourly) and
`src/jobs/weather_grid.py` (23 x 24 = 552 points at 1.5 deg, 6-hourly) - ~8,080
Open-Meteo calls/day of 10,000. All-or-nothing writes; a failure keeps the last good
rows and exits non-zero. Grids live in `weather_grid`, never in git (a commit every 6 h
would redeploy the site); `public_data.animation_files()` copies them to `app/static/`.

**Design tokens** are in `app/theme.py` and mirrored in `.streamlit/config.toml`
(`tests/test_theme.py`). Streamlit 1.64 cannot switch its native theme from Python, so
the Dark mode toggle drives `--mo-*` CSS variables; canvas-drawn widgets (st.dataframe)
follow the browser's scheme instead, which only differs if a viewer flips the toggle
against their OS setting. Brief colours that fail AA as text (paddy green 3.9:1, amber
2.1:1) are used for fills only, with darker `*-ink` variants for text; control borders
are `#7D909C` / `#627D8E` (>= 3:1). GADM units are called "sub-districts" in public copy,
never "blocks" (§5).

**Branding.** VRRTANTA mark option A ("cloud canopy") in `app/static/brand/`. The footer
credits the Smart India Hackathon / Ministry of Earth Sciences problem statement as text.
`app/static/partners/moes-logo.png` is shown only if placed by hand - never draw or
download a government emblem.

## 19. National hybrid live system (2026-09-27)

**Stage 2 (national ML retrain) is deferred - "coming soon".** Until it lands the live
engine blends nothing: `p_blend = p_ec46`, every run records `blend_basis: ec46_only`,
and the site says so. Replay of past dates also waits for it (no free EC46 archive).

- **National data**: `src.build_units --national` (2,347 GADM units, tiers from
  `config/tiers.yaml`: 739 validated / 1,457 experimental / 151 low, +256 low in Oct-Dec),
  `src.build_national_rain` (1990-2026, one year at a time, conservation 4e-9),
  `src.build_crop_calendar` (DES Appendix IV: sowing/harvest sourced, middle stages
  derived and labelled). District crop areas and agro-climatic region boundaries are
  data.gov.in downloads behind a captcha - not yet provided; crops fall back to
  "state default (DES crop calendar)", zones to NARP-where-sourced + defaults.
  The two CSVs found in Downloads on 2026-09-27 (`crop_production_up_...csv`,
  `imd_agrimet_..._30years.csv`) were NOT used: they look synthetic (fall armyworm in
  1994; smooth monotone areas; 24 districts).
- **Live engine** `src/live/` + `.github/workflows/live.yml` (daily 04:30 UTC, then
  `src.live.verify`). EC46 51 members at 188 points (1.5 deg) + ensemble mean, 0.5 deg
  short range at 1,314 points, IMD real-time observed (Open-Meteo bias-adjusted fallback).
  ~5,740 Open-Meteo calls/run; the client paces itself under 550/min and **4,500/hour**
  (the free tier's hourly 5,000 is the binding limit, so a run takes ~75 min). The
  3-hourly `weather.yml` is retired; the national weather layer comes from the daily run.
- **Events** are defined once in `src/labels.py` / `config/labels.yaml` for EC46 members,
  verification and (later) training. Onset stops being forecast after 15 Aug
  (`config/season.yaml hazard_until`): a strict-rule "no confirmed onset" otherwise reads as
  "the monsoon has not arrived" in September.
- **Storage** (Neon 0.5 GB): per-mille ints; full detail for 7 days + Mondays; observed
  rain for the current season; DB size printed each run, warning above 400 MB.
- **Crop impact** `src/crop_impact.py` + `config/crop_vulnerability.yaml` (+ `_i18n`):
  disease RISK names only, never pesticides/doses (tested); `review: pending`.
- **Public UI**: tabs Map | Next 30 days | Accuracy | About; EN/हिं/मरा (`config/i18n/`,
  Hindi/Marathi drafted, review pending); SoI sub-districts (CC0) for all states coloured
  from the GADM unit covering most of each; LGD blocks in search; Devanagari search is
  transliterated to Latin; `?unit=` deep links; panel built server-side
  (`app/unit_detail.py`). Approved advisories older than 14 days are never shown.
- **Accuracy**: `src/accuracy.py` - no overall %, BSS/AUC/hit/FAR/reliability per hazard
  x week; live scorecard needs 30 verified forecasts; blend-weight suggestions go to the
  `settings` table, never applied automatically.
