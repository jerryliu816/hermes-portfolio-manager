"""Claude-backed extraction provider.

This is the ONLY module in the project that imports ``anthropic``. Every
vendor-specific concern -- request shape, beta flags, error taxonomy, PDF
encoding, token accounting -- is contained here and converted to the neutral
types in ``contracts``. Swapping in a local model means writing a sibling of
this file and changing one config value.

Notable API details, verified against the installed SDK rather than recalled:

* ``client.beta.messages.parse(..., output_format=<PydanticModel>)`` returns a
  response whose ``.parsed_output`` is a validated instance. The beta path is
  used (not ``client.messages.parse``) because only it accepts ``betas`` and
  ``fallbacks``.
* Thinking is ``{"type": "adaptive"}``. ``budget_tokens`` is rejected with a
  400 on Opus 5.
* Server-side refusal fallback is enabled so a safety classifier declining a
  financial document reroutes instead of failing the import outright.
* A PDF document block must precede the text block in the same message.
* The system prompt is marked cacheable: it is byte-identical across every
  import, so it should be paid for once rather than per document.
"""

from __future__ import annotations

import base64
import time
from typing import Any, TypeVar, cast

import anthropic
from anthropic.types.beta import (
    BetaBase64PDFSourceParam,
    BetaContentBlockParam,
    BetaOutputConfigParam,
    BetaRequestDocumentBlockParam,
    BetaTextBlockParam,
)
from pydantic import BaseModel, ValidationError

from ...config import Settings, get_settings
from ..canonical import SCHEMA_VERSION, DocumentOverview, ExtractedPortfolioDocument
from ..contracts import (
    DocumentKind,
    ExtractionContext,
    ExtractionError,
    ExtractionErrorKind,
    ExtractionResult,
    OverviewResult,
    ProviderCapabilities,
    ProviderHealth,
    SourceDocument,
)
from ..prompts.portfolio import (
    OVERVIEW_SYSTEM,
    POSITIONS_SYSTEM,
    overview_note,
    overview_user_prompt,
    positions_user_prompt,
)
from ..prompts.recovery import RECOVERY_SYSTEM, recovery_user_prompt
from ..prompts.versions import (
    OVERVIEW_PROMPT_VERSION,
    POSITIONS_PROMPT_VERSION,
    RECOVERY_PROMPT_VERSION,
)

#: Enables `fallbacks="default"`, which reroutes a classifier refusal by
#: category instead of failing. Financial documents full of personal names and
#: account numbers are exactly the shape that can trip a classifier.
_REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: Generous: a 14-account aggregation produces a large structured response, and
#: truncation would silently drop accounts.
_MAX_TOKENS = 32000

TModel = TypeVar("TModel", bound=BaseModel)


