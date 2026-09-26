"""The area-weight matrix must equal a slow, independent area-weighted average.

`build_unit_rain` gets its speed from one matrix multiply per year instead of clipping
polygons every day. That is only legitimate if the result is identical to the obvious
slow computation, so this recomputes a few pilot units the long way - clip the polygon
against the grid, weight each cell by its own intersection area - and compares.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
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

pytestmark = pytest.mark.skipif(
    not (WEIGHTS_NPZ.is_file() and RAIN_GRID_NC.is_file() and UNITS_GPKG.is_file()),
    reason="Phase 1 outputs not built yet",
)


@pytest.fixture(scope="module")
def weights():
    npz = np.load(WEIGHTS_NPZ, allow_pickle=False)
    W = sp.csr_matrix(
        (npz["data"], npz["indices"], npz["indptr"]), shape=tuple(npz["shape"])
    )
    return {
        "W": W,
        "unit_id": [str(u) for u in npz["unit_id"]],
        "valid_cell": npz["valid_cell"],
        "small_unit": npz["small_unit"],
        "no_valid_cells": npz["no_valid_cells"],
        "unit_area_valid": npz["unit_area_valid"],
        "cell_coverage": npz["cell_coverage"],
    }


@pytest.fixture(scope="module")
def units():
    return gpd.read_file(UNITS_GPKG, layer="units")


def test_every_row_with_data_sums_to_one(weights):
    W = weights["W"]
    totals = np.asarray(W.sum(axis=1)).ravel()
    has_data = ~weights["no_valid_cells"]
    np.testing.assert_allclose(totals[has_data], 1.0, atol=1e-9)


def test_rows_without_observed_cells_sum_to_zero(weights):
    W = weights["W"]
    totals = np.asarray(W.sum(axis=1)).ravel()
    blank = weights["no_valid_cells"]
    if blank.any():
        np.testing.assert_allclose(totals[blank], 0.0, atol=0)


def test_no_weight_lands_on_a_cell_without_observations(weights):
    """Otherwise a coastal unit would be averaged against NaN."""
    W = weights["W"].tocoo()
    invalid = ~weights["valid_cell"]
    assert not invalid[W.col].any(), "a weight sits on a cell with no observations"


def test_weights_are_non_negative(weights):
    assert (weights["W"].data >= 0).all()


def test_matrix_multiply_matches_a_slow_per_day_area_average(weights, units):
    """The check that justifies the whole shortcut.

    Three pilot units, several days, recomputed by intersecting the polygon with the
    grid and weighting each cell by its own area - no cached matrix involved.
    """
    lat, lon = grid_subset()
    half = IMD_CELL_DEG / 2

    pilot_names = ["Jhansi", "Gaya", "Latur"]
    chosen = units[units["district"].isin(pilot_names)].groupby(
        "district", observed=True
    ).first().reset_index()
    assert len(chosen) == len(pilot_names), "pilot units not found in the unit layer"

    with xr.open_dataset(RAIN_GRID_NC) as ds:
        times = ds["time"].values
        # Spread the sample days across the record, inside the monsoon.
        picks = [t for t in times if str(t)[5:7] in ("07", "08")]
        sample = picks[:: max(1, len(picks) // 6)][:6]
        grids = {str(t)[:10]: ds["rain"].sel(time=t).values for t in sample}

    order = {uid: i for i, uid in enumerate(weights["unit_id"])}
    W = weights["W"]

    for row in chosen.itertuples():
        unit_ea = gpd.GeoSeries([row.geometry], crs=GEO_CRS).to_crs(EQUAL_AREA_CRS)

        for day, grid in grids.items():
            # --- slow path: intersect this polygon with every overlapping cell ---
            numerator = 0.0
            denominator = 0.0
            for i, y in enumerate(lat):
                for j, x in enumerate(lon):
                    value = grid[i, j]
                    if not np.isfinite(value):
                        continue          # no observation: cannot contribute
                    cell = gpd.GeoSeries(
                        [box(x - half, y - half, x + half, y + half)], crs=GEO_CRS
                    ).to_crs(EQUAL_AREA_CRS)
                    overlap = unit_ea.intersection(cell, align=False).area.iloc[0]
                    if overlap <= 0:
                        continue
                    numerator += overlap * float(value)
                    denominator += overlap
            assert denominator > 0, f"{row.unit_id} overlaps no observed cell"
            slow = numerator / denominator

            # --- fast path: the cached matrix ---
            # A sparse row times a dense vector yields a 1-element array.
            fast = float(
                (W[order[row.unit_id]] @ np.nan_to_num(grid.reshape(-1), nan=0.0))[0]
            )

            assert slow == pytest.approx(fast, rel=1e-4, abs=1e-4), (
                f"{row.district}/{row.unit_name} on {day}: "
                f"slow {slow:.6f} mm vs matrix {fast:.6f} mm"
            )


def test_small_unit_flag_means_smaller_than_its_grid_cell(weights, units):
    small = np.asarray(weights["small_unit"])
    order = {uid: i for i, uid in enumerate(weights["unit_id"])}
    idx = units["unit_id"].map(order).to_numpy()
    areas = units["area_km2"].to_numpy()
    # One 0.25 deg cell over this BBOX is 662-742 km2 (measured in EPSG:6933).
    flagged = small[idx]
    assert areas[flagged].max() < 745, "a unit larger than any grid cell is flagged small"
    assert flagged.sum() > 0, "no small units found, which contradicts the unit areas"


def test_coverage_areas_are_consistent_with_the_matrix(weights):
    """unit_area_valid and cell_coverage must come from the same intersections."""
    assert weights["unit_area_valid"].sum() == pytest.approx(
        weights["cell_coverage"].sum(), rel=1e-9
    )
