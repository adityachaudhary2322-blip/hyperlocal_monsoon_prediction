---
title: Monsoon AI Advisory Control Panel
emoji: "🌧"
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: Probabilistic 1-4 week monsoon outlook and crop advisories
---

# Monsoon AI

Probabilistic 1–4 week monsoon outlook (onset, dry spells, heavy rain) at sub-district
level for Uttar Pradesh, NCT of Delhi, Bihar, Madhya Pradesh and Maharashtra, with
color-coded risk maps, rule-based crop advisories in Hindi/Marathi/English, a Streamlit
app and WhatsApp delivery via the Twilio sandbox.

Project context and hard constraints live in [CLAUDE.md](CLAUDE.md). Read it first.

## Setup

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in credentials
.venv\Scripts\python.exe -m src.check_env
```

`requirements.txt` carries an `--extra-index-url` for the CUDA 12.8 PyTorch wheels.

## Running things

Every pipeline step is a module, prints a summary (rows, files written, date range),
and fails loudly on empty output. Run them in this order:

```powershell
.venv\Scripts\python.exe -m src.check_env        # CUDA + every import

# Phase 1 - data
.venv\Scripts\python.exe -m src.fetch_imd        # IMD .grd archive + current-year realtime
.venv\Scripts\python.exe -m src.build_rain_grid  # clip to BBOX -> imd_rain.nc (float32)
.venv\Scripts\python.exe -m src.build_units      # GADM L3 -> units.gpkg
.venv\Scripts\python.exe -m src.build_zones      # NARP zones, citations re-verified
.venv\Scripts\python.exe -m src.build_weights    # sparse unit x cell weights -> weights.npz
.venv\Scripts\python.exe -m src.build_unit_rain  # weights @ grid -> unit_rain.parquet
.venv\Scripts\python.exe -m src.fetch_indices    # ONI/Nino3.4/DMI/MJO -> indices.parquet
.venv\Scripts\python.exe -m src.data_quality     # outputs/data_quality.md + figures

# Phase 2 - targets and features
.venv\Scripts\python.exe -m src.build_features   # -> train_table.parquet (pilots only)

# Phase 3 - baselines
.venv\Scripts\python.exe -m src.train_baselines     # climatology + calibrated LightGBM
.venv\Scripts\python.exe -m src.evaluate_baselines  # metrics.csv, figures, baseline_report.md

# Phase 4 - Chronos-2 stacking hybrid
.venv\Scripts\python.exe -m src.chronos_features    # Chronos-2 weekly quantiles (GPU)
.venv\Scripts\python.exe -m src.stack_models        # stackers, model_comparison.md, model_choice.yaml

# Phase 5 - expert system
.venv\Scripts\python.exe -m src.advisory --demo   # worked example per state

# Phase 6 - API language layer (needs SARVAM_API_KEY / GEMINI_API_KEY in .env)
.venv\Scripts\python.exe -m src.translate_rules --language mr   # pre-translate rules.yaml
.venv\Scripts\python.exe -m src.prewarm --languages en,hi,mr    # cache for offline demo

