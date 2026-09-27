"""The three UI languages carry the same keys and placeholders (config/i18n/*.yaml)."""

from __future__ import annotations

import re

import yaml

from src.common import CONFIG_DIR

LANGS = ("en", "hi", "mr")


def load(lang: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))


def test_every_language_has_every_key():
    keys = {lang: set(load(lang)) for lang in LANGS}
    for lang in ("hi", "mr"):
        assert keys[lang] == keys["en"], (lang, keys[lang] ^ keys["en"])


def test_placeholders_match_english():
    en = load("en")
    for lang in ("hi", "mr"):
        other = load(lang)
        for key, text in en.items():
            if isinstance(text, str):
                want = set(re.findall(r"\{(\w+)\}", text))
                got = set(re.findall(r"\{(\w+)\}", str(other[key])))
                assert want == got, f"{lang}.{key}: {got} != {want}"


def test_crop_translations_cover_every_english_threat():
    table = yaml.safe_load((CONFIG_DIR / "crop_vulnerability.yaml").read_text(encoding="utf-8"))
    tr = yaml.safe_load((CONFIG_DIR / "crop_vulnerability_i18n.yaml").read_text(encoding="utf-8"))
    for lang in ("hi", "mr"):
        assert set(tr[lang]["threats"]) == set(table["threats"])
        for crop, spec in table["crops"].items():
            threats = {k for k in spec if k != "name"}
            assert threats <= set(tr[lang]["crops"][crop]), (lang, crop)
