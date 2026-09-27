"""Mock mode: the one switch that decides whether anything leaves this machine.

A single rule, applied identically to the language providers and the message sender:

    mock = MOCK_MODE is true  OR  the credentials for that service are absent

So the app runs end to end today with no keys, and the day keys land in `.env` and
`MOCK_MODE=false` is set, the same code paths start calling the real services. No
branches to delete, no code to change.

`.env` is loaded here (python-dotenv), and `src.config` layers `st.secrets` on top for
the hosted site, so every entry point - Streamlit, the pipeline, the tests - sees the
same configuration under the same variable names.
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

from src.common import ROOT

TRUTHY = {"1", "true", "yes", "on"}

LLM_KEYS = ("SARVAM_API_KEY", "GEMINI_API_KEY")
TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")


@lru_cache(maxsize=1)
def _load_env_once() -> None:
    load_dotenv(ROOT / ".env", override=False)


def env(name: str, default: str | None = None) -> str | None:
    """Delegates to src.config, which also reads st.secrets on Streamlit Cloud.

    Kept as the name the rest of src/ already imports, so adding the hosted settings
    source did not mean touching every call site.
    """
    from src.config import get

    return get(name, default)


def mock_mode_forced() -> bool:
    """True when MOCK_MODE is explicitly on."""
    return (env("MOCK_MODE", "") or "").lower() in TRUTHY


def _has_all(names: tuple[str, ...]) -> bool:
    return all(env(name) for name in names)


def llm_is_mock() -> bool:
    """Template-only: forced, or neither language provider has a key."""
    if mock_mode_forced():
        return True
    return not any(env(name) for name in LLM_KEYS)


def sender_is_mock() -> bool:
    """Write to the messages table instead of calling Twilio."""
    if mock_mode_forced():
        return True
    return not _has_all(TWILIO_KEYS)


def llm_mock_reason() -> str:
    if mock_mode_forced():
        return "MOCK_MODE is on"
    missing = [n for n in LLM_KEYS if not env(n)]
    return f"no API key set ({', '.join(missing)})"


def sender_mock_reason() -> str:
    if mock_mode_forced():
        return "MOCK_MODE is on"
    missing = [n for n in TWILIO_KEYS if not env(n)]
    return f"Twilio not configured ({', '.join(missing)})"


def status() -> dict:
    """Everything the UI needs to explain itself to the operator."""
    return {
        "mock_mode_forced": mock_mode_forced(),
        "llm_mock": llm_is_mock(),
        "llm_reason": llm_mock_reason() if llm_is_mock() else "API keys present",
        "sender_mock": sender_is_mock(),
        "sender_reason": (sender_mock_reason() if sender_is_mock()
                          else "Twilio configured"),
    }


# --------------------------------------------------------------------------
# Chronos-2 on CPU
# --------------------------------------------------------------------------
# Measured 2026-09-27 on 2 threads with CUDA disabled, 30 pilot units, 730-day
# context: 5 s to load the predictor plus 20 s to forecast one start date - 25 s
# against the 5-minute budget the deployment brief sets. So inference is not the
# blocker; see CHRONOS_OFF_REASON for what is.
CHRONOS_CPU_SECONDS = 25
CHRONOS_CPU_BUDGET_SECONDS = 300

CHRONOS_OFF_REASON = (
    "Chronos-2 inference fits the CPU budget (25 s measured against 300 s allowed), "
    "but the stacker that turns its quantiles into probabilities is fitted in memory "
    "by src.stack_models and never saved, so there is no artifact to load online. "
    "Until it is persisted, forecasts run on LightGBM."
)


def chronos_enabled() -> bool:
    """Whether the online forecast path may use Chronos-2.

    Off unless ENABLE_CHRONOS is explicitly on. The gate is a flag rather than a
    timing check because the measurement is already known and stable, and because a
    Space should not spend a minute benchmarking on every cold start.
    """
    return (env("ENABLE_CHRONOS", "") or "").lower() in TRUTHY
