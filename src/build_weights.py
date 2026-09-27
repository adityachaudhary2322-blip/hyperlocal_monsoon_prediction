"""Build the sparse unit x grid-cell area-weight matrix, once.

Run:  python -m src.build_weights [--force]

This is the efficiency contract from CLAUDE.md section 10: unit rainfall is
`daily_grid @ W.T`, with W built here a single time and cached. Polygons are never
clipped per day.

Two things make the weights correct rather than merely plausible:

1. Areas are computed in an equal-area CRS (EPSG:6933). In plain degrees a cell at
   31 N would be weighted as if it were the same size as one at 15.5 N.
2. Cells with no rainfall observations (the -999 sentinel over sea and outside India)
   are zeroed *before* rows are renormalised. Without that step a coastal taluk would
   be averaged against NaN cells and silently biased.
"""

from __future__ import annotations

import argparse

import geopandas as gpd
import numpy as np
import scipy.sparse as sp
import xarray as xr
from shapely.geometry import box

from src.common import (
    EQUAL_AREA_CRS,
    GEO_CRS,
    IMD_CELL_DEG,
    RAIN_GRID_NC,
    UNITS_GPKG,
    WEIGHTS_NPZ,
    grid_subset,
)
from src.report import DataError, print_list, require_nonempty, summarize

ROW_SUM_TOL = 1e-9


def cell_frame(lat: np.ndarray, lon: np.ndarray,
               cell_deg: float = IMD_CELL_DEG) -> gpd.GeoDataFrame:
    """One polygon per grid cell, with cell_id = lat_index * n_lon + lon_index.

    That ordering must match the C-order flatten of the (lat, lon) rainfall grid.
    `cell_deg` defaults to the IMD grid; the live engine reuses this for its 0.5 and
    1.5 degree forecast grids.
    """
    half = cell_deg / 2
    ids, polys = [], []
    for i, y in enumerate(lat):
        for j, x in enumerate(lon):
            ids.append(i * lon.size + j)
            polys.append(box(x - half, y - half, x + half, y + half))
    return gpd.GeoDataFrame({"cell_id": ids}, geometry=polys, crs=GEO_CRS)


