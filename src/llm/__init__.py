"""API-based language layer: provider chain, validation, caching and cost logging.

No model weights are loaded locally. `TemplateProvider` is the floor and always
works offline; Sarvam and Gemini are tried first only when a key is present.
"""

from src.llm.providers import (  # noqa: F401
    GeminiProvider,
    ProviderChain,
    ProviderError,
    SarvamProvider,
    TemplateProvider,
    build_chain,
)
from src.llm.validation import (  # noqa: F401
    PrivacyError,
    ValidationError,
    scrub_payload,
    validate_text,
)
