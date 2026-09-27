"""Privacy scrubbing and output validation for the language layer.

Two separate jobs, deliberately in one place so neither can be bypassed:

* `scrub_payload` runs BEFORE any request leaves the process. It whitelists the
  fields a provider may see and aborts outright if anything phone-number-shaped
  appears anywhere in the payload. Subscriber data must never reach a third party.
* `validate_text` runs AFTER every generation, from every provider including the
  templates. A failure is not a warning - it sends the request to the next provider.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from src.common import load_config

DEVANAGARI = (0x0900, 0x097F)
LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi"}


class PrivacyError(RuntimeError):
    """Raised when a payload contains something that must never leave the process.

    This is never caught and retried: a leak is a bug, not a transient failure.
    """


class ValidationError(RuntimeError):
    """Raised when generated text fails a quality gate. Triggers the next provider."""


def _config() -> dict:
    return load_config("llm")


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------
def _walk(value: Any) -> list[str]:
    """Every string anywhere in a nested structure, including dict keys."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            out.extend(_walk(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            out.extend(_walk(item))
    elif value is not None:
        out.append(str(value))
    return out


def scan_for_private_data(payload: Any, cfg: dict | None = None) -> list[str]:
    """Names of the forbidden patterns found anywhere in `payload`."""
    cfg = cfg or _config()
    haystack = " \n".join(_walk(payload))
    found = []
    for rule in cfg["privacy"]["forbidden_patterns"]:
        if re.search(rule["pattern"], haystack):
            found.append(rule["name"])
    return found


def scrub_payload(payload: dict, cfg: dict | None = None) -> dict:
    """Whitelist the fields a provider may see, then assert nothing private remains.

    The order matters: scrub first so that a phone number sitting in a dropped field
    does not abort a legitimate request, then scan what actually survives. If the
    scan still trips, the caller put subscriber data in an advisory field and the
    request must not go out at all.
    """
    cfg = cfg or _config()
    allowed = set(cfg["privacy"]["allowed_fields"])
    scrubbed = {k: v for k, v in payload.items() if k in allowed}

    leaks = scan_for_private_data(scrubbed, cfg)
    if leaks:
        raise PrivacyError(
            f"refusing to call a provider: {', '.join(leaks)} found in the advisory "
            f"payload (fields {sorted(scrubbed)}). Subscriber data must never be "
            "sent to a language provider."
        )
    return scrubbed


# --------------------------------------------------------------------------
# Output validation
# --------------------------------------------------------------------------
def _script_of(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "none"
    devanagari = sum(1 for c in letters if DEVANAGARI[0] <= ord(c) <= DEVANAGARI[1])
    latin = sum(1 for c in letters
                if "LATIN" in unicodedata.name(c, ""))
    if devanagari > latin:
        return "devanagari"
    if latin > 0:
        return "latin"
    return "other"


#: Devanagari digits map onto ASCII so "१०" and "10" compare equal. Devanagari
#: numerals are perfectly correct in a Hindi or Marathi SMS - arguably more natural -
#: and rejecting them would have thrown away a complete, well-formed translation.
DEVANAGARI_DIGITS = str.maketrans("०१२३४"
                                  "५६७८९",
                                  "0123456789")


def _numbers_in(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text.translate(DEVANAGARI_DIGITS))


def validate_text(text: str, *, language: str,
                  action_keyword: "str | list[str] | None" = None,
                  allowed_numbers: list[str] | None = None, trusted: bool = False,
                  source_text: str | None = None, cfg: dict | None = None) -> None:
    """Raise ValidationError if the text is not safe to send to a farmer.

    `allowed_numbers` is normally the probability digits: "10 mein se 7" contributes
    "10" and "7". Every other digit is treated as a hallucinated quantity.

    `trusted=True` marks human-authored, cited text from rules.yaml. Length, script
    and the action keyword still apply, but the anti-hallucination guards (stray
    numbers, chemical doses) do not: those exist to catch a model inventing a dose,
    and the rule base legitimately carries CRIDA's own "2% urea or DAP" spray. Running
    them against the templates would reject the agronomy for looking like a
    hallucination.
    """
    cfg = cfg or _config()
    rules = cfg["validation"]
    text = (text or "").strip()

    if not text:
        raise ValidationError("empty text")

    if len(text) > rules["max_chars"]:
        raise ValidationError(
            f"{len(text)} characters exceeds the {rules['max_chars']} limit"
        )

    if rules.get("require_action_keyword") and action_keyword:
        # A list means "any of these spellings will do": Hindi writes sowing as both
        # बुवाई and बुआई, and rejecting a correct translation over a spelling variant
        # would push good text to the fallback for no reason.
        wanted = ([action_keyword] if isinstance(action_keyword, str)
                  else list(action_keyword))
        if not any(w.lower() in text.lower() for w in wanted if w):
            raise ValidationError(
                f"none of the action keywords {wanted} are present, so the text no "
                "longer says what the rule engine decided"
            )

    wanted = (rules.get("require_script") or {}).get(language)
    if wanted:
        actual = _script_of(text)
        if actual != wanted:
            raise ValidationError(
                f"{LANGUAGE_NAMES.get(language, language)} text came back in "
                f"{actual} script, expected {wanted}"
            )

    # A translation far shorter than its source has usually dropped a clause. This
    # is the only check that catches semantic loss: Gemini returned "a dry spell is
    # likely" for "Delay sowing by 10 days because a dry spell is likely" and passed
    # length, script, keyword and number checks.
    ratio = rules.get("min_translation_length_ratio")
    if source_text and ratio:
        if len(text) < ratio * len(source_text.strip()):
            raise ValidationError(
                f"translation is {len(text)} characters against a "
                f"{len(source_text.strip())}-character source "
                f"({len(text) / len(source_text.strip()):.0%}); a clause was "
                "probably dropped"
            )

    if trusted:
        return

    for pattern in rules.get("forbidden_patterns", []):
        match = re.search(pattern, text)
        if match:
            raise ValidationError(
                f"forbidden content {match.group(0)!r}: {rules['forbidden_reason']}"
            )

    if rules.get("allow_only_probability_numbers"):
        permitted = set(allowed_numbers or [])
        stray = [n for n in _numbers_in(text) if n not in permitted]
        if stray:
            raise ValidationError(
                f"unexpected number(s) {stray} - only the probability "
                f"({sorted(permitted) or 'none'}) may appear"
            )