class AnthropicExtractionProvider:
    """Extraction backed by the Claude Messages API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        key = self._settings.anthropic_api_key.get_secret_value()
        if not key:
            raise ExtractionError(
                ExtractionErrorKind.AUTHENTICATION,
                "ANTHROPIC_API_KEY is not set",
                detail="set it in .env or the environment, or use EXTRACTION_PROVIDER=mock",
            )
        self._client = anthropic.Anthropic(
            api_key=key,
            timeout=self._settings.anthropic_timeout_seconds,
            max_retries=2,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._settings.anthropic_model

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_pdf=True,
            supports_csv=True,
            supports_structured_output=True,
            max_document_bytes=self._settings.extraction_max_bytes,
            max_pdf_pages=self._settings.extraction_max_pages,
            incurs_cost=True,
        )

    def health(self) -> ProviderHealth:
        """Cheap liveness probe. Never raises -- callers get a verdict."""
        try:
            self._client.models.retrieve(self.model)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not throw
            return ProviderHealth(healthy=False, detail=str(exc)[:200], model=self.model)
        return ProviderHealth(healthy=True, detail="model reachable", model=self.model)

    # -- stage 1 ------------------------------------------------------------
    def understand_document(
        self, document: SourceDocument, context: ExtractionContext
    ) -> OverviewResult:
        self._guard_size(document)
        started = time.perf_counter()
        overview, usage = self._call(
            system=OVERVIEW_SYSTEM,
            user_text=overview_user_prompt(document.filename, document.kind.value),
            document=document,
            output_model=DocumentOverview,
        )
        return OverviewResult(
            overview=overview,
            provider=self.name,
            model=self.model,
            prompt_version=OVERVIEW_PROMPT_VERSION,
            latency_ms=int((time.perf_counter() - started) * 1000),
            token_usage=usage,
        )

    # -- stage 2 ------------------------------------------------------------
    def extract_positions(
        self,
        document: SourceDocument,
        overview: DocumentOverview,
        context: ExtractionContext,
    ) -> ExtractionResult:
        self._guard_size(document)
        started = time.perf_counter()

        recovering = bool(context.reconciliation_failures)
        if recovering:
            system = RECOVERY_SYSTEM
            user_text = recovery_user_prompt(
                document.filename, document.kind.value, context.reconciliation_failures
            )
            prompt_version = RECOVERY_PROMPT_VERSION
        else:
            system = POSITIONS_SYSTEM
            institutions = sorted({a.institution for a in overview.accounts})
            user_text = positions_user_prompt(
                document.filename,
                document.kind.value,
                context.account_keys,
                overview_note(overview.document_type, institutions, len(overview.accounts)),
            )
            prompt_version = POSITIONS_PROMPT_VERSION

        parsed, usage = self._call(
            system=system,
            user_text=user_text,
            document=document,
            output_model=ExtractedPortfolioDocument,
        )
        return ExtractionResult(
            document=parsed,
            provider=self.name,
            model=self.model,
            prompt_version=prompt_version,
            schema_version=SCHEMA_VERSION,
            latency_ms=int((time.perf_counter() - started) * 1000),
            token_usage=usage,
        )

    # -- internals ----------------------------------------------------------
    def _guard_size(self, document: SourceDocument) -> None:
        """Refuse an oversized document rather than truncating it.

        Silent truncation would drop holdings while leaving the surviving
        arithmetic self-consistent -- an undetectable corruption.
        """
        limit = self._settings.extraction_max_bytes
        if len(document.content) > limit:
            raise ExtractionError(
                ExtractionErrorKind.DOCUMENT_TOO_LARGE,
                f"document is {len(document.content)} bytes, over the {limit}-byte limit",
                detail="split the document; it will never be silently truncated",
            )

    def _content_blocks(
        self, document: SourceDocument, user_text: str
    ) -> list[BetaContentBlockParam]:
        """Build the user message. A PDF block must come BEFORE the text block."""
        if document.kind is DocumentKind.PDF:
            pdf = BetaRequestDocumentBlockParam(
                type="document",
                source=BetaBase64PDFSourceParam(
                    type="base64",
                    media_type="application/pdf",
                    data=base64.b64encode(document.content).decode("ascii"),
                ),
            )
            return [pdf, BetaTextBlockParam(type="text", text=user_text)]
        return [
            BetaTextBlockParam(
                type="text",
                text=f"{user_text}\n\n--- DOCUMENT ---\n{document.text}",
            )
        ]

    def _call(
        self,
        *,
        system: str,
        user_text: str,
        document: SourceDocument,
        output_model: type[TModel],
    ) -> tuple[TModel, dict[str, int]]:
        try:
            response = self._client.beta.messages.parse(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                # Marked cacheable: byte-identical on every import.
                system=[
                    BetaTextBlockParam(
                        type="text",
                        text=system,
                        cache_control={"type": "ephemeral"},
                    )
                ],
                messages=[{"role": "user", "content": self._content_blocks(document, user_text)}],
                output_format=output_model,
                output_config=BetaOutputConfigParam(
                    effort=cast(Any, self._settings.extraction_effort)
                ),
                thinking={"type": "adaptive"},
                betas=[_REFUSAL_FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.NotFoundError as exc:
            raise ExtractionError(
                ExtractionErrorKind.MODEL_NOT_FOUND,
                f"model {self.model!r} is not available to this API key",
                detail=str(exc)[:200],
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise ExtractionError(
                ExtractionErrorKind.AUTHENTICATION,
                "the Anthropic API rejected the credentials",
                detail=str(exc)[:200],
            ) from exc
        except anthropic.RateLimitError as exc:
            raise ExtractionError(
                ExtractionErrorKind.RATE_LIMITED,
                "rate limited by the Anthropic API",
                retryable=True,
                detail=str(exc)[:200],
            ) from exc
        except anthropic.APIStatusError as exc:
            raise ExtractionError(
                ExtractionErrorKind.UNKNOWN,
                f"Anthropic API returned status {exc.status_code}",
                retryable=exc.status_code >= 500,
                detail=str(exc)[:200],
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ExtractionError(
                ExtractionErrorKind.CONNECTION,
                "could not reach the Anthropic API",
                retryable=True,
                detail=str(exc)[:200],
            ) from exc

        # Check the stop reason BEFORE touching content: on a refusal the
        # content blocks are not the extraction you asked for.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise ExtractionError(
                ExtractionErrorKind.REFUSAL,
                "the model declined to process this document",
                detail=f"category={category}",
            )
        if stop_reason == "max_tokens":
            raise ExtractionError(
                ExtractionErrorKind.INVALID_OUTPUT,
                "the response hit the token ceiling and would be truncated",
                retryable=True,
                detail="extract fewer accounts per call (EXTRACTION_ACCOUNTS_PER_CALL)",
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ExtractionError(
                ExtractionErrorKind.INVALID_OUTPUT,
                "the model returned no structured output",
                retryable=True,
                detail=f"stop_reason={stop_reason}",
            )
        if not isinstance(parsed, output_model):
            try:
                parsed = output_model.model_validate(parsed)
            except ValidationError as exc:
                raise ExtractionError(
                    ExtractionErrorKind.INVALID_OUTPUT,
                    "structured output did not match the canonical schema",
                    retryable=True,
                    detail=str(exc)[:300],
                ) from exc

        return parsed, self._usage(response)

    @staticmethod
    def _usage(response: object) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        out: dict[str, int] = {}
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = getattr(usage, field, None)
            if isinstance(value, int):
                out[field] = value
        return out
