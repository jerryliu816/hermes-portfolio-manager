"""Structural validation: completeness and the zero-value skip.

The completeness invariant is the pipeline's most important guard because the
failure it catches is invisible to every other check -- drop an account between
stages and all the remaining arithmetic still reconciles perfectly.
"""

from __future__ import annotations

from decimal import Decimal

from portfolio_manager.ingestion.validator import (
    account_reported_value,
    check_completeness,
    partition_accounts,
    should_skip_account,
)
from portfolio_manager.models.enums import Completeness


def test_matching_account_sets_are_complete() -> None:
    report = check_completeness(["A", "B", "C"], ["C", "B", "A"])
    assert report.completeness is Completeness.COMPLETE
    assert report.is_complete
    assert not report.requires_review


def test_a_dropped_account_forces_review() -> None:
    """THE critical case: stage 2 loses an account, arithmetic still adds up."""
    report = check_completeness(["A", "B", "C"], ["A", "B"])
    assert report.completeness is Completeness.PARTIAL
    assert report.missing_accounts == ["C"]
    assert report.requires_review
    assert any("absent from the holdings pass" in e for e in report.errors)


def test_an_invented_account_forces_review() -> None:
    report = check_completeness(["A"], ["A", "B"])
    assert report.completeness is Completeness.PARTIAL
    assert report.unexpected_accounts == ["B"]
    assert report.requires_review


def test_review_is_required_independently_of_arithmetic() -> None:
    """Completeness is not something clean numbers can buy your way out of."""
    report = check_completeness(["A", "B"], ["A"])
    assert report.requires_review is True


def test_position_count_mismatch_is_a_warning_not_incompleteness() -> None:
    """Stage 1's count is best-effort; its account set is authoritative."""
    report = check_completeness(["A"], ["A"], expected_counts={"A": 22}, actual_counts={"A": 12})
    assert report.completeness is Completeness.COMPLETE
    assert report.position_count_mismatches
    assert "stage 1 saw 22" in report.position_count_mismatches[0]


def test_unknown_expected_count_is_not_a_mismatch() -> None:
    report = check_completeness(["A"], ["A"], expected_counts={"A": None}, actual_counts={"A": 5})
    assert not report.position_count_mismatches


def test_account_value_treats_absent_as_zero_for_summing() -> None:
    assert account_reported_value([Decimal("10"), None, Decimal("5")]) == Decimal("15")
    assert account_reported_value([None, None]) == Decimal("0")


def test_accounts_with_no_reported_value_are_skipped() -> None:
    """Four real accounts hold positions but report no value at all."""
    assert should_skip_account([None, None, None])
    assert should_skip_account([Decimal("0")])
    assert not should_skip_account([Decimal("0"), Decimal("100")])


def test_partition_records_every_skip_with_a_reason() -> None:
    """A skip is never silent -- it lands on the import summary."""
    keep, skipped = partition_accounts(
        [
            ("Z1", "Joint WROS - TOD", ["$20100.00", "--"]),
            ("U1", "IIA 6QQ-80000", ["--", "--", "--"]),
            ("U2", "Employer Equity Plan", ["$0.00"]),
        ]
    )
    assert keep == ["Z1"]
    assert [s.external_account_key for s in skipped] == ["U1", "U2"]
    assert skipped[0].reason == "zero_reported_value"
    assert skipped[0].position_count == 3
    assert skipped[0].display_name == "IIA 6QQ-80000"


def test_a_valued_account_keeps_its_zero_quantity_holdings() -> None:
    """The skip is account-level: a real 529 has value AND closed rows."""
    keep, skipped = partition_accounts(
        [("F1", "Sam Sample 529", ["$48207.66", "$18935.41", "--", "--", "--"])]
    )
    assert keep == ["F1"]
    assert skipped == []


def test_an_unparseable_value_does_not_cause_a_silent_skip() -> None:
    """Garbage must surface in review, not make an account disappear."""
    keep, skipped = partition_accounts([("Z1", "Odd", ["twelve dollars", "$5.00"])])
    assert keep == ["Z1"]
    assert skipped == []


def test_skip_records_serialise_for_the_snapshot() -> None:
    _, skipped = partition_accounts([("U1", "Opaque", ["--"])])
    payload = skipped[0].to_json()
    assert payload["reason"] == "zero_reported_value"
    assert payload["external_account_key"] == "U1"