def valid_cell_mask(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Flat boolean mask of cells that ever carry a rainfall observation.

    Derived from the rainfall grid itself rather than a coastline: a cell counts as
    valid if it is finite on any sampled day.
    """
    if not RAIN_GRID_NC.is_file():
        raise DataError(
            f"missing {RAIN_GRID_NC}; run python -m src.build_rain_grid first "
            "(the valid-cell mask comes from the data, not from a coastline)"
        )
    with xr.open_dataset(RAIN_GRID_NC) as ds:
        if ds.sizes["lat"] != lat.size or ds.sizes["lon"] != lon.size:
            raise DataError(
                f"grid mismatch: {RAIN_GRID_NC.name} is "
                f"{ds.sizes['lat']}x{ds.sizes['lon']}, config implies "
                f"{lat.size}x{lon.size}"
            )
        # Sample across the record so a cell that is only wet in some years still
        # counts; one slice per ~2 years keeps this cheap.
        step = max(1, ds.sizes["time"] // 400)
        sample = ds["rain"].isel(time=slice(None, None, step))
        mask = np.isfinite(sample).any("time").values
    return mask.reshape(-1)


def build(units: gpd.GeoDataFrame, lat: np.ndarray, lon: np.ndarray,
          valid: np.ndarray, cell_deg: float = IMD_CELL_DEG):
    """Build the weight matrix.

    Returns (W, cells_per_unit, small_unit, unit_area_valid, cell_coverage) where W
    rows sum to 1 and the two area vectors are raw intersection areas in m^2.
    """
    cells = cell_frame(lat, lon, cell_deg)
    n_cells = lat.size * lon.size

    units_ea = units.to_crs(EQUAL_AREA_CRS)
    cells_ea = cells.to_crs(EQUAL_AREA_CRS)

    print(f"  intersecting {len(units)} units with {n_cells} cells "
          f"in {EQUAL_AREA_CRS}...")
    inter = gpd.overlay(
        units_ea[["unit_id", "geometry"]],
        cells_ea[["cell_id", "geometry"]],
        how="intersection",
        keep_geom_type=True,
    )
    inter["weight"] = inter.area
    print(f"  {len(inter):,} unit-cell overlaps")

    order = {uid: i for i, uid in enumerate(units["unit_id"])}
    rows = inter["unit_id"].map(order).to_numpy()
    cols = inter["cell_id"].to_numpy()
    W = sp.csr_matrix(
        (inter["weight"].to_numpy(), (rows, cols)),
        shape=(len(units), n_cells),
    )

    # Drop cells with no observations, THEN renormalise, so each row is a weighted
    # mean over the cells that actually hold data.
    keep = sp.diags(valid.astype(np.float64))
    W = (W @ keep).tocsr()
    W.eliminate_zeros()

    # Keep the raw intersection areas (m^2) before normalising. They make an exact
    # conservation identity testable downstream: the rainfall volume summed over
    # units must equal the volume summed over cells weighted by their coverage.
    area_matrix = W.copy()
    unit_area_valid = np.asarray(area_matrix.sum(axis=1)).ravel()
    cell_coverage = np.asarray(area_matrix.sum(axis=0)).ravel()

    totals = np.asarray(W.sum(axis=1)).ravel()
    scale = np.where(totals > 0, 1.0 / np.where(totals > 0, totals, 1.0), 0.0)
    W = (sp.diags(scale) @ W).tocsr()

    cells_per_unit = np.diff(W.indptr)

    # A unit is "small" when it is smaller than the grid cell containing its
    # centroid - i.e. its value comes from a cell bigger than the unit itself.
    centroids = units_ea.geometry.centroid
    cell_area = cells_ea.set_index("cell_id").area
    containing = gpd.sjoin(
        gpd.GeoDataFrame(geometry=centroids, crs=EQUAL_AREA_CRS),
        cells_ea[["cell_id", "geometry"]],
        how="left", predicate="within",
    )
    containing = containing[~containing.index.duplicated()].reindex(units_ea.index)
    ref_area = containing["cell_id"].map(cell_area).to_numpy()
    small_unit = units["area_km2"].to_numpy() * 1e6 < ref_area

    return W, cells_per_unit, small_unit, unit_area_valid, cell_coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if WEIGHTS_NPZ.is_file() and not args.force:
        print(f"{WEIGHTS_NPZ} exists; pass --force to rebuild")
        return 0

    print("--- build_weights ---")
    if not UNITS_GPKG.is_file():
        raise DataError(f"missing {UNITS_GPKG}; run python -m src.build_units first")
    units = gpd.read_file(UNITS_GPKG, layer="units")

    lat, lon = grid_subset()
    valid = valid_cell_mask(lat, lon)
    print(f"  grid {lat.size} x {lon.size} = {lat.size * lon.size} cells, "
          f"{int(valid.sum())} with observations "
          f"({100 * valid.mean():.1f}%)")

    W, cells_per_unit, small_unit, unit_area_valid, cell_coverage = build(
        units, lat, lon, valid)
    require_nonempty(W.data, "weight matrix")

    # Every unit that overlaps at least one observed cell must have a row summing to 1.
    totals = np.asarray(W.sum(axis=1)).ravel()
    has_data = cells_per_unit > 0
    bad = np.nonzero(has_data & (np.abs(totals - 1.0) > ROW_SUM_TOL))[0]
    if bad.size:
        raise DataError(
            f"{bad.size} row(s) do not sum to 1, e.g. unit "
            f"{units['unit_id'].iloc[bad[0]]} sums to {totals[bad[0]]!r}"
        )

    no_data = ~has_data
    if no_data.any():
        print()
        print_list(
            "units with NO observed grid cell (rainfall will be NaN)",
            [f"{r.state}/{r.district}/{r.unit_name} ({r.area_km2:.3f} km2)"
             for r in units[no_data].itertuples()],
            limit=20,
        )

    WEIGHTS_NPZ.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        WEIGHTS_NPZ,
        data=W.data.astype(np.float64),
        indices=W.indices,
        indptr=W.indptr,
        shape=np.array(W.shape),
        unit_id=units["unit_id"].to_numpy().astype("U32"),
        lat=lat,
        lon=lon,
        valid_cell=valid,
        small_unit=small_unit,
        no_valid_cells=no_data,
        unit_area_valid=unit_area_valid,
        cell_coverage=cell_coverage,
    )
    require_nonempty(WEIGHTS_NPZ, "weights.npz")

    summarize(
        "build_weights",
        rows=int(W.nnz),
        files=[WEIGHTS_NPZ],
        extra={
            "matrix": f"{W.shape[0]} units x {W.shape[1]} cells",
            "density": f"{100 * W.nnz / (W.shape[0] * W.shape[1]):.4f}%",
            "row sums": f"min {totals[has_data].min():.9f} "
                        f"max {totals[has_data].max():.9f}",
            "cells/unit": f"min {cells_per_unit[has_data].min()} "
                          f"median {int(np.median(cells_per_unit[has_data]))} "
                          f"max {cells_per_unit.max()}",
            "small_unit": int(small_unit.sum()),
            "no_valid_cells": int(no_data.sum()),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
