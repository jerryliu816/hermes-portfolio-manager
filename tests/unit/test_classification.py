"""Account classification.

The negative cases matter more than the positive ones. Tax treatment drives
every tax-placement analysis, so a wrong inference silently poisons the
analytics, while an absent one merely prompts the user to classify the account.
"""

from __future__ import annotations

import pytest

from portfolio_manager.models.enums import AccountType, TaxTreatment
from portfolio_manager.services.classification import classify_account


@pytest.mark.parametrize(
    ("name", "account_type", "tax"),
    [
        ("ROTH IRA", AccountType.ROTH_IRA, TaxTreatment.TAX_FREE),
        ("Rollover IRA", AccountType.ROLLOVER_IRA, TaxTreatment.TAX_DEFERRED),
        ("Traditional IRA", AccountType.TRADITIONAL_IRA, TaxTreatment.TAX_DEFERRED),
        ("Joint WROS - TOD", AccountType.JOINT, TaxTreatment.TAXABLE),
        (
            "Sam Sample-Individual 529 College Savings Account",
            AccountType.FIVE_TWENTY_NINE,
            TaxTreatment.EDUCATION,
        ),
        (
            "Pat Q Sample - Brokerage Account - 78000044",
            AccountType.BROKERAGE,
            TaxTreatment.TAXABLE,
        ),
    ],
)
def test_recognisable_names_classify_confidently(
    name: str, account_type: AccountType, tax: TaxTreatment
) -> None:
    result = classify_account(name)
    assert result.account_type is account_type
    assert result.tax_treatment is tax
    assert not result.needs_confirmation


@pytest.mark.parametrize(
    "name",
    [
        "IIA 6QQ-80000",  # opaque identifier
        "**********rust",  # masked by the source
        "Pat Q Sample, Robin Sample",  # a bare personal name
        "",
    ],
)
def test_opaque_names_stay_unknown(name: str) -> None:
    """5 of 14 real accounts carry no reliable signal. Guessing is worse."""
    result = classify_account(name)
    assert result.account_type is AccountType.UNKNOWN
    assert result.tax_treatment is TaxTreatment.UNKNOWN
    assert result.needs_confirmation


def test_a_bare_ira_does_not_guess_between_roth_and_traditional() -> None:
    """The wrapper is knowable; the tax treatment genuinely is not."""
    result = classify_account("My IRA")
    assert result.tax_treatment is TaxTreatment.UNKNOWN
    assert result.needs_confirmation
    assert "does not distinguish" in result.rationale


def test_roth_beats_the_generic_ira_rule() -> None:
    """Rule ordering: the specific pattern must win."""
    assert classify_account("ROTH IRA").tax_treatment is TaxTreatment.TAX_FREE
    assert classify_account("Rollover IRA").tax_treatment is TaxTreatment.TAX_DEFERRED


def test_corporate_accounts_get_a_type_but_not_a_personal_tax_treatment() -> None:
    """A corporate account is not 'taxable' in the personal sense."""
    result = classify_account("Corporation")
    assert result.account_type is AccountType.CORPORATE
    assert result.tax_treatment is TaxTreatment.UNKNOWN
    assert result.needs_confirmation


def test_equity_plans_get_a_type_but_not_a_tax_treatment() -> None:
    """RSUs, ESPP shares and options are taxed differently from one another."""
    result = classify_account("Employer Equity Plan")
    assert result.account_type is AccountType.EQUITY_PLAN
    assert result.tax_treatment is TaxTreatment.UNKNOWN


def test_a_provider_hint_supplements_the_display_name() -> None:
    """The model often states the type more plainly than the raw label."""
    result = classify_account("IIA 6QQ-80000", "Roth IRA")
    assert result.tax_treatment is TaxTreatment.TAX_FREE


def test_every_classification_explains_itself() -> None:
    for name in ("ROTH IRA", "Corporation", "IIA 6QQ-80000"):
        assert classify_account(name).rationale
