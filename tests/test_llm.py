"""Privacy, validation, caching and fallback for the language layer.

The privacy tests are the important ones: everything else degrades gracefully, but a
subscriber phone number reaching a third-party API is not recoverable.
"""

from __future__ import annotations

import copy

import pytest

from src.common import load_config
from src.llm import cache as cache_module
from src.llm.providers import (
    Generated,
    ProviderChain,
    ProviderError,
    TemplateProvider,
    build_chain,
)
from src.llm.validation import (
    PrivacyError,
    ValidationError,
    scan_for_private_data,
    scrub_payload,
    validate_text,
)
from src.report import DataError


@pytest.fixture
def cfg(tmp_path):
    """Real config, but with the cache pointed at a temp SQLite file."""
    out = copy.deepcopy(load_config("llm"))
    out["cache"]["path"] = str(tmp_path / "llm_cache.sqlite")
    return out


ADVISORY = {
    "template_text": "Heavy rain is likely. Drain excess water from soybean.",
    "action": "DRAINAGE_POSTPONE_FERTILIZER_SPRAY",
    "action_keyword": "drain",
    "crops": "soybean",
    "probability": 0.7,
    "horizon_days": 7,
    "language": "en",
}


class SpyProvider:
    """Records every payload it is handed, so a leak is provable."""

    name = "spy"
    source = "llm"

    def __init__(self, cfg=None, text="Drain the water from your field."):
        self.cfg = cfg
        self.text = text
        self.seen: list = []
        self.calls = 0

    def available(self):
        return True

    def generate_advisory_text(self, advisory, language):
        self.calls += 1
        self.seen.append(advisory)
        return Generated(text=self.text, provider=self.name, source=self.source,
                         language=language, meta={"output_chars": len(self.text)})

    def translate(self, text, target_language, **kw):
        self.calls += 1
        self.seen.append(text)
        return Generated(text=self.text, provider=self.name, source=self.source,
                         language=target_language, meta={})


class BrokenProvider(SpyProvider):
    name = "broken"

    def generate_advisory_text(self, advisory, language):
        self.calls += 1
        raise ProviderError("simulated outage")


# ==========================================================================
# Privacy - the non-negotiable
# ==========================================================================
@pytest.mark.parametrize("phone", [
    "9812345678", "+919812345678", "+91 98123 45678", "0091-9812345678",
    "whatsapp:+919812345678",
])
def test_phone_numbers_are_detected_anywhere_in_a_payload(phone, cfg):
    assert scan_for_private_data({"unit_name": f"Jhansi {phone}"}, cfg)


def test_phone_number_in_an_allowed_field_aborts_before_any_call(cfg):
    """The explicit requirement: a phone number must never reach a provider."""
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    leaky = dict(ADVISORY, unit_name="Bansgaon (contact 9812345678)")

    with pytest.raises(PrivacyError, match="phone_number"):
        chain.generate_advisory_text(leaky, "en")

    assert spy.calls == 0, "a provider was called with a phone number in the payload"
    assert spy.seen == []


def test_a_privacy_error_does_not_fall_through_to_the_next_provider(cfg):
    """A leak is a caller bug; retrying hands the same payload to someone else."""
    first, second = SpyProvider(cfg), SpyProvider(cfg)
    chain = ProviderChain([first, second, TemplateProvider(cfg)], cfg)
    leaky = dict(ADVISORY, district="Gaya 9812345678")

    with pytest.raises(PrivacyError):
        chain.generate_advisory_text(leaky, "en")
    assert first.calls == 0 and second.calls == 0


def test_subscriber_fields_are_dropped_before_the_request_is_built(cfg):
    payload = dict(ADVISORY, phone="9812345678", subscriber_name="A. Kumar",
                   msisdn="+919812345678", email="farmer@example.com")
    scrubbed = scrub_payload(payload, cfg)
    assert "phone" not in scrubbed
    assert "subscriber_name" not in scrubbed
    assert "msisdn" not in scrubbed
    assert "email" not in scrubbed
    assert scrubbed["action"] == ADVISORY["action"]


def test_only_whitelisted_fields_survive_scrubbing(cfg):
    allowed = set(cfg["privacy"]["allowed_fields"])
    scrubbed = scrub_payload(dict(ADVISORY, secret="x", notes="y"), cfg)
    assert set(scrubbed) <= allowed


def test_email_and_aadhaar_are_also_blocked(cfg):
    assert "email" in scan_for_private_data({"crop": "rice a@b.co"}, cfg)
    assert "aadhaar" in scan_for_private_data({"crop": "1234 5678 9012"}, cfg)


def test_a_clean_advisory_passes_privacy(cfg):
    assert scan_for_private_data(scrub_payload(ADVISORY, cfg), cfg) == []


# ==========================================================================
# Output validation
# ==========================================================================
def test_text_over_the_limit_is_rejected(cfg):
    with pytest.raises(ValidationError, match="exceeds"):
        validate_text("क " * 400, language="hi", cfg=cfg)


def test_missing_action_keyword_is_rejected(cfg):
    with pytest.raises(ValidationError, match="action keyword"):
        validate_text("The weather will be fine.", language="en",
                      action_keyword="drain", cfg=cfg)


def test_hindi_must_come_back_in_devanagari(cfg):
    with pytest.raises(ValidationError, match="script"):
        validate_text("Bhari barish hone wali hai, paani nikaalein.",
                      language="hi", cfg=cfg)
    validate_text("भारी बारिश होने वाली है, पानी निकालें।", language="hi", cfg=cfg)


def test_pesticide_doses_are_blocked(cfg):
    for bad in ["Spray carbendazim 0.1% solution",
                "Apply 25 kg urea now",
                "Use streptocycline as needed",
                "Mulch @ 3 t per acre"]:
        with pytest.raises(ValidationError):
            validate_text(bad, language="en", cfg=cfg)


