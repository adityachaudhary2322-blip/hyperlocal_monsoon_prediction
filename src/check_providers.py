"""Verify each language provider against its live API.

Run:  python -m src.check_providers [--text "..."] [--languages hi,mr]

Calls each provider DIRECTLY rather than through the fallback chain: the chain is
designed to hide a provider failure behind the next one, which is exactly what this
script needs to expose. One failure is reported and the run continues.

Key values are never printed. Only "set" or "missing" is shown, and every line of
output passes through a redactor that replaces any configured secret with `***` -
provider error bodies sometimes echo the key back.
"""

from __future__ import annotations

import argparse
import time

from src.common import load_config
from src.llm.providers import GeminiProvider, SarvamProvider
from src.llm.validation import ValidationError, validate_text
from src.runtime import env, status as runtime_status

DEFAULT_TEXT = "Delay sowing by 10 days because a dry spell is likely."
SECRET_ENV_NAMES = ("SARVAM_API_KEY", "GEMINI_API_KEY", "TWILIO_ACCOUNT_SID",
                    "TWILIO_AUTH_TOKEN", "ADMIN_PASSWORD", "TELEGRAM_BOT_TOKEN")

LANGUAGE_NAMES = {"hi": "Hindi", "mr": "Marathi", "en": "English"}


def redact(text: str) -> str:
    """Replace any configured secret with ***, wherever it appears."""
    out = str(text)
    for name in SECRET_ENV_NAMES:
        value = env(name)
        if value and len(value) >= 6:
            out = out.replace(value, "***")
    return out


def say(text: str = "") -> None:
    print(redact(text))


def key_state(name: str) -> str:
    return "set" if env(name) else "missing"


def classify(error: Exception) -> str:
    """auth / rate limit / model name / network / other."""
    message = str(error).lower()
    if any(s in message for s in ("401", "403", "unauthorized", "forbidden",
                                 "invalid api key", "api key not valid",
                                 "permission_denied", "unauthenticated")):
        return "auth - the key was rejected"
    if any(s in message for s in ("429", "rate limit", "resource_exhausted",
                                  "quota")):
        return "rate limit - retry later or raise the quota"
    # Check availability BEFORE the model-name rule: a 503 body contains the word
    # "model" and was being misreported as a bad model name.
    if any(s in message for s in ("503", "unavailable", "high demand", "overloaded",
                                  "500", "502", "internal error")):
        return "service unavailable - transient, the provider is busy"
    if "400" in message and "mode" in message:
        return "bad request - the mode/model combination is not supported"
    if any(s in message for s in ("404", "not found", "model", "deprecated")):
        return "model name - the configured model may not exist on this account"
    if any(s in message for s in ("timeout", "timed out", "connection",
                                  "dns", "ssl", "unreachable", "getaddrinfo")):
        return "network - could not reach the endpoint"
    return "other"


def keyword_for(language: str, rules: dict) -> str | None:
    """The keyword that must survive: this sentence is DELAY_SOWING_10D."""
    return (rules["actions"]["DELAY_SOWING_10D"].get("keywords") or {}).get(language)


def check(provider, language: str, text: str, rules: dict) -> dict:
    started = time.perf_counter()
    try:
        result = provider.translate(text, language, source_language="en")
    except Exception as exc:  # noqa: BLE001 - reporting is the whole job
        return {"ok": False, "stage": "call", "kind": classify(exc),
                "error": redact(str(exc))[:300],
                "latency_ms": (time.perf_counter() - started) * 1000}

    latency = result.latency_ms or (time.perf_counter() - started) * 1000
    keyword = keyword_for(language, rules)
    try:
        # "10 days" is part of the source sentence, so 10 is a legitimate number.
        validate_text(result.text, language=language, action_keyword=keyword,
                      allowed_numbers=["10"], source_text=text)
        return {"ok": True, "text": result.text, "latency_ms": latency,
                "keyword": keyword, "meta": result.meta}
    except ValidationError as exc:
        return {"ok": False, "stage": "validation", "kind": str(exc),
                "text": result.text, "latency_ms": latency, "keyword": keyword}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--languages", default="hi,mr")
    args = parser.parse_args()

    languages = [l.strip() for l in args.languages.split(",") if l.strip()]
    rules = load_config("rules")
    llm_cfg = load_config("llm")

    say("--- check_providers ---")
    say(f"  SARVAM: {key_state('SARVAM_API_KEY')}")
    say(f"  GEMINI: {key_state('GEMINI_API_KEY')}")
    say(f"  MOCK_MODE: {env('MOCK_MODE') or '(unset)'}")

    state = runtime_status()
    say(f"  language layer: {'MOCK (templates only)' if state['llm_mock'] else 'LIVE'}"
        f" - {state['llm_reason']}")
    say(f"  sender:         {'MOCK' if state['sender_mock'] else 'LIVE'}"
        f" - {state['sender_reason']}")
    say()
    say(f'  test sentence: "{args.text}"')
    say()

    providers = [
        ("sarvam", SarvamProvider(llm_cfg),
         llm_cfg["providers"]["sarvam"]["translate_model"]),
        ("gemini", GeminiProvider(llm_cfg), llm_cfg["providers"]["gemini"]["model"]),
    ]

    results = []
    for name, provider, model in providers:
        say(f"  [{name}] model={model}")
        if not provider.available():
            say(f"      SKIPPED - no key set for {provider.settings['api_key_env']}")
            results.append((name, "-", False, "no key"))
            say()
            continue

        for language in languages:
            outcome = check(provider, language, args.text, rules)
            label = LANGUAGE_NAMES.get(language, language)
            if outcome["ok"]:
                say(f"      {label:<8} PASS  {outcome['latency_ms']:.0f} ms")
                say(f"               {outcome['text']}")
                if outcome.get("keyword"):
                    say(f"               keyword '{outcome['keyword']}' present, "
                        f"{len(outcome['text'])}/300 chars")
                else:
                    say(f"               no keyword configured for {label}; "
                        f"{len(outcome['text'])}/300 chars")
                results.append((name, language, True, ""))
            elif outcome["stage"] == "call":
                say(f"      {label:<8} FAIL  ({outcome['kind']})")
                say(f"               {outcome['error']}")
                results.append((name, language, False, outcome["kind"]))
            else:
                say(f"      {label:<8} FAIL  validation: {outcome['kind']}")
                say(f"               returned: {outcome['text'][:160]}")
                results.append((name, language, False, "validation"))
        say()

    passed = sum(1 for _, _, ok, _ in results if ok)
    say(f"  {passed} of {len(results)} checks passed")
    for name, language, ok, note in results:
        if not ok:
            say(f"      - {name}/{language}: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
