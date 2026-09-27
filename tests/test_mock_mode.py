"""Mock mode must make network calls impossible, not merely unlikely.

The rule under test: with MOCK_MODE on, or with the credentials for a service
missing, that service is never contacted. These tests assert it by making the network
itself explode - any real HTTP call fails the test rather than silently succeeding.
"""

from __future__ import annotations

import copy

import pytest

from src.common import load_config
from src.db.models import (Advisory, ForecastRun, Message, Subscriber,
                           Unit)
from src.db.session import create_all, session_scope
from src.llm.providers import ProviderChain, TemplateProvider, build_chain
from src.runtime import llm_is_mock, sender_is_mock, status

LLM_KEYS = ("SARVAM_API_KEY", "GEMINI_API_KEY")
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")


@pytest.fixture(autouse=True)
def _clear_env_cache():
    """runtime caches the .env load; drop it so monkeypatched vars take effect."""
    from src.runtime import _load_env_once

    _load_env_once.cache_clear()
    yield
    _load_env_once.cache_clear()


@pytest.fixture
def no_network(monkeypatch):
    """Make every outbound HTTP call raise, so a leak cannot pass quietly."""
    import requests

    def explode(*args, **kwargs):
        raise AssertionError(
            "a network call was made while in mock mode: "
            f"args={args[:2]} kwargs={sorted(kwargs)}"
        )

    monkeypatch.setattr(requests, "post", explode)
    monkeypatch.setattr(requests, "get", explode)
    monkeypatch.setattr(requests.Session, "request", explode)
    return explode


@pytest.fixture
def cfg(tmp_path):
    out = copy.deepcopy(load_config("llm"))
    out["cache"]["path"] = str(tmp_path / "cache.sqlite")
    return out


ADVISORY = {
    "template_text": "Heavy rain is likely. Drain excess water from soybean.",
    "action": "DRAINAGE_POSTPONE_FERTILIZER_SPRAY",
    "action_keyword": "drain",
    "crops": "soybean",
    "probability": 0.7,
    "horizon_days": 7,
}


# ==========================================================================
# The switch itself
# ==========================================================================
def test_missing_keys_mean_mock(monkeypatch):
    for name in LLM_KEYS + TWILIO_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "false")
    from src.runtime import _load_env_once

    _load_env_once.cache_clear()
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)

    assert llm_is_mock() is True
    assert sender_is_mock() is True


def test_mock_mode_forces_mock_even_with_keys(monkeypatch):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    for name in LLM_KEYS + TWILIO_KEYS:
        monkeypatch.setenv(name, "present")
    monkeypatch.setenv("MOCK_MODE", "true")

    assert llm_is_mock() is True
    assert sender_is_mock() is True
    assert status()["llm_reason"] == "MOCK_MODE is on"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_spellings_all_enable_mock(monkeypatch, value):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("SARVAM_API_KEY", "present")
    monkeypatch.setenv("MOCK_MODE", value)
    assert llm_is_mock() is True


def test_keys_plus_mock_mode_false_means_live(monkeypatch):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("SARVAM_API_KEY", "present")
    monkeypatch.setenv("MOCK_MODE", "false")
    assert llm_is_mock() is False


def test_one_service_can_be_live_while_the_other_is_mocked(monkeypatch):
    """The real situation today: translation live, delivery mocked."""
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.setenv("SARVAM_API_KEY", "present")
    for name in TWILIO_KEYS:
        monkeypatch.delenv(name, raising=False)

    assert llm_is_mock() is False
    assert sender_is_mock() is True


# ==========================================================================
# No network in mock mode
# ==========================================================================
def test_chain_makes_no_http_call_when_keys_are_missing(monkeypatch, no_network, cfg):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    for name in LLM_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "false")

    result = build_chain(cfg).generate_advisory_text(ADVISORY, "en")
    assert result.provider == "template"
    assert result.source == "template"


def test_chain_makes_no_http_call_when_mock_mode_is_on(monkeypatch, no_network, cfg):
    """Keys present, MOCK_MODE on: the providers must still not be called."""
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    for name in LLM_KEYS:
        monkeypatch.setenv(name, "present")
    monkeypatch.setenv("MOCK_MODE", "true")

    result = build_chain(cfg).generate_advisory_text(ADVISORY, "en")
    assert result.provider == "template"
    assert any("MOCK_MODE is on" in a for a in result.attempts)