def test_only_the_probability_number_is_allowed(cfg):
    validate_text("Drain your field. Chance 7 in 10.", language="en",
                  action_keyword="drain", allowed_numbers=["7", "10"], cfg=cfg)
    with pytest.raises(ValidationError, match="unexpected number"):
        validate_text("Drain your field within 3 days. Chance 7 in 10.",
                      language="en", action_keyword="drain",
                      allowed_numbers=["7", "10"], cfg=cfg)


def test_empty_text_is_rejected(cfg):
    with pytest.raises(ValidationError, match="empty"):
        validate_text("   ", language="en", cfg=cfg)


# ==========================================================================
# Fallback chain
# ==========================================================================
def test_chain_falls_through_to_the_template_when_a_provider_errors(cfg):
    broken = BrokenProvider(cfg)
    chain = ProviderChain([broken, TemplateProvider(cfg)], cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert broken.calls == 1
    assert result.provider == "template"
    assert result.source == "template"
    assert any("simulated outage" in a for a in result.attempts)


def test_text_failing_validation_is_rejected_and_the_chain_continues(cfg):
    """A provider that returns a dose must not be trusted just because it replied."""
    bad = SpyProvider(cfg, text="Drain the field and spray carbendazim 0.1% solution")
    chain = ProviderChain([bad, TemplateProvider(cfg)], cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert bad.calls == 1
    assert result.provider == "template"
    assert any("rejected" in a for a in result.attempts)


def test_llm_output_is_labelled_and_requires_approval(cfg):
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert result.source == "llm"
    assert result.requires_approval is True


def test_template_output_does_not_require_approval(cfg):
    chain = ProviderChain([TemplateProvider(cfg)], cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert result.source == "template"
    assert result.requires_approval is False


def test_offline_mode_skips_every_api_provider(cfg):
    cfg["offline_mode"] = True
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert spy.calls == 0
    assert result.provider == "template"
    assert any("offline mode" in a for a in result.attempts)


def test_a_provider_without_a_key_is_skipped_not_failed(cfg, monkeypatch):
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    chain = build_chain(cfg)
    result = chain.generate_advisory_text(ADVISORY, "en")
    assert result.provider == "template"
    assert any("no API key" in a for a in result.attempts)


def test_the_chain_must_end_with_the_template_provider(cfg):
    with pytest.raises(DataError, match="template"):
        build_chain(cfg, order=["sarvam", "gemini"])


def test_unknown_provider_name_is_rejected(cfg):
    with pytest.raises(DataError, match="unknown provider"):
        build_chain(cfg, order=["nope", "template"])


def test_template_provider_cannot_translate(cfg):
    with pytest.raises(ProviderError, match="cannot translate"):
        TemplateProvider(cfg).translate("Drain the field", "mr")


# ==========================================================================
# Cache and call log
# ==========================================================================
def test_identical_requests_hit_the_cache_instead_of_the_provider(cfg):
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)

    first = chain.generate_advisory_text(ADVISORY, "en")
    second = chain.generate_advisory_text(ADVISORY, "en")

    assert spy.calls == 1, "the second identical request called the API again"
    assert second.cache_hit is True
    assert second.text == first.text


def test_probabilities_in_the_same_bucket_share_a_cache_entry(cfg):
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    chain.generate_advisory_text(dict(ADVISORY, probability=0.71), "en")
    chain.generate_advisory_text(dict(ADVISORY, probability=0.73), "en")
    assert spy.calls == 1, "0.71 and 0.73 should not cost two calls"


def test_a_different_bucket_is_a_different_entry(cfg):
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    chain.generate_advisory_text(dict(ADVISORY, probability=0.71), "en")
    chain.generate_advisory_text(dict(ADVISORY, probability=0.31), "en")
    assert spy.calls == 2


def test_bucketing_is_monotone(cfg):
    assert cache_module.bucket(0.71, cfg) == cache_module.bucket(0.73, cfg)
    assert cache_module.bucket(0.71, cfg) != cache_module.bucket(0.66, cfg)
    assert cache_module.bucket(None, cfg) is None


def test_every_call_is_logged_with_its_provider_and_outcome(cfg):
    broken = BrokenProvider(cfg)
    chain = ProviderChain([broken, TemplateProvider(cfg)], cfg)
    chain.generate_advisory_text(ADVISORY, "en")

    with cache_module.connect(cfg=cfg) as connection:
        rows = connection.execute(
            "SELECT provider, ok, error FROM llm_calls ORDER BY id"
        ).fetchall()
    providers = [r[0] for r in rows]
    assert "broken" in providers and "template" in providers
    failed = [r for r in rows if r[0] == "broken"]
    assert failed and failed[0][1] == 0 and "outage" in failed[0][2]


def test_daily_totals_group_by_day_and_provider(cfg):
    chain = ProviderChain([TemplateProvider(cfg)], cfg)
    chain.generate_advisory_text(ADVISORY, "en")
    totals = cache_module.daily_totals(cfg=cfg)
    assert totals
    assert {"day", "provider", "calls"} <= set(totals[0])


def test_daily_budget_stops_calling_out(cfg):
    cfg["rate_limit"]["daily_call_budget"] = 1
    spy = SpyProvider(cfg)
    chain = ProviderChain([spy, TemplateProvider(cfg)], cfg)
    chain.generate_advisory_text(dict(ADVISORY, probability=0.71), "en")
    result = chain.generate_advisory_text(dict(ADVISORY, probability=0.31), "en")
    assert spy.calls == 1
    assert result.provider == "template"
    assert any("budget" in a for a in result.attempts)
