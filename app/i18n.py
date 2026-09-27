"""Public UI strings in English / Hindi / Marathi (config/i18n/*.yaml).

`t("key", n=2)` returns the string for the current language (st.session_state["lang"]),
falling back to English for a missing key - tests/test_i18n.py keeps the files in step,
so the fallback is a safety net, not a habit.
"""

from __future__ import annotations

import functools

import streamlit as st
import yaml

from src.common import CONFIG_DIR

LANGS = {"en": "English", "hi": "हिंदी", "mr": "मराठी"}


@functools.lru_cache(maxsize=None)
def strings(lang: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))


def lang() -> str:
    value = st.session_state.get("lang", "en")
    return value if value in LANGS else "en"


def t(key: str, lang_code: str | None = None, **fmt) -> str:
    code = lang_code or lang()
    text = strings(code).get(key)
    if text is None:
        text = strings("en").get(key, key)
    return text.format(**fmt) if fmt else text


def span(weeks: int, lang_code: str | None = None) -> str:
    return t("span_1", lang_code) if weeks == 1 else t("span_n", lang_code, n=weeks)


def bundle(lang_code: str | None = None) -> dict:
    """Every string for the map component, which renders labels in the browser."""
    code = lang_code or lang()
    return {**strings("en"), **{k: v for k, v in strings(code).items() if v}}
