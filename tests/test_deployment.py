"""The deployment contract for the Hugging Face Space.

These tests exist because every one of them covers a failure that only shows up after a
Space has been built and started - a wrong `app_port`, a heavy import that drags GDAL
into a slim image, a `postgres://` URL SQLAlchemy 2.x refuses, an id collision from
unsynchronised Postgres sequences. Each is cheap to assert here and expensive to
discover from a build log.

Nothing here needs a Postgres server: the URL handling is pure string work, and the
copy logic is exercised against a second SQLite file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from src.common import ROOT

DOCKERFILE = ROOT / "Dockerfile"
README = ROOT / "README.md"
SITE_REQS = ROOT / "requirements.txt"
DEV_REQS = ROOT / "requirements-dev.txt"
ASSETS = ROOT / "app" / "assets"
APP_PORT = 7860


def _pins(path: Path) -> dict[str, str]:
    return {m.group(1).lower().replace("_", "-"): m.group(2)
            for m in re.finditer(r"^([A-Za-z0-9_.-]+)==([^\s#]+)",
                                 path.read_text(encoding="utf-8"), re.M)}


# ==========================================================================
# Space configuration
# ==========================================================================
def test_readme_front_matter_configures_a_docker_space():
    text = README.read_text(encoding="utf-8")
    assert text.startswith("---"), "Hugging Face reads YAML front matter first"
    front = yaml.safe_load(text.split("---")[1])
    assert front["sdk"] == "docker"
    assert front["app_port"] == APP_PORT
    assert front["title"]


def test_dockerfile_and_readme_agree_on_the_port():
    """A mismatch here serves a blank page with no error anywhere."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert f"EXPOSE {APP_PORT}" in text
    assert f'"--server.port", "{APP_PORT}"' in text
    front = yaml.safe_load(README.read_text(encoding="utf-8").split("---")[1])
    assert front["app_port"] == APP_PORT


def test_container_binds_all_interfaces_and_runs_headless():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert '"--server.address", "0.0.0.0"' in text, "127.0.0.1 is unreachable"
    assert '"--server.headless", "true"' in text


def test_dockerfile_installs_the_openmp_runtime_lightgbm_needs():
    """python:3.11-slim has no libgomp1, and `import lightgbm` fails without it."""
    assert "libgomp1" in DOCKERFILE.read_text(encoding="utf-8")


def test_dockerfile_points_writable_caches_at_home():
    """Only $HOME and /tmp are writable on a Space; these all default elsewhere."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    for variable in ("HOME=", "HF_HOME=", "MPLCONFIGDIR="):
        assert variable in text, f"{variable} is unset, so a runtime write will fail"


def test_dockerignore_excludes_secrets_and_research_data():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for pattern in (".env", "data/", "models/", ".venv/"):
        assert pattern in ignore, f"{pattern} would be copied into an image layer"


# ==========================================================================
# The Space dependency set
# ==========================================================================
def test_site_requirements_exclude_the_offline_pipeline_stack():
    """The brief's list: no torch, autogluon, imdlib or xarray on the website."""
    pins = _pins(SITE_REQS)
    for package in ("torch", "autogluon.timeseries", "imdlib", "xarray", "netcdf4",
                    "cdsapi", "scikit-learn", "joblib", "matplotlib"):
        assert package not in pins, f"{package} is not needed to serve the site"


def test_dev_requirements_include_the_site_set_and_the_research_stack():
    """One install on the laptop has to give both, or the two drift apart."""
    text = DEV_REQS.read_text(encoding="utf-8")
    assert "-r requirements.txt" in text
    pins = _pins(DEV_REQS)
    for package in ("torch", "autogluon.timeseries", "xarray", "imdlib",
                    "scikit-learn", "matplotlib"):
        assert package in pins, f"{package} belongs in the laptop file"


def test_site_requirements_cover_everything_the_app_imports():
    pins = _pins(SITE_REQS)
    for package in ("streamlit", "streamlit-folium", "folium", "geopandas", "shapely",
                    "pandas", "numpy", "lightgbm", "pyarrow", "sqlalchemy",
                    "psycopg2-binary", "bcrypt", "pyyaml", "python-dotenv",
                    "requests", "google-genai", "twilio"):
        assert package in pins, f"{package} is imported at runtime but not pinned"


