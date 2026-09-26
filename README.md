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

## Data splits

train 1990–2018 · validate 2019–2021 · test 2022 onward. Climatology uses training
years only.
