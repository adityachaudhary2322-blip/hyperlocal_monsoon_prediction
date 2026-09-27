"""Readers for the committed live assets in app/assets/live/ (scripts/build_live_assets.py)."""

from __future__ import annotations

import functools
import json

import numpy as np
import pandas as pd
import scipy.sparse as sp

from src.common import ROOT
from src.report import DataError

ASSETS = ROOT / "app" / "assets" / "live"


def _npz(name: str):
    path = ASSETS / name
    if not path.is_file():
        raise DataError(f"missing {path}; run python scripts/build_live_assets.py")
    return np.load(path, allow_pickle=False)


@functools.lru_cache(maxsize=None)
def grid(tag: str) -> dict:
    """{'lat','lon' (points,), 'W' (units x points csr), 'unit_id'} for tag '05' or '15'."""
    z = _npz(f"grid_{tag}.npz")
    W = sp.csr_matrix((z["data"], z["indices"], z["indptr"]), shape=tuple(z["shape"]))
    return {"lat": z["lat"], "lon": z["lon"], "W": W, "unit_id": z["unit_id"],
            "step": float(z["step"])}


@functools.lru_cache(maxsize=None)
def imd_weights() -> dict:
    z = _npz("weights_imd.npz")
    W = sp.csr_matrix((z["data"], z["indices"], z["indptr"]), shape=tuple(z["shape"]))
    return {"W": W, "unit_id": z["unit_id"], "no_data": z["no_valid_cells"]}


@functools.lru_cache(maxsize=None)
def units() -> pd.DataFrame:
    path = ASSETS / "units.csv"
    if not path.is_file():
        raise DataError(f"missing {path}; run python scripts/build_live_assets.py")
    return pd.read_csv(path)


@functools.lru_cache(maxsize=None)
def climatology() -> dict:
    z = _npz("climatology.npz")
    return {k: z[k] for k in z.files}


@functools.lru_cache(maxsize=None)
def district_crops() -> dict:
    return json.loads((ASSETS / "district_crops.json").read_text(encoding="utf-8"))


def check_alignment() -> None:
    """Every asset must list units in the same order, or values land on the wrong unit."""
    ids = units()["unit_id"].astype(str).tolist()
    for name, other in (("grid_05", grid("05")["unit_id"]), ("grid_15", grid("15")["unit_id"]),
                        ("weights_imd", imd_weights()["unit_id"]),
                        ("climatology", climatology()["unit_id"])):
        if [str(u) for u in other] != ids:
            raise DataError(f"{name} unit order differs from units.csv; rebuild live assets")


def week_index(doy: int) -> int:
    """Climatology week whose 7-day window covers day-of-year `doy`."""
    return int(min(max((doy - 1) // 7, 0), climatology()["mean"].shape[0] - 1))
