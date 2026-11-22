"""Inferring account type and tax treatment from what the document says.

The guiding rule is that **an honest `unknown` beats a plausible guess**. Tax
treatment drives every tax-placement analysis in the app, so a wrong inference
is worse than an absent one: an absent one prompts the user to classify the
account, while a wrong one silently poisons the analytics.

Measured on the real corpus, 5 of 14 accounts carry no reliable signal at all
(`IIA 6QQ-80000`, `Corporation`, `Employer Equity Plan`, `**********rust`, and a
bare personal name). At that ratio, classification is the normal path rather
than an edge case, which is why every result carries a confidence and why the
UI walks the user through confirming them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..models.enums import AccountType, TaxTreatment


@dataclass(frozen=True)
class Classification:
    account_type: AccountType
    tax_treatment: TaxTreatment
    #: 0.0-1.0. Below `CONFIRMATION_THRESHOLD` the UI asks rather than assumes.
    confidence: Decimal
    #: What in the name drove the decision, so the user can judge it.
    rationale: str

    @property
    def needs_confirmation(self) -> bool:
        return (
            self.confidence < CONFIRMATION_THRESHOLD or self.tax_treatment is TaxTreatment.UNKNOWN
        )


CONFIRMATION_THRESHOLD = Decimal("0.85")

_UNKNOWN = Classification(
    AccountType.UNKNOWN, TaxTreatment.UNKNOWN, Decimal("0"), "no recognisable signal in the name"
)

#: Ordered most-specific first: "roth ira" must beat the bare "ira" rule, and
#: "rollover ira" must not be caught by it either.
_RULES: tuple[tuple[re.Pattern[str], AccountType, TaxTreatment, Decimal, str], ...] = (
    (
        re.compile(r"\broth\b"),
        AccountType.ROTH_IRA,
        TaxTreatment.TAX_FREE,
        Decimal("0.95"),
        "'roth' indicates a Roth IRA, which is tax-free on qualified withdrawal",
    ),
    (
        re.compile(r"\brollover\b"),
        AccountType.ROLLOVER_IRA,
        TaxTreatment.TAX_DEFERRED,
        Decimal("0.95"),
        "'rollover' indicates a rollover IRA, which is tax-deferred",
    ),
    (
        re.compile(r"\btraditional\s+ira\b"),
        AccountType.TRADITIONAL_IRA,
        TaxTreatment.TAX_DEFERRED,
        Decimal("0.95"),
        "'traditional IRA' is tax-deferred",
    ),
    (
        re.compile(r"\b529\b|college\s+savings"),
        AccountType.FIVE_TWENTY_NINE,
        TaxTreatment.EDUCATION,
        Decimal("0.95"),
        "a 529 college savings plan has its own education tax treatment",
    ),
    (
        re.compile(r"\bhsa\b|health\s+savings"),
        AccountType.HSA,
        TaxTreatment.TAX_FREE,
        Decimal("0.9"),
        "an HSA is tax-free when used for qualified expenses",
    ),
    (
        re.compile(r"\bjoint\b|\bwros\b|\btod\b|\btenants\b"),
        AccountType.JOINT,
        TaxTreatment.TAXABLE,
        Decimal("0.9"),
        "a joint / WROS / TOD registration is a taxable account",
    ),
    (
        re.compile(r"\bbrokerage\b|\bindividual\b|\btaxable\b"),
        AccountType.BROKERAGE,
        TaxTreatment.TAXABLE,
        Decimal("0.85"),
        "an individual brokerage account is taxable",
    ),
    # Deliberately last: a bare "ira" tells us the wrapper but NOT whether it is
    # traditional or Roth, so tax treatment stays unknown.
    (
        re.compile(r"\bira\b"),
        AccountType.UNKNOWN,
        TaxTreatment.UNKNOWN,
        Decimal("0.4"),
        "'IRA' alone does not distinguish traditional (tax-deferred) from Roth (tax-free)",
    ),
)

#: Names that identify a *wrapper* but leave the tax regime genuinely open.
#: A corporate account is not "taxable" in the personal sense, and an employer
#: equity plan may hold RSUs, ESPP shares or options under different rules.
_TYPE_ONLY_RULES: tuple[tuple[re.Pattern[str], AccountType, Decimal, str], ...] = (
    (
        re.compile(r"\bcorporation\b|\bcorporate\b|\bllc\b|\binc\b\.?$"),
        AccountType.CORPORATE,
        Decimal("0.8"),
        "a corporate account is subject to entity taxation, not personal treatment",
    ),
    (
        re.compile(r"equity\s+plan|\bespp\b|\brsu\b|stock\s+plan"),
        AccountType.EQUITY_PLAN,
        Decimal("0.8"),
        "an employer equity plan may hold RSUs, ESPP shares or options, "
        "whose tax treatment depends on the grant",
    ),
    (
        re.compile(r"\btrust\b"),
        AccountType.TRUST,
        Decimal("0.7"),
        "a trust's tax treatment depends on whether it is grantor or non-grantor",
    ),
)


def classify_account(display_name: str, type_hint: str | None = None) -> Classification:
    """Infer account type and tax treatment, or honestly decline.

    `type_hint` is whatever the extraction provider offered; it is searched
    alongside the display name because a model often states the type more
    plainly than the raw account label does.
    """
    haystack = f"{display_name or ''} {type_hint or ''}".lower()
    if not haystack.strip():
        return _UNKNOWN

    for pattern, account_type, tax, confidence, rationale in _RULES:
        if pattern.search(haystack):
            return Classification(account_type, tax, confidence, rationale)

    for pattern, account_type, confidence, rationale in _TYPE_ONLY_RULES:
        if pattern.search(haystack):
            return Classification(account_type, TaxTreatment.UNKNOWN, confidence, rationale)

    return _UNKNOWN