def test_site_and_research_pins_never_disagree():
    """One package at two versions means the Space runs untested code."""
    research = _pins(DEV_REQS)
    space = _pins(SITE_REQS)
    clashes = {name: (research[name], space[name])
               for name in set(research) & set(space)
               if research[name] != space[name]}
    assert not clashes, f"version conflicts: {clashes}"


def test_chronos_layer_uses_the_cpu_wheel_index():
    text = (ROOT / "requirements-chronos.txt").read_text(encoding="utf-8")
    assert "download.pytorch.org/whl/cpu" in text
    assert "+cpu" in text, "the default index would pull the ~2.5 GB CUDA build"


# ==========================================================================
# No GDAL on the app path
# ==========================================================================
@pytest.mark.parametrize("module", [
    "app.common",
    "app.theme",
    "app.public_data",
    "app.components.public_map",
    "src.jobs.weather",
    "src.jobs.weather_grid",
    "src.db.init",
    "src.db.session",
    "src.pipeline.run",
    "src.artifacts",
])
def test_app_modules_do_not_import_geopandas_at_module_level(module):
    """A top-level geopandas import would break the slim image at startup."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec(module)
    assert spec and spec.origin
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    top_level = []
    for node in tree.body:                      # module level only, not inside a def
        if isinstance(node, ast.Import):
            top_level += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    offenders = [n for n in top_level
                 if n.split(".")[0] in {"xarray", "torch", "autogluon", "netCDF4",
                                        "imdlib", "cdsapi", "sklearn", "joblib",
                                        "matplotlib"}]
    assert not offenders, f"{module} imports {offenders} at import time"


# ==========================================================================
# Map assets
# ==========================================================================
def test_map_layers_exist_and_stay_under_ten_megabytes():
    layers = ASSETS / "map_layers_simplified.gpkg"
    assert layers.is_file(), "run python scripts/build_map_assets.py"
    total = sum(p.stat().st_size
                for p in list(ASSETS.glob("*.gpkg")) + list(ASSETS.glob("*.geojson")))
    assert total < 10e6, f"map layers total {total / 1e6:.1f} MB, over the 10 MB cap"


def test_map_layer_can_be_read_one_state_at_a_time():
    """The memory guarantee: a state read must not pull all 739 shapes."""
    import geopandas as gpd

    layers = ASSETS / "map_layers_simplified.gpkg"
    bihar = gpd.read_file(layers, layer="units", where="state = 'Bihar'")
    assert 0 < len(bihar) < 739
    assert set(bihar["state"]) == {"Bihar"}
    for column in ("unit_id", "centroid_lat", "centroid_lon", "is_pilot"):
        assert column in bihar.columns


def test_pilot_asset_holds_the_30_pilot_units_with_centroids():
    payload = json.loads((ASSETS / "pilot_units.geojson").read_text(encoding="utf-8"))
    features = payload["features"]
    assert len(features) == 30, "CLAUDE.md §6: the 8 pilot districts hold 30 units"

    unit_ids = {f["properties"]["unit_id"] for f in features}
    assert len(unit_ids) == 30, "duplicate unit_id in the asset"

    for feature in features:
        properties = feature["properties"]
        # Precomputed so the app never reprojects, and inside the project BBOX
        # (CLAUDE.md §2: lat 15.5-31, lon 72.5-88.5).
        assert 15.0 <= properties["centroid_lat"] <= 31.5, properties
        assert 72.0 <= properties["centroid_lon"] <= 89.0, properties
        assert feature["geometry"]["coordinates"]


def test_every_seeded_unit_has_geometry():
    """The Risk Map silently omits a unit with no feature, so they must agree."""
    from src.db.init import load_pilot_units

    payload = json.loads((ASSETS / "pilot_units.geojson").read_text(encoding="utf-8"))
    drawn = {f["properties"]["unit_id"] for f in payload["features"]}
    seeded = set(load_pilot_units()["unit_id"])
    assert seeded <= drawn, f"seeded but not drawable: {sorted(seeded - drawn)}"


# ==========================================================================
# Database portability
# ==========================================================================
@pytest.mark.parametrize("raw,expected", [
    ("postgres://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
    ("postgresql://u:p@h:5432/db", "postgresql+psycopg2://u:p@h:5432/db"),
    ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
    ("sqlite:///data/monsoon.db", "sqlite:///data/monsoon.db"),
])
def test_provider_urls_are_normalised(raw, expected):
    """Render and Neon hand out `postgres://`, which SQLAlchemy 2.x rejects."""
    from src.db.session import normalize_url

    assert normalize_url(raw) == expected


def test_database_url_selects_postgres_and_falls_back_to_sqlite(monkeypatch):
    from src.db import session as session_module

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    assert session_module.is_postgres() is True

    monkeypatch.setenv("DATABASE_URL", "")
    assert session_module.is_postgres() is False
    assert session_module.database_url().startswith("sqlite:///")


def test_describe_never_leaks_the_password(monkeypatch):
    """It is printed by db.init and shown on the Models page."""
    from src.db.session import describe

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgres://alice:sup3rs3cret@db.example:5432/m")
    text = describe()
    assert "sup3rs3cret" not in text
    assert "Postgres" in text


def test_migration_scrubs_credentials_from_an_error():
    """A driver error can quote the whole DSN; the console must not."""
    from src.db.migrate_to_postgres import scrub

    url = "postgresql+psycopg2://alice:sup3rs3cret@db.example:5432/monsoon"
    message = f"OperationalError: could not connect to {url}"
    cleaned = scrub(message, url)
    assert "sup3rs3cret" not in cleaned
    assert "alice" not in cleaned
    assert "db.example" not in cleaned


def test_timestamps_are_stored_as_naive_utc_on_any_backend():
    """SQLite drops the offset and Postgres discards it; both then keep wall time."""
    import datetime as dt

    from src.db.models import UTCDateTime

    column = UTCDateTime()
    plus_530 = dt.datetime(2026, 6, 20, 17, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    stored = column.process_bind_param(plus_530, None)
    assert stored == dt.datetime(2026, 6, 20, 12, 0), "not converted to UTC"
    assert stored.tzinfo is None, "a naive column must receive a naive value"

    naive = dt.datetime(2026, 6, 20, 12, 0)
    assert column.process_bind_param(naive, None) == naive
    assert column.process_bind_param(None, None) is None


def test_migration_copies_every_table_in_dependency_order(tmp_path):
    """Rehearsed against SQLite: a message must never precede its subscriber."""
    import datetime as dt

    from sqlalchemy import create_engine

    from src.db.migrate_to_postgres import copy_table
    from src.db.models import (Advisory, Base, ForecastRun, Message, Subscriber,
                               Unit)
    from src.db.session import session_scope

    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"

    with session_scope(source_path) as session:
        from src.db.session import create_all

        create_all(source_path)
        session.add(Unit(unit_id="U1", unit_name="Testpur", district="Gaya",
                         state="Bihar", is_pilot=True))
        session.flush()
        run = ForecastRun(as_of=dt.date(2024, 6, 19),
                          requested_as_of=dt.date(2024, 6, 19), n_units=1)
        session.add(run)
        session.add(Subscriber(name="Test Farmer", phone="+919999900001",
                               language="en", unit_id="U1", district="Gaya",
                               state="Bihar", consent=True))
        session.flush()
        session.add(Advisory(run_id=run.id, unit_id="U1", as_of=dt.date(2024, 6, 19),
                             action="IRRIGATE_MULCH", confidence="high",
                             status="approved", text_en="Irrigate now.",
                             source="template"))

    source_engine = create_engine(f"sqlite:///{source_path}", future=True)
    target_engine = create_engine(f"sqlite:///{target_path}", future=True)
    Base.metadata.create_all(target_engine)

    counts = {t.name: copy_table(source_engine, target_engine, t)
              for t in Base.metadata.sorted_tables}

    assert counts["units"] == 1
    assert counts["subscribers"] == 1
    assert counts["advisories"] == 1
    assert counts["messages"] == 0

    from sqlalchemy import func, select

    with target_engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(Message.__table__)).scalar() == 0
        assert connection.execute(
            select(Advisory.__table__.c.text_en)).scalar() == "Irrigate now."

    source_engine.dispose()
    target_engine.dispose()


# ==========================================================================
# Model artifacts
# ==========================================================================
def test_artifact_status_reports_what_is_missing(tmp_path, monkeypatch):
    from src import artifacts

    monkeypatch.setattr(artifacts, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(artifacts, "DATA_PROCESSED", tmp_path / "processed")
    state = artifacts.status()
    assert state.present is False
    assert "0/12 boosters" in state.detail
    assert "train_table.parquet" in state.detail


def test_missing_repo_and_missing_files_is_a_loud_error(tmp_path, monkeypatch):
    """Silence here produces a Space whose Run button fails deep in the pipeline."""
    from src import artifacts
    from src.report import DataError

    monkeypatch.setattr(artifacts, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(artifacts, "DATA_PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(artifacts, "repo_id", lambda: None)

    with pytest.raises(DataError, match="HF_MODEL_REPO"):
        artifacts.ensure_artifacts(quiet=True)


def test_present_artifacts_need_no_network(monkeypatch):
    """A local checkout must not call the Hub on every start."""
    import huggingface_hub

    def explode(*_a, **_k):
        raise AssertionError("snapshot_download was called though the files are here")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", explode)
    from src import artifacts

    if not artifacts.status().present:
        pytest.skip("models are not trained in this checkout")
    assert artifacts.ensure_artifacts(quiet=True).present


# ==========================================================================
# The Chronos gate
# ==========================================================================
def test_chronos_is_off_unless_explicitly_enabled(monkeypatch):
    from src.runtime import chronos_enabled

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.delenv("ENABLE_CHRONOS", raising=False)
    assert chronos_enabled() is False

    monkeypatch.setenv("ENABLE_CHRONOS", "true")
    assert chronos_enabled() is True


def test_the_measured_chronos_cost_is_inside_the_budget():
    """Recorded so the claim in the README is checkable, not folklore."""
    from src.runtime import CHRONOS_CPU_BUDGET_SECONDS, CHRONOS_CPU_SECONDS

    assert CHRONOS_CPU_SECONDS < CHRONOS_CPU_BUDGET_SECONDS


# ==========================================================================
# Streamlit Community Cloud: entry point and secrets
# ==========================================================================
def test_the_entry_file_is_app_main_py():
    """Streamlit Cloud is configured with a single main-file path."""
    main = ROOT / "app" / "main.py"
    assert main.is_file()
    text = main.read_text(encoding="utf-8")
    assert "st.navigation" in text
    # Streamlit runs the entry file as a script, not a package module, so it has to
    # put the project root on sys.path before importing anything from src/.
    assert "sys.path.insert" in text


def test_secrets_toml_is_gitignored_and_assets_are_not():
    """`--no-index` is what makes this meaningful.

    Without it, `git check-ignore` skips files that are already tracked, so a rule that
    would drop a file from a *fresh clone* still looks fine here. That is exactly how
    `data/` nearly swallowed `data/SOURCES.md`: git cannot re-include a file once a
    parent directory is excluded, and the tracked copy hid it locally.
    """
    import subprocess

    def ignored(path: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", "--no-index", path],
                              cwd=ROOT).returncode == 0

    for path in (".streamlit/secrets.toml", ".env", "data/monsoon.db",
                 "data/processed/train_table.parquet", "models/lgbm/dry_14.txt"):
        assert ignored(path), f"{path} must never be committed"

    for path in (
        # The site runs on these; *.parquet would otherwise catch the feature table.
        "app/assets/models/features.parquet",
        "app/assets/map_layers_simplified.gpkg",
        "app/assets/models/dry_14.txt",
        "app/assets/models/dry_14.calib.json",
        ".streamlit/secrets.toml.example",
        # Provenance and directory layout, committed since Phase 1.
        "data/SOURCES.md",
        "data/raw/.gitkeep",
        "data/processed/.gitkeep",
    ):
        assert not ignored(path), f"{path} is needed in the repo but is ignored"


def test_config_reads_env_and_secrets_under_the_same_names(monkeypatch):
    from src import config

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    config.reset_cache()

    monkeypatch.setattr(config, "_secrets", lambda: {"DATABASE_URL": "from-secrets"})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert config.get("DATABASE_URL") == "from-secrets"

    # A real environment variable always wins, so a shell can override a deploy.
    monkeypatch.setenv("DATABASE_URL", "from-environment")
    assert config.get("DATABASE_URL") == "from-environment"


def test_config_survives_having_no_secrets_file(monkeypatch):
    """Touching st.secrets raises when there is no file; that means 'unset'."""
    from src import config

    class Boom:
        def __iter__(self):
            raise FileNotFoundError("no secrets.toml")

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    config.reset_cache()
    import streamlit as st

    monkeypatch.setattr(st, "secrets", Boom())
    assert config._secrets() == {}
    config.reset_cache()


def test_hosted_flag_is_explicit(monkeypatch):
    from src import config

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setattr(config, "_secrets", lambda: {})
    for name in ("HOSTED_MODE", "STREAMLIT_RUNTIME_ENV", "HOSTNAME"):
        monkeypatch.delenv(name, raising=False)
    assert config.hosted() is False

    monkeypatch.setenv("HOSTED_MODE", "true")
    assert config.hosted() is True
    monkeypatch.setenv("HOSTED_MODE", "false")
    assert config.hosted() is False


def test_config_describe_never_reveals_a_value(monkeypatch):
    from src import config

    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setattr(config, "_secrets", lambda: {})
    monkeypatch.setenv("SARVAM_API_KEY", "sk-do-not-print-me")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:hunter2@h/db")

    reported = config.describe()
    blob = " ".join(f"{k}={v}" for k, v in reported.items())
    assert "do-not-print-me" not in blob
    assert "hunter2" not in blob
    assert reported["SARVAM_API_KEY"] == "set"
    assert reported["DATABASE_URL"] == "set"


# ==========================================================================
# The committed model assets
# ==========================================================================
def test_committed_assets_are_complete():
    from src import hosted_models

    assert hosted_models.available(), (
        "run python scripts/export_models.py; the hosted site has no other models")
    info = hosted_models.manifest()
    assert info["calibration"] == "isotonic_cv", (
        "CLAUDE.md 13: cross-fitted isotonic is the calibration Phase 3 settled on")
    assert len(info["calibrated"]) == 12
    assert len(info["feature_columns"]) > 20


def test_committed_assets_and_trained_models_agree_exactly():
    """The whole point of the export: the site must not compute different numbers."""
    import pandas as pd

    from src import hosted_models
    from src.baseline_common import LGBM_DIR, TRAIN_TABLE, feature_columns, load_table
    from src.pipeline.run import predict as trained_predict

    if not TRAIN_TABLE.is_file():
        pytest.skip("no locally trained feature table in this checkout")
    if not (LGBM_DIR / "dry_14_isotonic_cv.joblib").is_file():
        pytest.skip("no trained calibrators in this checkout")

    table = load_table()
    date = pd.Timestamp("2024-06-19")
    day = table[table["start_date"] == date]
    if day.empty:
        pytest.skip("2024-06-19 is not in this feature table")

    trained = trained_predict(day, feature_columns(table))

    hosted_table = hosted_models.load_table()
    hosted_day = hosted_table[hosted_table["start_date"] == date]
    committed = hosted_models.predict(hosted_day)

    merged = trained.merge(committed, on=["unit_id", "hazard", "horizon"],
                           suffixes=("_trained", "_committed"))
    assert len(merged) == 360
    largest = (merged["probability_trained"] -
               merged["probability_committed"]).abs().max()
    assert largest == 0.0, f"committed assets differ by {largest:.2e}"


def test_isotonic_breakpoints_reproduce_sklearn():
    """numpy.interp must equal IsotonicRegression.predict, or calibration drifts."""
    import json

    import numpy as np

    joblib = pytest.importorskip("joblib")
    from src.baseline_common import LGBM_DIR
    from src.hosted_models import ASSETS, calibrate

    source = LGBM_DIR / "dry_14_isotonic_cv.joblib"
    exported = ASSETS / "dry_14.calib.json"
    if not source.is_file() or not exported.is_file():
        pytest.skip("calibrators are not built in this checkout")

    estimator = joblib.load(source)
    breakpoints = json.loads(exported.read_text(encoding="utf-8"))
    # Deliberately probes outside [x_min, x_max], where the 'clip' behaviour matters.
    probe = np.linspace(0.0, 1.0, 141)
    assert np.array_equal(estimator.predict(probe), calibrate(probe, breakpoints))


def test_hosted_inference_needs_no_sklearn_or_joblib():
    """Neither is in requirements.txt, so an import at module scope would break."""
    import ast

    source = (ROOT / "src" / "hosted_models.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    for banned in ("sklearn", "joblib", "torch", "geopandas"):
        assert banned not in names, f"src/hosted_models.py imports {banned}"


# ==========================================================================
# Writing to another database
# ==========================================================================
def test_db_url_override_redirects_every_later_session(tmp_path):
    from src.db import session as session_module

    target = tmp_path / "elsewhere.db"
    try:
        session_module.use_url(f"sqlite:///{target}")
        assert session_module.database_url().endswith("elsewhere.db")
        session_module.create_all()
        assert target.is_file()
    finally:
        session_module.use_url(None)
    assert not session_module.database_url().endswith("elsewhere.db")


def test_db_url_override_normalises_a_provider_scheme():
    from src.db import session as session_module

    try:
        session_module.use_url("postgres://u:p@h:5432/db")
        assert session_module.database_url().startswith("postgresql+psycopg2://")
        assert session_module.is_postgres() is True
    finally:
        session_module.use_url(None)


def test_pipeline_accepts_a_db_url_and_an_env_name():
    """--db-url-env keeps the connection string out of the process list."""
    import inspect

    from src.pipeline import run

    source = inspect.getsource(run.main)
    assert "--db-url" in source
    assert "--db-url-env" in source
    # Passing both is ambiguous about which database gets written.
    assert "not both" in source


def test_pipeline_main_accepts_argv_for_in_process_calls():
    """The hosted Run button calls this directly rather than forking a second Python."""
    import inspect

    from src.pipeline import run

    assert "argv" in inspect.signature(run.main).parameters


# ==========================================================================
# Public dashboard: static map files, weather jobs, workflows
# ==========================================================================
STATIC = ROOT / "app" / "static"
WEATHER_REQS = ROOT / "requirements-weather.txt"


def test_weather_job_pins_match_the_site():
    """The Actions job must run the same library versions the site was tested with."""
    site, job = _pins(SITE_REQS), _pins(WEATHER_REQS)
    assert job, "requirements-weather.txt pins nothing"
    drift = {k: (v, site.get(k)) for k, v in job.items() if site.get(k) != v}
    assert not drift, f"requirements-weather.txt drifts from requirements.txt: {drift}"


def test_public_map_layers_stay_under_five_megabytes():
    """The brief: the national layer and each state's file under 5 MB."""
    national = STATIC / "india_states.geojson"
    assert national.is_file(), "run python scripts/build_public_map_assets.py"
    assert national.stat().st_size < 5e6
    for path in STATIC.glob("*_*.geojson"):
        assert path.stat().st_size < 5e6, f"{path.name} is {path.stat().st_size / 1e6:.1f} MB"


