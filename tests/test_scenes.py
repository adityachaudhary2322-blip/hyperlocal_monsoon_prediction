"""Seasonal header scenes: which scene, what the forecast makes it do, and the guarantees
the band makes (no images, particle caps, pause/idle/reduced motion, Classic stays plain).
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.common import ROOT
from tests.test_public_access import seeded_db  # noqa: F401  (pytest fixture)

JS = (ROOT / "app" / "components" / "scene_header.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("month, scene", [(1, "winter"), (2, "winter"), (3, "spring"), (4, "spring"),
                                          (5, "summer"), (6, "monsoon"), (9, "monsoon"),
                                          (10, "harvest"), (11, "harvest"), (12, "winter")])
def test_scene_by_month(month, scene):
    from app import scenes

    assert scenes.by_date(dt.date(2026, month, 15)) == scene


def test_scene_by_date_is_judged_in_india():
    """20:00 UTC on 31 May is already 1 June in Asia/Kolkata: monsoon, not summer."""
    from app import scenes

    assert scenes.by_date(dt.datetime(2026, 5, 31, 20, 0, tzinfo=dt.timezone.utc)) == "monsoon"
    assert scenes.by_date(dt.datetime(2026, 5, 31, 17, 0, tzinfo=dt.timezone.utc)) == "summer"


def test_menu_choices_default_and_classic():
    from app import scenes

    assert scenes.cfg()["default"] == "monsoon"
    assert scenes.resolve(None) == "monsoon"
    assert scenes.resolve("classic") == "classic"
    assert scenes.resolve("nonsense") == "monsoon"
    assert scenes.resolve("auto", dt.date(2026, 1, 10)) == "winter"
    assert {"monsoon", "auto", "classic", "harvest", "winter", "spring", "summer"} == \
        set(scenes.cfg()["choices"])


@pytest.mark.parametrize("rain, p_dry, mode", [(0.0, 0.1, "none"), (4.9, None, "none"),
                                               (5.0, 0.2, "light"), (49.9, 0.59, "light"),
                                               (50.0, 0.1, "heavy"), (120.0, 0.6, "clear"),
                                               (None, 0.7, "clear"), (None, 0.2, None)])
def test_forecast_mode(rain, p_dry, mode):
    from app import scenes

    assert scenes.forecast_mode(rain, p_dry) == mode


def test_particle_caps_and_timing_come_from_config():
    from app import scenes

    cfg = scenes.cfg()
    assert cfg["particles"]["desktop"] <= 150 and cfg["particles"]["phone"] <= 60
    assert 30 <= cfg["fps"] <= 60 and cfg["idle_stop_seconds"] == 60


def test_band_script_guarantees():
    for needle in ("prefers-reduced-motion", "visibilitychange", "IntersectionObserver",
                   "idle_stop_seconds", "mo-scene-in"):
        assert needle in JS, needle
    # Vector shapes only: no images, no downloads.
    for banned in ("new Image", "drawImage", ".png", ".jpg", "fetch(", "http://", "https://"):
        assert banned not in JS, banned


# --------------------------------------------------------------------------
# Every page: animated on public pages, faint and still on officer pages, plain on Classic
# --------------------------------------------------------------------------
@pytest.fixture()
def band(monkeypatch):
    import app.components.scene_header as component

    seen: list[dict] = []
    monkeypatch.setattr(component, "scene_header", lambda data, key="mo_scene": seen.append(data))
    return seen


def _run(user=None, scene=None, page=None, effects=True):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=120)
    if user is not None:
        at.session_state["user"] = user
    if scene:
        at.session_state["scene"] = scene
    at.session_state["effects"] = effects
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_public_page_band_animates(seeded_db, band):  # noqa: F811
    _run()
    assert band and band[-1]["scene"] == "monsoon"
    assert band[-1]["animate"] is True and band[-1]["faint"] is False


def test_effects_off_shows_a_still_frame(seeded_db, band):  # noqa: F811
    _run(effects=False)
    assert band[-1]["animate"] is False


def test_officer_pages_get_a_faint_still_band(seeded_db, band):  # noqa: F811
    from src.db.models import User

    _run(user=User(username="o", name="O", role="officer", assigned_states="Maharashtra",
                   active=True, password_hash="x"))
    # Signed in, the default page is still Home (public); force an officer page run.
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "pages" / "overview.py"), default_timeout=120)
    at.session_state["user"] = User(username="o", name="O", role="officer",
                                    assigned_states="Maharashtra", active=True, password_hash="x")
    at.session_state["_mo_public"] = False
    at.run()
    assert band[-1]["faint"] is True and band[-1]["animate"] is False
    assert band[-1]["mode"] is None and band[-1]["caption"] == ""


def test_classic_keeps_the_plain_title(seeded_db, band):  # noqa: F811
    at = _run(scene="classic")
    assert band == []                                   # no band drawn
    assert any("Monsoon outlook" in str(t.value) for t in at.title)


def test_scene_menu_offers_classic(seeded_db, band):  # noqa: F811
    at = _run()
    radio = at.radio(key="scene")
    assert "classic" in radio.options or "Classic" in [str(o) for o in radio.options]
    radio.set_value("classic").run()
    assert not at.exception
