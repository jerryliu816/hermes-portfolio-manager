"""The provider boundary itself.

The point of these tests is that the boundary stays vendor-neutral: if an
`anthropic` type ever leaks through it, swapping in a local model stops being
a one-file change.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from portfolio_manager.config import Settings
from portfolio_manager.extraction.contracts import (
    DocumentExtractionProvider,
    DocumentKind,
    ExtractionError,
    ExtractionErrorKind,
    SourceDocument,
)
from portfolio_manager.extraction.providers.local_llm_provider import LocalLLMExtractionProvider
from portfolio_manager.extraction.registry import get_provider

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)


def test_source_document_hashes_content_for_dedupe() -> None:
    doc = SourceDocument.from_path(FIXTURE)
    assert doc.sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert doc.kind is DocumentKind.CSV


def test_describe_never_leaks_document_content() -> None:
    """The only safe thing to log about a statement is its hash and size."""
    doc = SourceDocument.from_path(FIXTURE)
    described = doc.describe()
    assert doc.sha256[:12] in described
    for secret in ("Sample", "Joint WROS", "20100.00", "90000001"):
        assert secret not in described


def test_pdf_content_is_not_offered_as_text() -> None:
    pdf = SourceDocument(filename="s.pdf", kind=DocumentKind.PDF, content=b"%PDF-1.4", sha256="abc")
    with pytest.raises(ValueError, match="binary"):
        _ = pdf.text


def test_registry_returns_the_configured_provider() -> None:
    assert get_provider("mock").name == "mock"
    assert get_provider("local").name == "local"


def test_registry_rejects_an_unknown_provider() -> None:
    with pytest.raises(ExtractionError) as excinfo:
        get_provider("definitely-not-a-provider")
    assert "expected one of" in str(excinfo.value)


def test_anthropic_provider_refuses_to_start_without_a_key() -> None:
    """Fail at construction with a clear message, not mid-import."""
    from portfolio_manager.extraction.providers.anthropic_provider import (
        AnthropicExtractionProvider,
    )

    settings = Settings(anthropic_api_key="", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ExtractionError) as excinfo:
        AnthropicExtractionProvider(settings)
    assert excinfo.value.kind is ExtractionErrorKind.AUTHENTICATION
    assert "mock" in str(excinfo.value), "the error should name the offline alternative"


def test_local_provider_is_interface_complete_but_honest() -> None:
    """A stub must raise, never silently return empty results."""
    provider = LocalLLMExtractionProvider()
    assert isinstance(provider, DocumentExtractionProvider)
    assert provider.health().healthy is False
    with pytest.raises(ExtractionError):
        provider.understand_document(
            SourceDocument.from_path(FIXTURE),
            __import__(
                "portfolio_manager.extraction.contracts", fromlist=["ExtractionContext"]
            ).ExtractionContext(),
        )


def test_mock_path_does_not_require_the_vendor_sdk() -> None:
    """The registry imports `anthropic` lazily, so offline use stays offline."""
    for module in [m for m in sys.modules if m.startswith("anthropic")]:
        del sys.modules[module]
    get_provider("mock")
    assert not any(m.startswith("anthropic") for m in sys.modules), (
        "selecting the mock provider must not import the vendor SDK"
    )


def test_extraction_error_reports_kind_and_detail() -> None:
    err = ExtractionError(
        ExtractionErrorKind.RATE_LIMITED, "slow down", retryable=True, detail="retry-after 30s"
    )
    assert err.retryable is True
    assert "rate_limited" in str(err)
    assert "retry-after 30s" in str(err)


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (ExtractionErrorKind.RATE_LIMITED, True),
        (ExtractionErrorKind.TIMEOUT, True),
        (ExtractionErrorKind.AUTHENTICATION, False),
        (ExtractionErrorKind.MODEL_NOT_FOUND, False),
        (ExtractionErrorKind.DOCUMENT_TOO_LARGE, False),
    ],
)
def test_error_kinds_carry_a_retry_decision(kind: ExtractionErrorKind, retryable: bool) -> None:
    """The pipeline must be able to tell transient from permanent."""
    err = ExtractionError(kind, "boom", retryable=retryable)
    assert err.retryable is retryable
