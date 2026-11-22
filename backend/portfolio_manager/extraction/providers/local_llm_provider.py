"""Placeholder for a locally hosted model (e.g. on the DGX).

Deliberately a stub with a complete interface rather than an empty file: it
proves the provider boundary is genuinely implementable by something that is
not Claude, and it is what the benchmark command will compare against when the
time comes to decide whether a local model is good enough.

Every method raises rather than silently degrading -- a half-working extraction
provider is worse than an absent one.
"""

from __future__ import annotations

from ..canonical import DocumentOverview
from ..contracts import (
    ExtractionContext,
    ExtractionError,
    ExtractionErrorKind,
    ExtractionResult,
    OverviewResult,
    ProviderCapabilities,
    ProviderHealth,
    SourceDocument,
)

_NOT_IMPLEMENTED = "the local LLM provider is not implemented yet"


class LocalLLMExtractionProvider:
    """Interface-complete stub for a self-hosted extraction model."""

    def __init__(self, endpoint: str | None = None, model: str | None = None) -> None:
        self._endpoint = endpoint
        self._model = model

    @property
    def name(self) -> str:
        return "local"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_pdf=False,
            supports_csv=False,
            supports_structured_output=False,
            max_document_bytes=None,
            max_pdf_pages=None,
            incurs_cost=False,
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=False, detail=_NOT_IMPLEMENTED, model=self._model)

    def understand_document(
        self, document: SourceDocument, context: ExtractionContext
    ) -> OverviewResult:
        raise ExtractionError(ExtractionErrorKind.UNSUPPORTED_DOCUMENT, _NOT_IMPLEMENTED)

    def extract_positions(
        self,
        document: SourceDocument,
        overview: DocumentOverview,
        context: ExtractionContext,
    ) -> ExtractionResult:
        raise ExtractionError(ExtractionErrorKind.UNSUPPORTED_DOCUMENT, _NOT_IMPLEMENTED)