.venv\Scripts\python.exe -m pytest -q
```

Steps are idempotent: an existing output is left alone unless you pass `--force`.
`src.build_features` takes `--all-units` to go beyond the pilot districts.

## Layout

| Path | Contents |
|---|---|
| `config/` | YAML config — `project.yaml`, `onset.yaml`, `zones.yaml` |
| `data/raw/` | immutable downloads (IMD, ERA5, GADM) |
| `data/processed/` | derived float32 artifacts (Parquet / NetCDF) |
| `src/` | pipeline code |
| `models/` | trained model artifacts |
| `app/` | Streamlit app |
| `outputs/figs`, `outputs/metrics` | maps and scores |
| `tests/` | pytest |

`src.chronos_features` needs a GPU and downloads `autogluon/chronos-2` (~1 GB) on
first run. It reads every parquet **before** importing AutoGluon: on this machine
`pd.read_parquet(filters=...)` segfaults once torch is loaded.

## Data splits

train 1990–2018 · validate 2019–2021 · test 2022 onward. Climatology uses training
years only.

## Public dashboard

The front page (`app/pages/public/home.py`) is public: a MapLibre globe with national
weather, monsoon risk for the 5 covered states, three data-driven animations, and an
About page. Officer pages appear only after **Sign in (officers)**.

```powershell
# Map files in app/static/ (served by Streamlit static serving). Uses the Survey of India
# download in data/raw/official_boundary/ if present, else the SoI mirror (see below).
.venv\Scripts\python.exe scripts\build_public_map_assets.py
.venv\Scripts\python.exe scripts\build_onset_replay.py     # monsoon-advance replay, 2022-2025
.venv\Scripts\python.exe scripts\export_models.py --skill-only   # model_skill.csv for Home
# National weather (3-hourly) and the animation grid (6-hourly), into the database:
.venv\Scripts\python.exe -m src.jobs.weather
.venv\Scripts\python.exe -m src.jobs.weather_grid --write-static
```

**Official boundaries.** Survey of India publishes *Digital Vector Data 1:1M* free on
[onlinemaps.surveyofindia.gov.in](https://onlinemaps.surveyofindia.gov.in/Digital_Product_Show.aspx),
but the download needs a signed-in account. Download **OVSF/1M/7** ("Entire country up to
district level with HQ"), unzip it into `data/raw/official_boundary/`, and rerun
`build_public_map_assets.py`. Until then the build uses the same SoI layers from the
[india-geodata](https://github.com/yashveeeeeeer/india-geodata) mirror
(`data/raw/boundary_mirror/SOI_{States,Districts}.parquet`, release tags `admin/states`
and `admin/districts`) and the map says "Boundaries indicative, not official".

**GitHub Actions.** `.github/workflows/weather.yml` (every 3 h) and `weather-grid.yml`
(every 6 h) keep the public map current while the laptop is off. Add one repository
secret, **`DATABASE_URL_CLOUD`** (Settings → Secrets and variables → Actions); the jobs
read it by name through `--db-url-env`. They install only `requirements-weather.txt`.
Open-Meteo budget: ~8,080 calls/day of the free 10,000 (non-commercial use; data CC BY 4.0,
credited in the footer).

**Partner logo.** `app/static/partners/` is empty on purpose. The footer shows a partner
logo only if a file named `moes-logo.png` is placed there by hand.

## Deploying to Streamlit Community Cloud

Entry file **`app/main.py`**. The free tier gives ~2.7 GB of RAM, no persistent disk,
and secrets through `st.secrets`. The site serves the public dashboard and the officer
portal; the data pipeline and Chronos-2 stay on the laptop.

### Two dependency files

| File | Used by | Contains |
|---|---|---|
| `requirements.txt` | the website | streamlit, folium, geopandas, lightgbm, SQLAlchemy, psycopg2 — no torch, autogluon, imdlib or xarray |
| `requirements-dev.txt` | the laptop | `-r requirements.txt` plus the research stack (CUDA torch, autogluon.timeseries, xarray, netCDF4, imdlib, cdsapi, scikit-learn, matplotlib) |

`tests/test_deployment.py` fails if a package is pinned at two different versions
across the two files, or if the website file grows a package it does not need.

### What lives where

| Thing | Where | Why |
|---|---|---|
| Forecasts, advisories, subscribers, users, audit log | Postgres via `DATABASE_URL` | the disk is wiped on every restart |
| LightGBM boosters + isotonic breakpoints + feature rows | `app/assets/models/`, committed (4.0 MB) | Streamlit Cloud deploys from git; there is no model registry |
| Sub-district polygons | `app/assets/map_layers_simplified.gpkg`, committed (2.1 MB) | read one state at a time |
| Secrets | **App settings → Secrets** | see `.streamlit/secrets.toml.example` |
| Public map GeoJSON, search index, onset replay, brand files | `app/static/`, committed (~4 MB) | served by `server.enableStaticServing`, loaded one state at a time |
| National weather and animation grids | Postgres (`weather_now`, `weather_state`, `weather_grid`) | written by GitHub Actions; the app copies grids into `app/static/` at runtime |

`.gitignore` ignores `data/`, `models/`, `.venv/`, `.env` and
`.streamlit/secrets.toml`, and re-includes `app/assets/**` — the blanket `*.parquet`
rule would otherwise drop the committed feature table.

### One-time setup

```powershell
# 1. Commit the models the site forecasts with. Isotonic calibrators are converted to
#    JSON breakpoints, so the site needs no scikit-learn or joblib; the script asserts
#    numpy.interp reproduces IsotonicRegression.predict exactly before writing them.
.venv\Scripts\python.exe scripts\export_models.py

# 2. Commit the simplified map layer (739 units, 2.1 MB, indexed on state).
.venv\Scripts\python.exe scripts\build_map_assets.py

# 3. Copy the local SQLite database into Postgres. --target-env names the variable, so
#    the connection string never reaches the shell history or the process list.
.venv\Scripts\python.exe -m src.db.migrate_to_postgres --target-env DATABASE_URL_CLOUD --reset
```

Then set `DATABASE_URL` and `HOSTED_MODE = "true"` in the app's Secrets, and point
Streamlit Cloud at `app/main.py`.

### Settings: one name, two sources

`src/config.py` is the only settings lookup. Precedence is environment variable →
`st.secrets` → `.env`, under identical names in all three, so nothing in the app
branches on where it is running. `python -m src.config` prints which settings are
configured and never a value.

### What `HOSTED_MODE=true` changes

- **Run forecast** predicts with LightGBM from `app/assets/models/`, in-process rather
  than by forking a second Python — the free tier does not have room for two
  interpreters each holding the boosters and the feature table.
- Chronos-2 controls are hidden and the page states that *Chronos-2 runs on the
  forecast engine*.
- Everything else reads `DATABASE_URL`.
- The Risk Map loads one state's shapes at a time, cached with `max_entries=2`.

The committed and locally trained models were verified to agree **bit for bit** — 360
probabilities on 2024-06-19, maximum difference 0.0 — so hosting does not change a
number. `tests/test_deployment.py` re-checks that.

### Managing accounts

`scripts/manage_users.py` works against **DATABASE_URL_CLOUD** - the deployed site's
database, since that is where the accounts people sign in with live.

```powershell
.venv\Scripts\python.exe scripts\manage_users.py list
.venv\Scripts\python.exe scripts\manage_users.py create officer_latur --role officer --states Maharashtra
.venv\Scripts\python.exe scripts\manage_users.py reset-password vrrtanta
.venv\Scripts\python.exe scripts\manage_users.py deactivate admin
```

- Passwords are read with `getpass`, so they do not echo and never reach argv, the
  shell history or the process list. There is deliberately no `--password` flag;
  `--password-env NAME` is the non-interactive route.
- `list` shows username, role, active and scope. It never reads or prints a hash.
- The connection string is never printed - only the variable it came from - and a driver
  error is scrubbed of its password, user and host first.
- Deactivating or demoting the **last active admin** is refused. The only way back in
  would be `python -m src.db.init --reset`, which wipes the forecasts too.
- Every change lands in `audit_log`.

`ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` set the admin account that
`python -m src.db.init` seeds, so a `--reset` does not resurrect a default `admin`
after the real administrator has been renamed.

### Writing full Chronos-2 forecasts from the laptop

The website forecasts with LightGBM. To push a Chronos-2 run into the same cloud
database, run the pipeline locally against it:

```powershell
.venv\Scripts\python.exe -m src.pipeline.run --pilot-only --as-of 2026-06-20 `
    --db-url-env DATABASE_URL_CLOUD
```

`--db-url` takes a literal URL; `--db-url-env` takes the *name* of a variable holding
one and is preferred, because a connection string on a command line ends up in the
shell history and the process list.

### Testing the hosted build locally

```powershell
py -3.11 -m venv .venv-site
.venv-site\Scripts\python.exe -m pip install -r requirements.txt
$env:HOSTED_MODE="true"
.venv-site\Scripts\streamlit.exe run app/main.py
```

### Chronos-2 on CPU

Off, and the reason is not cost. Measured 2026-09-27 with CUDA disabled on 2 threads,
30 pilot units, 730-day context: **25 s** for one forecast date (5 s to load the
predictor, 20 s to predict) against a 5-minute budget. What is missing is the stacker —
`src/stack_models.py` fits the logistic regression that turns Chronos quantiles into
hazard probabilities in memory and never saves it, so an online run has no artifact to
load. `requirements-chronos.txt` layers CPU-only torch and `autogluon.timeseries` for
when it is persisted.

### Docker (Hugging Face Spaces)

`Dockerfile` also builds this app as a Docker Space on port 7860 (`app_port` in the
front matter above). It installs `requirements.txt`, adds `libgomp1` for LightGBM, and
points `HF_HOME`/`MPLCONFIGDIR` at `$HOME` because only `$HOME` and `/tmp` are
writable there. `scripts/upload_models.py` and `src/artifacts.py` are the model-repo
path that target uses instead of committed assets.
