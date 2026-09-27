"""The seasonal header band as an st.components.v2 component (app/components/scene_header.js).

v2, like the map: it keeps its DOM across reruns, so a scene keeps animating while the
viewer changes a control instead of restarting. Only presentation goes in `data`: the
scene name, the page title and description, colours, and the forecast mode for the
selected area (never anything from outside app/public_data.py).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _component():
    return st.components.v2.component(
        "monsoon_scene_header",
        # The stylesheet travels inside the JS, which adds it to <head> once: with
        # isolate_styles=False, Streamlit 1.64 did not inject this component's `css`
        # (checked 2026-09-28: no <style> carried it), and without it the canvas takes
        # its pixel size and the band grows without bound.
        js=("const MO_SCENE_CSS = " + json.dumps((HERE / "scene_header.css").read_text(encoding="utf-8"))
            + ";\n" + (HERE / "scene_header.js").read_text(encoding="utf-8")),
        isolate_styles=False,        # --mo-* tokens and Mukta reach the band
    )


def scene_header(data: dict, key: str = "mo_scene"):
    try:
        return _component()(data=data, key=key)
    except Exception as err:                 # a new runtime (tests, a restarted server)
        if "not registered" not in str(err):  # has its own registry: declare it again
            raise
        _component.cache_clear()
        return _component()(data=data, key=key)
