"""Provider selection.

The only place that maps a configuration string to a concrete provider. Keeping
this in one function is what makes ``EXTRACTION_PROVIDER=local`` a config change
rather than a code change.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .contracts import DocumentExtractionProvider, ExtractionError, ExtractionErrorKind
from .providers.local_llm_provider import LocalLLMExtractionProvider
from .providers.mock_provider import MockExtractionProvider

KNOWN_PROVIDERS = ("mock", "anthropic", "local")


def get_provider(
    name: str | None = None, settings: Settings | None = None
) -> DocumentExtractionProvider:
    settings = settings or get_settings()
    chosen = (name or settings.extraction_provider).strip().lower()

    if chosen == "mock":
        return MockExtractionProvider()
    if chosen == "local":
        return LocalLLMExtractionProvider()
    if chosen == "anthropic":
        # Imported lazily so the mock path never needs the vendor SDK loaded.
        from .providers.anthropic_provider import AnthropicExtractionProvider

        return AnthropicExtractionProvider(settings)

    raise ExtractionError(
        ExtractionErrorKind.UNKNOWN,
        f"unknown extraction provider {chosen!r}",
        detail=f"expected one of: {', '.join(KNOWN_PROVIDERS)}",
    )
