"""Provider chain for advisory text: Sarvam -> Gemini -> templates.

Common interface
----------------
    provider.generate_advisory_text(advisory_json, language) -> Generated
    provider.translate(text, target_language)                -> Generated

`ProviderChain` tries each configured provider in order and moves on when one has no
API key, errors, rate-limits past its retries, or returns text that fails validation.
`TemplateProvider` is last and cannot fail, so the chain always returns something.

Hard rules, enforced here rather than left to callers:

* **Nothing subscriber-specific leaves the process.** Every payload passes through
  `scrub_payload` first, which whitelists fields and aborts on a phone number.
  A `PrivacyError` is never retried and never falls through to the next provider -
  it means the caller has a bug.
* **Every generation is validated**, including the templates' own output.
* **Anything from Sarvam or Gemini is `source="llm"`** and, per config/llm.yaml,
  requires human approval before it can be sent. Templates are `source="template"`.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.common import load_config
from src.llm import cache as cache_module
from src.llm.validation import (
    PrivacyError,
    ValidationError,
    scan_for_private_data,
    scrub_payload,
    validate_text,
)
from src.report import DataError

LANGUAGE_CODES = {"en": "en-IN", "hi": "hi-IN", "mr": "mr-IN"}
LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi"}


class ProviderError(RuntimeError):
    """A provider could not produce usable text. The chain moves to the next one."""


@dataclass
class Generated:
    text: str
    provider: str
    source: str                      # "llm" or "template"
    language: str
    cache_hit: bool = False
    latency_ms: float | None = None
    requires_approval: bool = True
    attempts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...
    def generate_advisory_text(self, advisory: dict, language: str) -> Generated: ...
    def translate(self, text: str, target_language: str, **kw) -> Generated: ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _probability_numbers(advisory: dict) -> list[str]:
    """Digits the validator will tolerate: those in the "10 mein se X" phrase."""
    allowed = {"10"}
    probability = advisory.get("probability")
    if probability is not None:
        out_of_ten = max(1, min(10, round(float(probability) * 10)))
        allowed.add(str(out_of_ten))
    phrase = advisory.get("probability_phrase")
    if phrase:
        allowed.update(re.findall(r"\d+", str(phrase)))
    return sorted(allowed)


def _retry_sleep(cfg: dict, attempt: int) -> float:
    delays = cfg["rate_limit"]["backoff_seconds"]
    base = delays[min(attempt, len(delays) - 1)]
    jitter = cfg["rate_limit"].get("jitter", 0.0)
    return base * (1 + random.uniform(-jitter, jitter))


# --------------------------------------------------------------------------
# Template provider - the floor
# --------------------------------------------------------------------------
class TemplateProvider:
    """Fixed text from rules.yaml. Offline, free, deterministic, never fails."""

    name = "template"
    source = "template"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config("llm")

    def available(self) -> bool:
        return True

    def generate_advisory_text(self, advisory: dict, language: str) -> Generated:
        text = (advisory.get("template_text") or "").strip()
        if not text:
            raise ProviderError(
                "no template_text in the advisory payload; the template provider "
                "renders what the rule engine already produced and cannot invent it"
            )
        return Generated(
            text=text, provider=self.name, source=self.source, language=language,
            requires_approval=self.cfg["governance"]["template_output_requires_approval"],
        )

    def translate(self, text: str, target_language: str, **kw) -> Generated:
        # Templates cannot translate. Saying so is better than echoing the source
        # text in the wrong language.
        raise ProviderError(
            f"the template provider cannot translate into "
            f"{LANGUAGE_NAMES.get(target_language, target_language)}; "
            "pre-translated text must already be in rules.yaml"
        )


# --------------------------------------------------------------------------
# Sarvam
# --------------------------------------------------------------------------
class SarvamProvider:
    """Sarvam translate API plus a chat model for rephrasing.

    Endpoints and model names verified against docs.sarvam.ai on 2026-09-26.
    `sarvam-m` is deprecated and is deliberately not used.
    """

    name = "sarvam"
    source = "llm"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config("llm")
        self.settings = self.cfg["providers"]["sarvam"]

    @property
    def api_key(self) -> str | None:
        from src.runtime import env

        return env(self.settings["api_key_env"]) or None

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True)) and bool(self.api_key)

    def _post(self, url: str, payload: dict) -> dict:
        import requests

        headers = {self.settings["auth_header"]: self.api_key,
                   "Content-Type": "application/json"}
        attempts = self.cfg["rate_limit"]["max_attempts"]
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = requests.post(
                    url, json=payload, headers=headers,
                    timeout=self.settings["timeout_seconds"],
                )
            except Exception as exc:  # network flake
                last = exc
                time.sleep(_retry_sleep(self.cfg, attempt))
                continue
            if response.status_code == 429:
                last = ProviderError("rate limited (429)")
                if attempt < attempts - 1:
                    time.sleep(_retry_sleep(self.cfg, attempt))
                    continue
            if response.status_code >= 400:
                raise ProviderError(
                    f"sarvam {url} returned {response.status_code}: "
                    f"{response.text[:200]}"
                )
            return response.json()
        raise ProviderError(f"sarvam {url} failed after {attempts} attempts: {last}")

    def translate(self, text: str, target_language: str, *, mode: str | None = None,
                  source_language: str = "en", **kw) -> Generated:
        if target_language not in LANGUAGE_CODES:
            raise ProviderError(f"unsupported language {target_language!r}")

        # The mode decides the model. sarvam-translate:v1 rejects every colloquial
        # mode with a 400 ("Supported modes are: formal only"); the colloquial
        # registers are served by mayura:v1, which has a tighter input limit.
        requested_mode = mode or self.settings["default_mode"]
        formal_modes = set(self.settings.get("formal_modes") or ["formal"])
        if requested_mode in formal_modes:
            model = self.settings["translate_model"]
            limit = self.settings["max_input_chars"]
        else:
            model = self.settings.get("colloquial_model",
                                      self.settings["translate_model"])
            limit = self.settings.get("max_input_chars_colloquial",
                                      self.settings["max_input_chars"])
        if len(text) > limit:
            raise ProviderError(
                f"input is {len(text)} chars, limit is {limit} for {model}"
            )

        payload = {
            "input": text,
            "source_language_code": LANGUAGE_CODES[source_language],
            "target_language_code": LANGUAGE_CODES[target_language],
            "model": model,
            "mode": requested_mode,
            # Keep digits ASCII so the number validator can read the probability.
            "numerals_format": self.settings["numerals_format"],
        }
        started = time.perf_counter()
        body = self._post(self.settings["translate_url"], payload)
        latency = (time.perf_counter() - started) * 1000

        out = (body.get("translated_text") or "").strip()
        if not out:
            raise ProviderError(f"no translated_text in response: {str(body)[:200]}")
        return Generated(
            text=out, provider=self.name, source=self.source,
            language=target_language, latency_ms=latency,
            requires_approval=self.cfg["governance"]["llm_output_requires_approval"],
            meta={"input_chars": len(text), "output_chars": len(out),
                  "mode": payload["mode"], "model": payload["model"]},
        )

    def generate_advisory_text(self, advisory: dict, language: str) -> Generated:
        """Rephrase the rule engine's text in simpler farmer language."""
        safe = scrub_payload(advisory, self.cfg)
        base = (advisory.get("template_text") or "").strip()
        if not base:
            raise ProviderError("no template_text to rephrase")

        instruction = (
            "Rewrite this farm advisory SMS in simple spoken "
            f"{LANGUAGE_NAMES[language]} for a smallholder farmer. "
            f"Keep it under {self.cfg['validation']['max_chars']} characters. "
            "Keep the action and the chance exactly as given. "
            "Do not add any dose, chemical name, quantity or date. "
            "Reply with the message only."
        )
        context = ", ".join(f"{k}: {v}" for k, v in safe.items())
        payload = {
            "model": self.settings["chat_model"],
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": f"{base}\n\n[{context}]"},
            ],
            "temperature": 0.2,
        }
        started = time.perf_counter()
        body = self._post(self.settings["chat_url"], payload)
        latency = (time.perf_counter() - started) * 1000

        try:
            out = body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected chat response shape: {exc}") from exc

        usage = body.get("usage") or {}
        return Generated(
            text=out, provider=self.name, source=self.source, language=language,
            latency_ms=latency,
            requires_approval=self.cfg["governance"]["llm_output_requires_approval"],
            meta={"input_tokens": usage.get("prompt_tokens"),
                  "output_tokens": usage.get("completion_tokens"),
                  "output_chars": len(out), "model": self.settings["chat_model"]},
        )


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
class GeminiProvider:
    """Gemini via the google-genai SDK."""

    name = "gemini"
    source = "llm"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config("llm")
        self.settings = self.cfg["providers"]["gemini"]
        self._client = None

    @property
    def api_key(self) -> str | None:
        from src.runtime import env

        return env(self.settings["api_key_env"]) or None

    def available(self) -> bool:
        return bool(self.settings.get("enabled", True)) and bool(self.api_key)

    def client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate(self, prompt: str, system: str) -> tuple[str, dict, float]:
        from google.genai import types

        attempts = self.cfg["rate_limit"]["max_attempts"]
        last: Exception | None = None
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                response = self.client().models.generate_content(
                    model=self.settings["model"],
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=self.settings["temperature"],
                        max_output_tokens=self.settings["max_output_tokens"],
                    ),
                )
            except Exception as exc:
                last = exc
                message = str(exc)
                # 503 UNAVAILABLE ("high demand") is explicitly temporary and is the
                # most common failure on the Flash models, so it retries like a 429.
                transient = any(token in message for token in
                                ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
                                 "500", "INTERNAL", "502"))
                if transient and attempt < attempts - 1:
                    time.sleep(_retry_sleep(self.cfg, attempt))
                    continue
                raise ProviderError(f"gemini call failed: {exc}") from exc
            latency = (time.perf_counter() - started) * 1000
            usage = getattr(response, "usage_metadata", None)
            meta = {
                "input_tokens": getattr(usage, "prompt_token_count", None),
                "output_tokens": getattr(usage, "candidates_token_count", None),
                "model": self.settings["model"],
            }
            return (response.text or "").strip(), meta, latency
        raise ProviderError(f"gemini failed after {attempts} attempts: {last}")

    def generate_advisory_text(self, advisory: dict, language: str) -> Generated:
        safe = scrub_payload(advisory, self.cfg)
        base = (advisory.get("template_text") or "").strip()
        if not base:
            raise ProviderError("no template_text to rephrase")

        system = (
            "You rewrite farm advisory SMS messages for smallholder farmers in India. "
            f"Write in simple spoken {LANGUAGE_NAMES[language]}. "
            f"Stay under {self.cfg['validation']['max_chars']} characters. "
            "Keep the action and the stated chance unchanged. "
            "Never mention a chemical, a dose, a quantity or a date. "
            "Reply with the message only, no preamble."
        )
        context = ", ".join(f"{k}: {v}" for k, v in safe.items())
        text, meta, latency = self._generate(f"{base}\n\n[{context}]", system)
        meta["output_chars"] = len(text)
        return Generated(
            text=text, provider=self.name, source=self.source, language=language,
            latency_ms=latency,
            requires_approval=self.cfg["governance"]["llm_output_requires_approval"],
            meta=meta,
        )

    def translate(self, text: str, target_language: str, **kw) -> Generated:
        system = (
            f"Translate to {LANGUAGE_NAMES.get(target_language, target_language)} "
            "using simple spoken words a farmer will understand. "
            "Translate the COMPLETE sentence - every instruction and every reason "
            "must appear in the translation. Do not summarise or shorten. "
            "Write in Devanagari script only, never Latin letters. "
            "Reply with the translation only."
        )
        out, meta, latency = self._generate(text, system)
        meta["input_chars"] = len(text)
        meta["output_chars"] = len(out)
        return Generated(
            text=out, provider=self.name, source=self.source,
            language=target_language, latency_ms=latency,
            requires_approval=self.cfg["governance"]["llm_output_requires_approval"],
            meta=meta,
        )


