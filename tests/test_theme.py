"""Design tokens: WCAG contrast in both themes, and config.toml kept in step.

Ratios use the WCAG 2.x relative-luminance formula. Text pairs need 4.5:1; large text,
icons and non-text marks (map fills against their outline, the focus ring) need 3:1.
"""

from __future__ import annotations

import tomllib

import pytest

from app.theme import BRAND_COLOURS, TOKENS
from src.common import ROOT


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


TEXT = ["ink", "muted", "primary", "accent-ink", "risk-low-ink", "risk-med-ink",
        "risk-high-ink"]
NON_TEXT = ["focus", "line-strong", "risk-low", "risk-high"]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("ground", ["bg", "surface"])
@pytest.mark.parametrize("token", TEXT)
def test_text_tokens_meet_aa(theme, ground, token):
    t = TOKENS[theme]
    assert contrast(t[token], t[ground]) >= 4.5, \
        f"{theme}: {token} {t[token]} on {ground} {t[ground]} is {contrast(t[token], t[ground]):.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("token", NON_TEXT)
def test_non_text_marks_meet_3_to_1(theme, token):
    t = TOKENS[theme]
    assert contrast(t[token], t["surface"]) >= 3.0


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_on_primary_buttons_meets_aa(theme):
    t = TOKENS[theme]
    assert contrast(t["on-primary"], t["primary"]) >= 4.5


def test_amber_fill_is_outlined_because_it_fails_alone():
    """#E1A42A is 2.1:1 on white - the brief's colour, kept for fills, so the map
    draws it with a dark outline that does reach 3:1."""
    t = TOKENS["light"]
    assert contrast(t["risk-med"], t["surface"]) < 3.0
    assert contrast(t["risk-outline"], t["risk-med"]) >= 3.0


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_brand_mark_is_visible_on_the_page(theme):
    t, b = TOKENS[theme], BRAND_COLOURS[theme]
    assert contrast(b["brand-blue"], t["bg"]) >= 3.0


def test_config_toml_mirrors_the_tokens():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    for theme in ("light", "dark"):
        section, t = config["theme"][theme], TOKENS[theme]
        assert section["primaryColor"].upper() == t["primary"].upper()
        assert section["backgroundColor"].upper() == t["bg"].upper()
        assert section["textColor"].upper() == t["ink"].upper()
        assert section["borderColor"].upper() == t["line"].upper()
    assert config["theme"]["font"].startswith("Mukta:")
    assert config["server"]["enableStaticServing"] is True


def test_brief_palette_is_used_verbatim():
    light = TOKENS["light"]
    assert (light["bg"], light["surface"], light["ink"], light["muted"]) == \
        ("#F6F8F9", "#FFFFFF", "#1C2B36", "#5B6B76")
    assert (light["primary"], light["accent"]) == ("#2E5E7E", "#4A8B4F")
    assert (light["risk-low"], light["risk-med"], light["risk-high"]) == \
        ("#3E9A61", "#E1A42A", "#C4453D")
    dark = TOKENS["dark"]
    assert (dark["bg"], dark["surface"], dark["ink"], dark["muted"]) == \
        ("#0F1B24", "#172733", "#E6EDF1", "#9AABB6")
