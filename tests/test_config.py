"""Guards on the config files: they must parse, and the splits must not leak."""

from pathlib import Path

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config"


def _load(name: str) -> dict:
    with (CONFIG / name).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize("name", ["project.yaml", "onset.yaml", "zones.yaml"])
def test_config_parses(name):
    assert isinstance(_load(name), dict)


def test_splits_do_not_overlap():
    splits = _load("project.yaml")["splits"]
    train_lo, train_hi = splits["train"]
    val_lo, val_hi = splits["validate"]
    test_lo, test_hi = splits["test"]

    assert train_lo < train_hi < val_lo <= val_hi < test_lo
    assert test_hi is None, "test split is open-ended: through the latest IMD year"


def test_climatology_uses_training_years_only():
    cfg = _load("project.yaml")
    assert cfg["climatology"]["years"] == cfg["splits"]["train"]


def test_bbox_covers_all_pilot_states():
    bbox = _load("project.yaml")["bbox"]
    # Latur (18.4N, 76.6E) is the southernmost pilot, Gorakhpur (26.8N, 83.4E) the
    # north-easternmost; the box must comfortably contain the 5 regions.
    assert bbox["lat_min"] == 15.5 and bbox["lat_max"] == 31.0
    assert bbox["lon_min"] == 72.5 and bbox["lon_max"] == 88.5


def test_onset_default_threshold():
    d = _load("onset.yaml")["defaults"]
    assert d["accum_mm"] == 20.0
    assert d["accum_days"] == 3


def test_language_map_matches_regions():
    cfg = _load("project.yaml")
    assert set(cfg["languages"]) == set(cfg["regions"])
    assert cfg["languages"]["Maharashtra"][0] == "mr"
    for region in ["Uttar Pradesh", "NCT of Delhi", "Bihar", "Madhya Pradesh"]:
        assert cfg["languages"][region][0] == "hi"
    for langs in cfg["languages"].values():
        assert "en" in langs


def test_pilots_carry_verified_gadm_names():
    pilots = _load("project.yaml")["pilots"]
    assert set(pilots) == {"Uttar Pradesh", "Bihar", "Madhya Pradesh", "Maharashtra"}
    assert "NCT of Delhi" not in pilots, "Delhi appears only in full runs"
    for state, units in pilots.items():
        assert len(units) == 2, f"{state} must have exactly 2 pilots"
        for unit in units:
            assert unit["gadm_name_2"], f"{unit} is missing its verified GADM NAME_2"
    # GADM spells Beed "Bid"; this is the one pilot whose GADM name differs.
    beed = next(u for u in pilots["Maharashtra"] if u["name"] == "Beed")
    assert beed["gadm_name_2"] == "Bid"
