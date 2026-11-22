"""Domain enumerations.

These are stored as VARCHAR with a CHECK constraint rather than as native
Postgres enum types. Native enums do not autogenerate reliably under Alembic
(no ALTER TYPE support, and downgrades that cannot be expressed), whereas
adding a value here is a one-line CHECK change.
"""

from __future__ import annotations

from enum import StrEnum


class AccountType(StrEnum):
    BROKERAGE = "brokerage"
    JOINT = "joint"
    TRADITIONAL_IRA = "traditional_ira"
    ROLLOVER_IRA = "rollover_ira"
    ROTH_IRA = "roth_ira"
    FIVE_TWENTY_NINE = "529"
    HSA = "hsa"
    CORPORATE = "corporate"
    EQUITY_PLAN = "equity_plan"
    TRUST = "trust"
    UNKNOWN = "unknown"


class TaxTreatment(StrEnum):
    TAXABLE = "taxable"
    TAX_DEFERRED = "tax_deferred"
    TAX_FREE = "tax_free"
    EDUCATION = "education"
    UNKNOWN = "unknown"


class SecurityType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    MONEY_MARKET = "money_market"
    BOND = "bond"
    CASH = "cash"
    TARGET_DATE = "target_date"
    OTHER = "other"
    UNKNOWN = "unknown"


class AssetClass(StrEnum):
    US_EQUITY = "us_equity"
    INTL_EQUITY = "intl_equity"
    FIXED_INCOME = "fixed_income"
    REAL_ESTATE = "real_estate"
    COMMODITY = "commodity"
    CASH_EQUIVALENT = "cash_equivalent"
    MULTI_ASSET = "multi_asset"
    UNKNOWN = "unknown"


class CostBasisSource(StrEnum):
    """How a position's cost basis was obtained.

    ``DERIVED`` means market_value - unrealized_gain_loss. ``UNKNOWN`` is
    displayed honestly and never imputed -- 29% of the real corpus has no basis.
    """

    REPORTED = "reported"
    DERIVED = "derived"
    UNKNOWN = "unknown"


class ValueSource(StrEnum):
    """Provenance of a position's measurements.

    ``PRIMARY`` came from the authoritative source. ``ENRICHED`` was replaced
    atomically from an optional enrichment document.
    """

    PRIMARY = "primary"
    ENRICHED = "enriched"


class SourceRole(StrEnum):
    AUTHORITATIVE = "authoritative"
    ENRICHMENT = "enrichment"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ImportStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    VALIDATION_FAILED = "validation_failed"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    IMPORTED = "imported"
    FAILED = "failed"


class SkipReason(StrEnum):
    ZERO_REPORTED_VALUE = "zero_reported_value"


class ContextKind(StrEnum):
    """What the user has told the advisor, by the shape it arrived in."""

    #: A belief about what is likely to happen ("rates fall within a year").
    VIEW = "view"
    #: A standing preference ("no individual bank stocks").
    PREFERENCE = "preference"
    #: A page to read. The advisor fetches it live unless text was captured.
    URL = "url"
    #: An uploaded document, with whatever text it yielded.
    DOCUMENT = "document"


class ContextCreator(StrEnum):
    USER = "user"
    #: Captured by the advisor from a chat. Shown for the user to confirm, since
    #: a paraphrase of what someone believes can be wrong.
    HERMES = "hermes"


class TextExtraction(StrEnum):
    """Whether an item's `body` holds text, and why not when it does not."""

    #: Typed by a person; nothing to extract.
    NOT_APPLICABLE = "not_applicable"
    EXTRACTED = "extracted"
    #: A PDF with no text layer. Stated rather than guessed at.
    NO_TEXT = "no_text"
    #: A URL whose content has not been captured; read it live.
    NOT_CAPTURED = "not_captured"


class RecommendationKind(StrEnum):
    #: Free text: what to do and why, with no structured proposal.
    NOTE = "note"
    #: Specific trades, with the engine's simulation of them stored alongside.
    TRADES = "trades"
    #: New target weights. Applying them means the user editing their policy.
    TARGET_CHANGE = "target_change"