def test_offline_mode_in_config_also_blocks_the_network(monkeypatch, no_network, cfg):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    for name in LLM_KEYS:
        monkeypatch.setenv(name, "present")
    monkeypatch.setenv("MOCK_MODE", "false")
    cfg["offline_mode"] = True

    result = build_chain(cfg).generate_advisory_text(ADVISORY, "en")
    assert result.provider == "template"


# ==========================================================================
# The sender
# ==========================================================================
def _seed(session) -> int:
    """A unit, a consented subscriber and a forecast run. Returns the run id.

    Each parent is flushed before its children: there are no relationship() links
    between these tables, only raw ForeignKeys, so SQLAlchemy does not order the
    INSERTs for us and foreign keys are enforced (PRAGMA foreign_keys=ON).
    """
    import datetime as _dt

    session.add_all([
        Unit(unit_id="U_TEST", unit_name="Testpur", district="Gaya",
             state="Bihar", is_pilot=True),
        # A second unit: advisories are unique per (run, unit), so a test that needs
        # two advisories in one run needs two units.
        Unit(unit_id="U_TEST2", unit_name="Testpur North", district="Gaya",
             state="Bihar", is_pilot=True),
    ])
    session.flush()
    session.add(Subscriber(id=9001, name="Test Farmer", phone="+919999900001",
                           language="en", unit_id="U_TEST", district="Gaya",
                           state="Bihar", consent=True, do_not_send=False))
    run = ForecastRun(as_of=_dt.date(2024, 6, 19),
                      requested_as_of=_dt.date(2024, 6, 19), n_units=1)
    session.add(run)
    session.flush()
    return run.id


def test_sender_writes_mock_rows_and_never_calls_twilio(monkeypatch, tmp_path,
                                                        no_network):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    for name in TWILIO_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MOCK_MODE", "false")

    def explode(*_a, **_k):
        raise AssertionError("Twilio was called in mock mode")

    monkeypatch.setattr("src.sender.send_twilio", explode)

    path = tmp_path / "test.db"
    create_all(path)
    from src.sender import send_approved

    with session_scope(path) as session:
        run_id = _seed(session)
        session.add(Advisory(
            run_id=run_id, unit_id="U_TEST", as_of=__import__("datetime").date(2024, 6, 19),
            action="DRAINAGE_POSTPONE_FERTILIZER_SPRAY", confidence="high",
            status="approved", text_en="Drain excess water from your field now.",
            source="template",
        ))

    with session_scope(path) as session:
        counts = send_approved(session, sent_by="pytest")

    assert counts["messages"] == 1
    with session_scope(path) as session:
        message = session.query(Message).one()
        assert message.channel == "mock"
        assert message.delivery_status == "delivered (mock)"
        assert message.provider_message_id is None


def test_sender_skips_subscribers_without_consent(monkeypatch, tmp_path):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("MOCK_MODE", "true")

    path = tmp_path / "consent.db"
    create_all(path)
    from src.sender import send_approved

    with session_scope(path) as session:
        run_id = _seed(session)
        session.query(Subscriber).filter(Subscriber.id == 9001).update(
            {"consent": False})
        session.add(Advisory(
            run_id=run_id, unit_id="U_TEST", as_of=__import__("datetime").date(2024, 6, 19),
            action="DRAINAGE_POSTPONE_FERTILIZER_SPRAY", confidence="high",
            status="approved", text_en="Drain excess water now.", source="template",
        ))

    with session_scope(path) as session:
        counts = send_approved(session, sent_by="pytest")

    assert counts["messages"] == 0
    with session_scope(path) as session:
        assert session.query(Message).count() == 0


def test_sender_never_sends_an_unapproved_advisory(monkeypatch, tmp_path):
    monkeypatch.setattr("src.runtime._load_env_once", lambda: None)
    monkeypatch.setenv("MOCK_MODE", "true")

    path = tmp_path / "pending.db"
    create_all(path)
    from src.sender import send_approved

    with session_scope(path) as session:
        run_id = _seed(session)
        for status_value, unit in (("pending_approval", "U_TEST"),
                                   ("rejected", "U_TEST2")):
            session.add(Advisory(
                run_id=run_id, unit_id=unit,
                as_of=__import__("datetime").date(2024, 6, 19),
                action="FLOOD_PREP", confidence="high", status=status_value,
                text_en="Move produce to higher ground.", source="template",
            ))

    with session_scope(path) as session:
        counts = send_approved(session, sent_by="pytest")

    assert counts["messages"] == 0
