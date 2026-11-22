"""The canonical extraction schema: what a provider must return.

This is the contract between "what does the document say?" and everything
downstream. It is deliberately provider-agnostic -- Claude, a local DGX model,
or a hand-written parser must all produce exactly this shape.

**Every monetary and quantity field is a string, not a Decimal or float.**
Two independent reasons, both load-bearing:

1. A Pydantic ``Decimal`` field emits ``anyOf: [number, string]`` in its JSON
   Schema, which does not survive strict structured-output generation.
2. A ``float`` cannot represent money. Keeping the wire type as a string means
   the value the model emitted is preserved byte-for-byte until the normalizer
   converts it to ``Decimal`` at the domain boundary, so a rounding bug can
   never be introduced by transport.

Strings here are also *unnormalized*: "$118936.42", "+60.27%", "--" and
"1,348.207" all arrive as written. Cleaning is the normalizer's job, and it is
deterministic and tested rather than delegated to the model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Bump when the shape below changes in a way that invalidates stored output.
#: Recorded on every ExtractionRun so old runs stay interpretable.
SCHEMA_VERSION = "1.1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedPosition(_Strict):
    """One holding as the document reports it, before any cleaning."""

    symbol: str | None = Field(
        description=(
            "Ticker symbol exactly as printed, or null when the document shows none. "
            "Many 529 and managed-portfolio holdings genuinely have no ticker -- "
            "return null rather than inventing one or copying the description."
        )
    )
    description: str = Field(description="Security name or description exactly as printed.")
    quantity: str | None = Field(
        description=(
            "Share/unit count exactly as printed, including any thousands separators. "
            "Null if the document does not show one. '--' means not reported: return null."
        )
    )
    price: str | None = Field(
        description="Per-unit price exactly as printed, e.g. '$117.51'. Null if absent."
    )
    market_value: str | None = Field(
        description=(
            "Current value of this holding exactly as printed, e.g. '$158427.80'. "
            "Null if the document shows '--' or leaves it blank."
        )
    )
    cost_basis: str | None = Field(
        description=(
            "Total cost basis exactly as printed. Null if absent. "
            "Do NOT compute it from other fields -- absence is meaningful."
        )
    )
    unrealized_gain_loss: str | None = Field(
        description=(
            "Total unrealized gain or loss exactly as printed, e.g. '+$44725.87'. "
            "Null if absent. Do NOT compute it."
        )
    )


class ExtractedAccount(_Strict):
    """One account and its holdings.

    ``institution`` is per account, not per document: an aggregation export can
    carry accounts from several institutions at once.
    """

    external_account_key: str = Field(
        description=(
            "The account identifier exactly as printed -- a number, a UUID, or "
            "whatever the document uses. Never invent or reformat it."
        )
    )
    display_name: str = Field(
        description="The account name exactly as printed, including any masking the source applied."
    )
    institution: str = Field(
        description=(
            "The institution that actually custodies this account. For an aggregation "
            "export this is often NOT the institution that produced the document: an "
            "account named 'Vanguard Brokerage - 12345678' inside a Fidelity export is "
            "custodied at Vanguard. Use 'unknown' when the document gives no signal."
        )
    )
    is_aggregated: bool = Field(
        description=(
            "True when this account is being reported second-hand by an aggregation "
            "feed rather than by its own custodian. Aggregated rows are typically "
            "missing gain/loss and may carry stale prices."
        )
    )
    account_type_hint: str | None = Field(
        description=(
            "Account type if the document states or clearly implies one "
            "(e.g. 'ROTH IRA', 'Rollover IRA', '529', 'Joint WROS'). "
            "Null when the name gives no reliable signal -- do not guess from a person's name."
        )
    )
    reported_value: str | None = Field(
        description=(
            "The account total the document itself states, if it states one. "
            "Null otherwise. Never sum the positions yourself to fill this in -- "
            "it exists to be checked against that sum."
        )
    )
    cash_balance: str | None = Field(
        default=None,
        description=(
            "A settlement fund, money-market sweep, or cash balance that the document "
            "reports for this account SEPARATELY from its holdings table -- often in an "
            "'account balance details' or 'cash' panel rather than as a holding row. "
            "Copy it exactly as printed. Null if the document shows none. This is real "
            "money and is frequently the entire reason an account's stated total exceeds "
            "the sum of its listed holdings, so do not skip it and do not compute it."
        ),
    )
    positions: list[ExtractedPosition] = Field(
        description="Every holding listed for this account, including zero-quantity rows."
    )


class ExtractedPortfolioDocument(_Strict):
    """A complete extraction of one source document."""

    schema_version: str = Field(default=SCHEMA_VERSION)
    document_type: str = Field(
        description=(
            "What kind of document this is, e.g. 'positions_export', 'account_statement', "
            "'holdings_web_page'. Use 'unknown' if unclear."
        )
    )
    producing_institution: str = Field(
        description="The institution that produced this document, not the accounts inside it."
    )
    as_of_date: str | None = Field(
        description=(
            "The date this data represents, as printed. Prefer an explicit 'as of' date "
            "over a download or print timestamp. Null if the document states no date."
        )
    )
    as_of_date_source: str | None = Field(
        description=(
            "Where the date came from, quoted from the document, e.g. "
            '"Date downloaded Aug-26-2026 3:04 a.m ET". Null if no date was found.'
        )
    )
    reported_total_value: str | None = Field(
        description=(
            "A portfolio-wide total if the document states one. Null otherwise. Never compute it."
        )
    )
    accounts: list[ExtractedAccount]
    warnings: list[str] = Field(
        description=(
            "Anything ambiguous, unreadable, or internally inconsistent that a human "
            "should look at. Empty list if nothing. Never use this to apologise -- "
            "state the specific concern."
        )
    )


class AccountDescriptor(_Strict):
    """Stage 1 output: an account identified, before its positions are read."""

    external_account_key: str
    display_name: str
    institution: str
    is_aggregated: bool
    account_type_hint: str | None
    reported_value: str | None
    expected_position_count: int | None = Field(
        description="How many holdings appear under this account, if countable. Null if not."
    )


class DocumentOverview(_Strict):
    """Stage 1 output: the shape of the document, before positions are read.

    Its account set is compared against Stage 2's. A mismatch forces human
    review no matter how cleanly the arithmetic reconciles -- an account that
    silently disappears between stages is the most dangerous failure mode in
    the pipeline, because the surviving numbers all still add up.
    """

    schema_version: str = Field(default=SCHEMA_VERSION)
    document_type: str
    producing_institution: str
    as_of_date: str | None
    as_of_date_source: str | None
    reported_total_value: str | None
    accounts: list[AccountDescriptor]
    warnings: list[str]