PROVIDERS = {"sarvam": SarvamProvider, "gemini": GeminiProvider,
             "template": TemplateProvider}


# --------------------------------------------------------------------------
# Chain
# --------------------------------------------------------------------------
class ProviderChain:
    """Tries providers in configured order, validating every result."""

    def __init__(self, providers: list[Provider], cfg: dict | None = None):
        self.cfg = cfg or load_config("llm")
        self.providers = providers

    # -- internals ---------------------------------------------------------
    def _mock(self) -> bool:
        """Template-only when mock mode is on or no provider has a key.

        Checked here rather than at each provider so there is exactly one place
        that decides whether this process may talk to the internet.
        """
        from src.runtime import llm_is_mock

        return bool(self.cfg.get("offline_mode")) or llm_is_mock()

    def _budget_exhausted(self) -> bool:
        budget = self.cfg["rate_limit"].get("daily_call_budget") or 0
        if budget <= 0:
            return False
        return cache_module.calls_today(cfg=self.cfg) >= budget

    def _run(self, operation: str, language: str, call, *,
             action_keyword: str | None, allowed_numbers: list[str] | None,
             cache_key: str | None, cache_fields: dict | None) -> Generated:
        if cache_key:
            hit = cache_module.get(cache_key, cfg=self.cfg)
            if hit:
                cache_module.log_call(provider=hit["provider"], operation=operation,
                                      ok=True, language=language, cache_hit=True,
                                      cfg=self.cfg)
                return Generated(
                    text=hit["text"], provider=hit["provider"], source=hit["source"],
                    language=language, cache_hit=True,
                    requires_approval=(hit["source"] == "llm"),
                    attempts=["cache"],
                )

        attempts: list[str] = []
        over_budget = self._budget_exhausted()
        for provider in self.providers:
            if self._mock() and provider.name != "template":
                from src.runtime import llm_mock_reason

                reason = ("offline mode" if self.cfg.get("offline_mode")
                          else llm_mock_reason())
                attempts.append(f"{provider.name}: skipped ({reason})")
                continue
            if over_budget and provider.name != "template":
                attempts.append(f"{provider.name}: skipped (daily budget reached)")
                continue
            if not provider.available():
                attempts.append(f"{provider.name}: skipped (no API key)")
                continue
            try:
                result = call(provider)
            except PrivacyError:
                # Never fall through: a leak is the caller's bug, not a provider
                # failure, and the next provider would be handed the same payload.
                raise
            except ProviderError as exc:
                attempts.append(f"{provider.name}: {exc}")
                cache_module.log_call(provider=provider.name, operation=operation,
                                      ok=False, language=language, error=str(exc)[:300],
                                      cfg=self.cfg)
                continue

            try:
                validate_text(result.text, language=language,
                              action_keyword=action_keyword,
                              allowed_numbers=allowed_numbers,
                              trusted=(result.source == "template"), cfg=self.cfg)
            except ValidationError as exc:
                attempts.append(f"{provider.name}: rejected ({exc})")
                cache_module.log_call(
                    provider=provider.name, operation=operation, ok=False,
                    language=language, latency_ms=result.latency_ms,
                    error=f"validation: {exc}"[:300], cfg=self.cfg,
                )
                continue

            meta = result.meta
            cache_module.log_call(
                provider=provider.name, operation=operation, ok=True,
                language=language, latency_ms=result.latency_ms,
                input_chars=meta.get("input_chars"),
                output_chars=meta.get("output_chars"),
                input_tokens=meta.get("input_tokens"),
                output_tokens=meta.get("output_tokens"),
                cost_inr=self._estimate_cost(provider.name, meta), cfg=self.cfg,
            )
            attempts.append(f"{provider.name}: ok")
            result.attempts = attempts
            if cache_key:
                cache_module.put(cache_key, kind=operation, language=language,
                                 text=result.text, provider=provider.name,
                                 source=result.source, cfg=self.cfg,
                                 **(cache_fields or {}))
            return result

        raise DataError(
            "every provider failed and the template fallback did not produce text; "
            f"attempts: {attempts}"
        )

    def _estimate_cost(self, provider: str, meta: dict) -> float:
        settings = self.cfg["providers"].get(provider, {})
        chars = (meta.get("input_chars") or 0) + (meta.get("output_chars") or 0)
        tokens = (meta.get("input_tokens") or 0) + (meta.get("output_tokens") or 0)
        rate_chars = settings.get("estimated_cost_per_1k_chars_inr", 0.0) or 0.0
        rate_tokens = settings.get("estimated_cost_per_1k_tokens_inr", 0.0) or 0.0
        return round(chars / 1000 * rate_chars + tokens / 1000 * rate_tokens, 6)

    # -- public ------------------------------------------------------------
    def generate_advisory_text(self, advisory: dict, language: str) -> Generated:
        # Scrub HERE, at the single choke point, not inside each provider. Leaving it
        # to providers means any new one that forgets the call leaks silently; doing
        # it once means a provider physically cannot be handed a phone number.
        safe = scrub_payload(advisory, self.cfg)

        key, prob = cache_module.make_key(
            kind="advisory", language=language, action=safe.get("action"),
            crop=str(safe.get("crops") or safe.get("crop")),
            probability=safe.get("probability"), cfg=self.cfg,
        )
        return self._run(
            "advisory", language,
            lambda p: p.generate_advisory_text(safe, language),
            action_keyword=safe.get("action_keyword"),
            allowed_numbers=_probability_numbers(safe),
            cache_key=key,
            cache_fields={"action": safe.get("action"),
                          "crop": str(safe.get("crops") or safe.get("crop")),
                          "prob_bucket": prob},
        )

    def translate(self, text: str, target_language: str, *,
                  action_keyword: str | None = None,
                  allowed_numbers: list[str] | None = None,
                  mode: str | None = None, source_language: str = "en") -> Generated:
        # Free text goes out verbatim, so it gets the same scan as a payload.
        leaks = scan_for_private_data({"template_text": text}, self.cfg)
        if leaks:
            raise PrivacyError(
                f"refusing to translate: {', '.join(leaks)} found in the text"
            )

        key, _ = cache_module.make_key(
            kind="translate", language=target_language, mode=mode, extra=text,
            cfg=self.cfg,
        )
        return self._run(
            "translate", target_language,
            lambda p: p.translate(text, target_language, mode=mode,
                                  source_language=source_language),
            action_keyword=action_keyword, allowed_numbers=allowed_numbers,
            cache_key=key, cache_fields={"mode": mode},
        )


def build_chain(cfg: dict | None = None, order: list[str] | None = None) -> ProviderChain:
    cfg = cfg or load_config("llm")
    names = order or cfg["fallback_order"]
    if "template" not in names:
        raise DataError(
            "fallback_order must end with `template`: it is the only provider that "
            "works offline and cannot fail"
        )
    providers = []
    for name in names:
        factory = PROVIDERS.get(name)
        if factory is None:
            raise DataError(f"unknown provider {name!r}; expected {sorted(PROVIDERS)}")
        providers.append(factory(cfg))
    return ProviderChain(providers, cfg)
