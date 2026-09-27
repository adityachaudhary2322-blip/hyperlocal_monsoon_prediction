"""One navigation for the whole app (app/main.py), driven through streamlit.testing.

* Signed out: the four public pages plus the hidden sign-in page - no officer route.
* Signed in: public pages and an "Officer portal" section; Settings for admins only.
* Every officer page can reach Home, Next 30 days and Accuracy, and can sign out.
* After sign-out no officer page is reachable, also by asking for its URL directly.
"""

from __future__ import annotations

import pytest

from src.common import ROOT
from tests.test_public_access import seeded_db  # noqa: F401  (pytest fixture)

OFFICER_PAGES = {"pages/overview.py": "Overview", "pages/risk_map.py": "Risk changes",
                 "pages/approvals.py": "Approval queue", "pages/custom_alert.py": "Custom alert",
                 "pages/outbox.py": "Outbox", "pages/models.py": "Models"}
OFFICER_URLS = {"overview", "risk-changes", "approvals", "custom-alert", "outbox", "models"}
PUBLIC_URLS = {"", "next-30-days", "accuracy", "about"}


def _user(role: str):
    from src.db.models import User

    return User(username=f"t_{role}", name=f"Test {role}", role=role,
                assigned_states="Maharashtra", active=True, password_hash="x")


def _app(user=None):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / "main.py"), default_timeout=120)
    if user is not None:
        at.session_state["user"] = user
    return at


def _titles(at) -> list[str]:
    return [str(t.value) for t in at.title]


@pytest.fixture()
def nav_spy(monkeypatch):
    """Records what st.navigation registered, and can open a page by URL path.

    `nav_spy["open"] = "overview"` makes the next runs resolve to that page when it is
    registered - what a click on its link, or typing its URL, does. When it is not
    registered the real call's choice (the default page, Home) stands, exactly as
    Streamlit serves an unknown URL. AppTest.switch_page cannot do this for an
    st.navigation app: after a switch it runs the page file on its own, without main.py.
    """
    import streamlit as st

    seen: dict = {"open": None}
    original = st.navigation

    def spy(pages, **kwargs):
        seen["position"] = kwargs.get("position")
        groups = pages if isinstance(pages, dict) else {"": pages}
        seen["sections"] = {k: {p.url_path for p in v} for k, v in groups.items()}
        chosen = original(pages, **kwargs)
        wanted = seen["open"]
        if wanted is not None:
            for group in groups.values():
                for page in group:
                    if page.url_path == wanted:
                        seen["served"] = wanted
                        chosen._can_be_called = False     # Streamlit's run-once guard
                        page._can_be_called = True
                        return page
        seen["served"] = chosen.url_path
        return chosen

    monkeypatch.setattr(st, "navigation", spy)
    return seen


def test_signed_out_navigation_is_public_only(seeded_db, nav_spy):  # noqa: F811
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert nav_spy["position"] == "top"
    urls = set().union(*nav_spy["sections"].values())
    assert urls == PUBLIC_URLS | {"sign-in"}
    assert not urls & OFFICER_URLS


@pytest.mark.parametrize("role, settings", [("officer", False), ("demo", False), ("admin", True)])
def test_signed_in_navigation_has_public_and_officer_sections(seeded_db, nav_spy, role,  # noqa: F811
                                                              settings):
    at = _app(_user(role))
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    sections = nav_spy["sections"]
    assert sections[""] == PUBLIC_URLS | {"sign-in"}      # sign-in stays, hidden
    officer = next(v for k, v in sections.items() if k)
    assert OFFICER_URLS <= officer
    assert ("settings" in officer) is settings


@pytest.mark.parametrize("url", sorted(OFFICER_URLS | {"settings"}))
def test_officer_urls_are_unreachable_signed_out(seeded_db, nav_spy, url):  # noqa: F811
    """Signed out, typing an officer page's URL serves Home: the page is not registered."""
    nav_spy["open"] = url
    at = _app()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert nav_spy["served"] == ""
    assert not set(OFFICER_PAGES.values()) & set(_titles(at))


@pytest.mark.parametrize("page", list(OFFICER_PAGES) + ["pages/settings.py"])
def test_officer_page_scripts_stop_without_a_user(seeded_db, page):  # noqa: F811
    """The second guard: run the page file on its own, bypassing navigation."""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app" / page), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert "Officer sign in" in _titles(at)
    assert OFFICER_PAGES.get(page, "Settings") not in _titles(at)


URL_OF = {"pages/overview.py": "overview", "pages/risk_map.py": "risk-changes",
          "pages/approvals.py": "approvals", "pages/custom_alert.py": "custom-alert",
          "pages/outbox.py": "outbox", "pages/models.py": "models"}


@pytest.mark.parametrize("page", list(OFFICER_PAGES))
def test_every_officer_page_reaches_public_pages_and_signs_out(seeded_db, nav_spy, page):  # noqa: F811
    user = _user("officer")
    at = _app(user)
    nav_spy["open"] = URL_OF[page]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert OFFICER_PAGES[page] in _titles(at)
    assert PUBLIC_URLS <= nav_spy["sections"][""]           # public links on this page

    for public in ("", "next-30-days", "accuracy"):          # Home, Next 30 days, Accuracy
        nav_spy["open"] = public
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert nav_spy["served"] == public
        assert not set(OFFICER_PAGES.values()) & set(_titles(at))
        assert at.session_state["user"] is user              # still signed in
        nav_spy["open"] = URL_OF[page]
        at.run()
        assert OFFICER_PAGES[page] in _titles(at)            # and back again

    at.button(key="mo_signout").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert "user" not in at.session_state
    assert nav_spy["served"] == ""                          # landed on Home
    assert set().union(*nav_spy["sections"].values()) == PUBLIC_URLS | {"sign-in"}
    at.run()                                                # the officer URL again
    assert OFFICER_PAGES[page] not in _titles(at)


def test_demo_user_cannot_open_settings(seeded_db, nav_spy):  # noqa: F811
    nav_spy["open"] = "settings"
    at = _app(_user("demo"))
    at.run()
    assert nav_spy["served"] != "settings"
    assert "Settings" not in _titles(at)


def test_top_bar_shows_role_and_demo_badge(seeded_db):  # noqa: F811
    at = _app(_user("demo"))
    at.run()
    html = "\n".join(str(getattr(e, "proto", "")) for e in at.get("html"))
    assert "Demo mode" in html and "Test demo" in html
