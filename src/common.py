"""Shared constants, paths and config loading.

Every pipeline module imports from here so the BBOX, the IMD grid definition and the
config file locations are defined exactly once (CLAUDE.md §2, §12).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]

CONFIG_DIR = ROOT / "config"
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
MODELS_DIR = ROOT / "models"
OUTPUTS_DIR = ROOT / "outputs"
FIGS_DIR = OUTPUTS_DIR / "figs"
METRICS_DIR = OUTPUTS_DIR / "metrics"

IMD_RAW = DATA_RAW / "imd"
BOUNDARIES_RAW = DATA_RAW / "boundaries"

# Phase 1 outputs
RAIN_GRID_NC = DATA_PROCESSED / "imd_rain.nc"
UNITS_GPKG = DATA_PROCESSED / "units.gpkg"
WEIGHTS_NPZ = DATA_PROCESSED / "weights.npz"
UNIT_RAIN_PARQUET = DATA_PROCESSED / "unit_rain.parquet"
INDICES_PARQUET = DATA_PROCESSED / "indices.parquet"
SOURCES_MD = ROOT / "data" / "SOURCES.md"

# --- IMD 0.25 deg gridded rainfall geometry ----------------------------------
# From imdlib.core (verified 2026-09-26): the binary .grd files are a fixed
# 129 x 135 float32 grid. Do not change these without re-reading imdlib.core.
IMD_LAT_SIZE = 129
IMD_LON_SIZE = 135
IMD_LAT = np.linspace(6.5, 38.5, IMD_LAT_SIZE)
IMD_LON = np.linspace(66.5, 100.0, IMD_LON_SIZE)
IMD_CELL_DEG = 0.25

# One float32 per cell per day. Used to validate downloads byte-exactly.
IMD_BYTES_PER_DAY = IMD_LAT_SIZE * IMD_LON_SIZE * 4  # 69_660

# Equal-area CRS for every area computation. Plain degrees would bias area
# weights by latitude, so never compute area in EPSG:4326.
EQUAL_AREA_CRS = "EPSG:6933"
GEO_CRS = "EPSG:4326"


@functools.lru_cache(maxsize=None)
def load_config(name: str = "project") -> dict[str, Any]:
    """Load config/<name>.yaml. Cached — configs are read-only at runtime."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} did not parse to a mapping")
    return cfg


def bbox() -> dict[str, float]:
    """The project bounding box. Clip every gridded read to this FIRST."""
    return load_config("project")["bbox"]


def regions() -> list[str]:
    """The 5 target NAME_1 values, exactly as they appear in the GADM files."""
    return list(load_config("project")["regions"])


def pilot_districts() -> dict[str, list[str]]:
    """state -> [verified GADM NAME_2, ...] for the pilot districts.

    Resolved through the verified table in config/project.yaml, never by fuzzy
    matching: GADM spells Beed "Bid" (CLAUDE.md §5).
    """
    return {
        state: [u["gadm_name_2"] for u in units]
        for state, units in load_config("project")["pilots"].items()
    }


def grid_subset() -> tuple[np.ndarray, np.ndarray]:
    """(lat, lon) of the IMD grid clipped to the project BBOX.

    63 x 65 = 4095 cells, down from 129 x 135 = 17415 (CLAUDE.md §2).
    """
    bb = bbox()
    lat = IMD_LAT[(IMD_LAT >= bb["lat_min"]) & (IMD_LAT <= bb["lat_max"])]
    lon = IMD_LON[(IMD_LON >= bb["lon_min"]) & (IMD_LON <= bb["lon_max"])]
    return lat, lon


def splits() -> dict[str, list]:
    """train / validate / test year ranges. test[1] is None = open-ended."""
    return load_config("project")["splits"]
