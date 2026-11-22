"""The extraction provider boundary.

Everything in this module is provider-agnostic by construction. No type from
``anthropic`` (or any other vendor SDK) may appear here or cross this boundary
-- that is what makes swapping in a local DGX model a change confined to one
file under ``providers/``.

Providers raise only :class:`ExtractionError` and its subclasses. A provider
that leaks a vendor exception is a bug.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .canonical import DocumentOverview, ExtractedPortfolioDocument


class DocumentKind(StrEnum):
    CSV = "csv"
    PDF = "pdf"
    TEXT = "text"


class ExtractionErrorKind(StrEnum):
    """Why an extraction failed, in terms the pipeline can act on."""

    #: Transient; retrying later may succeed.
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    #: Permanent for this configuration; retrying will not help.
    AUTHENTICATION = "authentication"
    MODEL_NOT_FOUND = "model_not_found"
    UNSUPPORTED_DOCUMENT = "unsupported_document"
    #: The model declined to process the document.
    REFUSAL = "refusal"
    #: The model responded but the output did not satisfy the schema.
    INVALID_OUTPUT = "invalid_output"
    #: The document exceeds a hard limit. Never silently truncated.
    DOCUMENT_TOO_LARGE = "document_too_large"
    UNKNOWN = "unknown"


class ExtractionError(Exception):
    """The only exception type a provider may raise."""

    def __init__(
        self,
        kind: ExtractionErrorKind,
        message: str,
        *,
        retryable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.detail = detail

    def __str__(self) -> str:
        base = f"[{self.kind}] {super().__str__()}"
        return f"{base} ({self.detail})" if self.detail else base


@dataclass(frozen=True)
class SourceDocument:
    """A document to extract, already read into memory.

    ``sha256`` is the deduplication key and the only safe thing to log about
    the content: the bytes themselves are a brokerage statement.
    """

    filename: str
    kind: DocumentKind
    content: bytes
    sha256: str

    @classmethod
    def from_path(cls, path: Path) -> SourceDocument:
        data = path.read_bytes()
        suffix = path.suffix.lower()
        kind = {
            ".csv": DocumentKind.CSV,
            ".pdf": DocumentKind.PDF,
        }.get(suffix, DocumentKind.TEXT)
        return cls(
            filename=path.name,
            kind=kind,
            content=data,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    @property
    def text(self) -> str:
        """Decoded text for CSV/text documents. UTF-8 BOM is stripped."""
        if self.kind is DocumentKind.PDF:
            raise ValueError("PDF content is binary; pass it to the provider as bytes")
        return self.content.decode("utf-8-sig", errors="replace")

    def describe(self) -> str:
        """A log-safe description. Never includes content."""
        return (
            f"{self.filename} ({self.kind}, {len(self.content)} bytes, sha256={self.sha256[:12]})"
        )


@dataclass(frozen=True)
class ExtractionContext:
    """Per-call knobs the pipeline passes down to a provider."""

    #: Accounts to extract in this call. Empty means "all of them".
    account_keys: tuple[str, ...] = ()
    #: Populated on a recovery attempt so the prompt can name what failed.
    reconciliation_failures: tuple[str, ...] = ()
    attempt_no: int = 1
    #: Pages the minimizer removed, recorded for the audit trail.
    dropped_pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can actually do, so the pipeline can plan around it."""

    name: str
    supports_pdf: bool
    supports_csv: bool
    supports_structured_output: bool
    #: None means "no meaningful limit" (e.g. the mock provider).
    max_document_bytes: int | None
    max_pdf_pages: int | None
    #: False for the mock and any offline provider; drives cost warnings.
    incurs_cost: bool


@dataclass
class ProviderHealth:
    """Result of a cheap liveness probe. Never raises."""

    healthy: bool
    detail: str
    model: str | None = None


@dataclass
class ExtractionResult:
    """What a provider returns, plus the metadata the audit trail needs."""

    document: ExtractedPortfolioDocument
    provider: str
    model: str | None
    prompt_version: str
    schema_version: str
    latency_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OverviewResult:
    """Stage 1 output plus metadata."""

    overview: DocumentOverview
    provider: str
    model: str | None
    prompt_version: str
    latency_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class DocumentExtractionProvider(Protocol):
    """The interface every extraction backend implements."""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> ProviderCapabilities: ...

    def health(self) -> ProviderHealth: ...

    def understand_document(
        self, document: SourceDocument, context: ExtractionContext
    ) -> OverviewResult:
        """Stage 1: identify the document and the set of accounts it contains."""
        ...

    def extract_positions(
        self,
        document: SourceDocument,
        overview: DocumentOverview,
        context: ExtractionContext,
    ) -> ExtractionResult:
        """Stage 2: read the holdings for the accounts named in the context."""
        ...