def test_every_covered_state_has_district_and_unit_files():
    meta = json.loads((STATIC / "boundary_source.json").read_text(encoding="utf-8"))
    assert len(meta["covered"]) == 5
    for info in meta["covered"].values():
        assert (STATIC / f"units_{info['key']}.geojson").is_file()
        assert (STATIC / f"districts_{info['key']}.geojson").is_file()


def test_map_geometry_is_plain_polygons():
    """GeometryCollections (from make_valid or GDAL rounding) broke the map once."""
    for name in ("india_states", "india_outline"):
        data = json.loads((STATIC / f"{name}.geojson").read_text(encoding="utf-8"))
        kinds = {f["geometry"]["type"] for f in data["features"]}
        assert kinds <= {"Polygon", "MultiPolygon"}, f"{name}: {kinds}"


def test_animation_data_stays_under_five_megabytes():
    total = sum(p.stat().st_size for p in (STATIC / n for n in
                ("onset_replay.json", "wind.json", "rain_forecast.json")) if p.is_file())
    assert total < 5e6, f"animation data is {total / 1e6:.1f} MB"


def test_runtime_grid_files_are_not_committed():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "app/static/wind.json" in ignore and "app/static/rain_forecast.json" in ignore


@pytest.mark.parametrize("workflow", ["weather.yml", "weather-grid.yml"])
def test_workflows_read_the_database_url_by_name_only(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    front = yaml.safe_load(text)
    assert front.get("permissions", {}).get("contents") == "read"
    assert "--db-url-env DATABASE_URL_CLOUD" in text
    assert "requirements-weather.txt" in text
    assert "postgres" not in text.lower(), "a connection string in the workflow"


def test_docker_image_carries_the_streamlit_config():
    """Without config.toml there is no static serving, so no map files."""
    assert ".streamlit/config.toml" in DOCKERFILE.read_text(encoding="utf-8")


def test_partner_logo_appears_only_when_the_file_exists(tmp_path, monkeypatch):
    """Nothing is drawn or downloaded in place of a ministry logo: the footer shows one
    only if someone has put app/static/partners/moes-logo.png there by hand."""
    import app.theme as theme

    rendered: list[str] = []
    monkeypatch.setattr(theme.st, "html", lambda body, **kw: rendered.append(body))
    monkeypatch.setattr(theme, "PARTNER_LOGO", tmp_path / "moes-logo.png")
    theme.footer()
    assert "moes-logo.png" not in rendered[-1]
    (tmp_path / "moes-logo.png").write_bytes(b"not-a-real-png")
    theme.footer()
    assert "partners/moes-logo.png" in rendered[-1]
    assert "Smart India Hackathon" in rendered[-1] and "not an official government" in rendered[-1]
