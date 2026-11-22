"""Deterministic offline provider.

This is not a toy. It drives the entire default test suite, so it must
reproduce the awkward shapes the real corpus contains -- absent symbols, "--"
sentinels, zero-quantity rows, per-account institutions and aggregation flags
-- rather than an idealised version of them. A pipeline that only ever sees
clean mock data will fall over on the first real document.

For CSV input it performs a genuine deterministic parse, so integration tests
exercise real values. For PDF input it replays a registered fixture, because
parsing a PDF without a model is exactly the problem this project exists to
avoid solving.
"""

from __future__ import annotations

import csv
import io
import re
import time
from typing import Final

from ..canonical import (
    SCHEMA_VERSION,
    AccountDescriptor,
    DocumentOverview,
    ExtractedAccount,
    ExtractedPortfolioDocument,
    ExtractedPosition,
)
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
from ..prompts.versions import OVERVIEW_PROMPT_VERSION, POSITIONS_PROMPT_VERSION

#: Column headers we understand, lowercased. Extra columns are ignored, and a
#: missing column yields null rather than an error.
_HEADER_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "account_key": ("account number", "account", "account #"),
    "account_name": ("account name", "account description"),
    "symbol": ("symbol", "ticker"),
    "description": ("description", "security description", "name"),
    "quantity": ("quantity", "shares", "qty"),
    "price": ("last price", "price", "share price"),
    "market_value": ("current value", "market value", "value", "current balance"),
    "cost_basis": ("cost basis total", "cost basis", "total cost"),
    "gain_loss": ("total gain/loss dollar", "unrealized gain/loss", "gain/loss"),
}

_NULLISH: Final[frozenset[str]] = frozenset({"", "--", "n/a", "na", "none"})

#: A UUID-shaped account key indicates an externally aggregated account.
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

#: Institution names we can recognise inside an aggregated account's label.
_KNOWN_INSTITUTIONS: Final[tuple[str, ...]] = (
    "vanguard",
    "fidelity",
    "schwab",
    "etrade",
    "e*trade",
    "merrill",
    "morgan stanley",
    "tiaa",
    "t. rowe price",
    "hp inc",
)


def _clean(value: str | None) -> str | None:
    """Map the document's null sentinels to None, preserving everything else."""
    if value is None:
        return None
    stripped = value.strip()
    return None if stripped.lower() in _NULLISH else stripped


def _map_headers(header: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower().lstrip("﻿") for h in header]
    mapping: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for idx, name in enumerate(lowered):
            if name in aliases:
                mapping[field] = idx
                break
    return mapping


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str | None:
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return None
    return _clean(row[idx])


def _guess_institution(account_key: str, account_name: str, producer: str) -> tuple[str, bool]:
    """Return (institution, is_aggregated) using only document-visible signals."""
    aggregated = bool(_UUID_RE.match(account_key))
    haystack = account_name.lower()
    for known in _KNOWN_INSTITUTIONS:
        if known in haystack:
            return known.title(), aggregated
    return ("unknown" if aggregated else producer), aggregated


def _account_type_hint(account_name: str) -> str | None:
    name = account_name.lower()
    for needle, hint in (
        ("roth", "Roth IRA"),
        ("rollover", "Rollover IRA"),
        ("529", "529"),
        ("college savings", "529"),
        ("traditional ira", "Traditional IRA"),
        ("joint", "Joint"),
        ("corporation", "Corporate"),
        ("equity plan", "Equity Plan"),
        ("hsa", "HSA"),
        ("trust", "Trust"),
    ):
        if needle in name:
            return hint
    if "ira" in name:
        return "IRA"
    return None


