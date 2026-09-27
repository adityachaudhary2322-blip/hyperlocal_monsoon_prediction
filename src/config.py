"""One settings lookup for both environments: `st.secrets` hosted, `.env` locally.

The variable names are identical in both, so no calling code branches on where it is
running. `DATABASE_URL` is `DATABASE_URL` whether it came from
`.streamlit/secrets.toml`, from the Streamlit Cloud secrets box, or from `.env`.

Precedence, highest first:

1. a real environment variable - so a shell or a container can always override;
2. `st.secrets` - the only writable channel on Streamlit Community Cloud;
3. `.env` via python-dotenv - the laptop.

`st.secrets` is read defensively. Importing streamlit is cheap, but *touching*
`st.secrets` raises when no secrets file exists, and it emits a bare-mode warning when
it is read outside a script run - so a missing file must look like "not configured",
never like a crash. It is also read through a cache: on Streamlit Cloud the secrets
never change during a container's life, and re-reading them on every rerun of every
page shows up in the memory budget.

Secrets are never printed. `describe()` reports only which names are set.
"""

from __future__ import annotations

import functools
import os
from typing import Any

TRUTHY = {"1", "true", "yes", "on", "y", "t"}

# Everything the app reads, so `describe()` and the settings page can report on the
# whole set rather than on whatever happens to be looked up.
KNOWN_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_CLOUD",
    "HOSTED_MODE",
    "MOCK_MODE",
    "ENABLE_CHRONOS",
    "ADMIN_PASSWORD",
    "SARVAM_API_KEY",
    "GEMINI_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM",
    "HF_MODEL_REPO",
    "HF_TOKEN",
)

# Anything whose value must never reach a log, a page or an error message.
SECRET_KEYS = frozenset({
    "DATABASE_URL", "DATABASE_URL_CLOUD", "ADMIN_PASSWORD", "SARVAM_API_KEY",
    "GEMINI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "HF_TOKEN",
})


def _dotenv_loaded() -> bool:
    """Load .env once, through the single entry point in src.runtime.

    Deliberately not cached here: `src.runtime._load_env_once` already is, and it is
    what the tests monkeypatch to keep a developer's real .env out of a test run. A
    second cache in front of it would swallow that patch.
    """
    from src.runtime import _load_env_once

    _load_env_once()
    return True


@functools.lru_cache(maxsize=1)
def _secrets() -> dict[str, Any]:
    """A plain dict of st.secrets, or {} when there are none.

    Flattened one level, so a `[twilio]` table in secrets.toml is reachable as
    TWILIO_ACCOUNT_SID as well as twilio.account_sid - Streamlit Cloud's UI encourages
    sections, and the brief requires the same variable names in both environments.
    """
    try:
        import streamlit as st
    except ModuleNotFoundError:          # a CLI run without streamlit installed
        return {}

    try:
        raw = st.secrets
        flat: dict[str, Any] = {}
        for key in raw:
            value = raw[key]
            if hasattr(value, "keys"):       # a [section] table
                for inner in value:
                    flat[f"{key}_{inner}".upper()] = value[inner]
            else:
                flat[str(key)] = value
        return flat
    except Exception:
        # No secrets.toml, or read outside a script run. Both mean "not configured".
        return {}


def hosted() -> bool:
    """True when running as the deployed website.

    HOSTED_MODE is explicit rather than sniffed, because the behaviour it gates -
    committed models, no local pipeline, Postgres only - has to be reproducible on the
    laptop for testing. Streamlit Cloud's own marker is accepted as a fallback so a
    forgotten secret does not silently run the laptop code path in production.
    """
    explicit = get("HOSTED_MODE")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip().lower() in TRUTHY
    return bool(os.environ.get("STREAMLIT_RUNTIME_ENV")) or \
        os.environ.get("HOSTNAME", "").startswith("streamlit")


def get(name: str, default: str | None = None) -> str | None:
    """The configured value for `name`, or `default`. Never raises."""
    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    from_secrets = _secrets().get(name)
    if from_secrets is not None and str(from_secrets).strip():
        return str(from_secrets).strip()

    _dotenv_loaded()
    value = os.environ.get(name, default)
    return str(value).strip() if isinstance(value, str) else value


def flag(name: str, default: bool = False) -> bool:
    raw = get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in TRUTHY


def source_of(name: str) -> str:
    """Where `name` came from: environment, st.secrets, .env, or unset."""
    if os.environ.get(name):
        return "environment"
    if name in _secrets():
        return "st.secrets"
    _dotenv_loaded()
    if os.environ.get(name):
        return ".env"
    return "unset"


def describe() -> dict[str, str]:
    """Which settings are configured. Values are reduced to set/missing."""
    out = {"hosted_mode": str(hosted()), "st.secrets": str(bool(_secrets()))}
    for key in KNOWN_KEYS:
        present = bool(get(key))
        if key in SECRET_KEYS:
            out[key] = "set" if present else "missing"
        else:
            out[key] = (get(key) or "unset") if present else "unset"
    return out


def reset_cache() -> None:
    """Drop the cached secrets and .env reads. Tests and reloads need this."""
    from src.runtime import _load_env_once

    # A test that has monkeypatched the loader to a no-op leaves a plain function
    # here, with no cache to clear - and that is exactly the state it wants.
    clear = getattr(_load_env_once, "cache_clear", None)
    if clear is not None:
        clear()
    _secrets.cache_clear()


def main() -> int:
    """`python -m src.config` prints the configuration without any values."""
    print("--- config ---")
    for key, value in describe().items():
        print(f"  {key:<24}{value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