def _split_rows(text: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Split a CSV into header, data rows, and trailing free-text lines.

    Real exports append disclaimers and a download-date footer after a blank
    line. Those rows are not data, but the date among them may be the only one
    the document carries, so they are returned rather than discarded.
    """
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return [], [], []
    header = reader[0]
    width = len(header)
    data: list[list[str]] = []
    trailing: list[str] = []
    for row in reader[1:]:
        # A data row has the full column count (a trailing comma yields one
        # extra empty field) and a non-empty first cell.
        if len(row) >= width and row and row[0].strip():
            data.append(row)
        elif any(cell.strip() for cell in row):
            trailing.append(" ".join(c for c in row if c.strip()))
    return header, data, trailing


_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"([A-Z][a-z]{2}-\d{1,2}-\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
)


def _find_date(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _DATE_RE.search(line)
        if match:
            return match.group(1), line.strip()
    return None, None


class MockExtractionProvider:
    """Offline provider: real CSV parsing, replayed PDF fixtures."""

    def __init__(self, pdf_fixtures: dict[str, ExtractedPortfolioDocument] | None = None) -> None:
        #: Keyed by sha256, so a fixture is bound to exact bytes.
        self._pdf_fixtures = dict(pdf_fixtures or {})

    @property
    def name(self) -> str:
        return "mock"

    def register_pdf_fixture(self, sha256: str, document: ExtractedPortfolioDocument) -> None:
        self._pdf_fixtures[sha256] = document

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_pdf=True,
            supports_csv=True,
            supports_structured_output=True,
            max_document_bytes=None,
            max_pdf_pages=None,
            incurs_cost=False,
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, detail="mock provider is always available", model=None)

    # -- stage 1 ------------------------------------------------------------
    def understand_document(
        self, document: SourceDocument, context: ExtractionContext
    ) -> OverviewResult:
        started = time.perf_counter()
        full = self._extract_all(document)
        overview = DocumentOverview(
            schema_version=SCHEMA_VERSION,
            document_type=full.document_type,
            producing_institution=full.producing_institution,
            as_of_date=full.as_of_date,
            as_of_date_source=full.as_of_date_source,
            reported_total_value=full.reported_total_value,
            accounts=[
                AccountDescriptor(
                    external_account_key=a.external_account_key,
                    display_name=a.display_name,
                    institution=a.institution,
                    is_aggregated=a.is_aggregated,
                    account_type_hint=a.account_type_hint,
                    reported_value=a.reported_value,
                    expected_position_count=len(a.positions),
                )
                for a in full.accounts
            ],
            warnings=list(full.warnings),
        )
        return OverviewResult(
            overview=overview,
            provider=self.name,
            model=None,
            prompt_version=OVERVIEW_PROMPT_VERSION,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # -- stage 2 ------------------------------------------------------------
    def extract_positions(
        self,
        document: SourceDocument,
        overview: DocumentOverview,
        context: ExtractionContext,
    ) -> ExtractionResult:
        started = time.perf_counter()
        full = self._extract_all(document)
        if context.account_keys:
            wanted = set(context.account_keys)
            full = full.model_copy(
                update={"accounts": [a for a in full.accounts if a.external_account_key in wanted]}
            )
        return ExtractionResult(
            document=full,
            provider=self.name,
            model=None,
            prompt_version=POSITIONS_PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # -- internals ----------------------------------------------------------
    def _extract_all(self, document: SourceDocument) -> ExtractedPortfolioDocument:
        if document.kind is DocumentKind.PDF:
            fixture = self._pdf_fixtures.get(document.sha256)
            if fixture is None:
                raise ExtractionError(
                    ExtractionErrorKind.UNSUPPORTED_DOCUMENT,
                    "mock provider cannot parse a PDF without a registered fixture",
                    detail=f"sha256={document.sha256[:12]}",
                )
            return fixture
        return self._parse_csv(document)

    def _parse_csv(self, document: SourceDocument) -> ExtractedPortfolioDocument:
        header, rows, trailing = _split_rows(document.text)
        if not header:
            raise ExtractionError(ExtractionErrorKind.UNSUPPORTED_DOCUMENT, "document is empty")
        mapping = _map_headers(header)
        if "account_key" not in mapping:
            raise ExtractionError(
                ExtractionErrorKind.UNSUPPORTED_DOCUMENT,
                "no recognisable account-number column",
                detail=f"headers={header[:6]}",
            )

        producer = "Fidelity" if "fidelity" in document.filename.lower() else "unknown"
        as_of, as_of_source = _find_date(trailing)

        ordered: list[str] = []
        by_key: dict[str, ExtractedAccount] = {}
        for row in rows:
            key = _cell(row, mapping, "account_key") or ""
            if not key:
                continue
            name = _cell(row, mapping, "account_name") or key
            if key not in by_key:
                institution, aggregated = _guess_institution(key, name, producer)
                by_key[key] = ExtractedAccount(
                    external_account_key=key,
                    display_name=name,
                    institution=institution,
                    is_aggregated=aggregated,
                    account_type_hint=_account_type_hint(name),
                    reported_value=None,
                    positions=[],
                )
                ordered.append(key)
            by_key[key].positions.append(
                ExtractedPosition(
                    symbol=_cell(row, mapping, "symbol"),
                    description=_cell(row, mapping, "description") or "",
                    quantity=_cell(row, mapping, "quantity"),
                    price=_cell(row, mapping, "price"),
                    market_value=_cell(row, mapping, "market_value"),
                    cost_basis=_cell(row, mapping, "cost_basis"),
                    unrealized_gain_loss=_cell(row, mapping, "gain_loss"),
                )
            )

        return ExtractedPortfolioDocument(
            schema_version=SCHEMA_VERSION,
            document_type="positions_export",
            producing_institution=producer,
            as_of_date=as_of,
            as_of_date_source=as_of_source,
            reported_total_value=None,
            accounts=[by_key[k] for k in ordered],
            warnings=[],
        )
