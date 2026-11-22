"""Structural validation of an extraction, before any arithmetic.

Two responsibilities, both of which can *block* an import no matter how well
the numbers add up:

* **The completeness invariant.** Stage 1's account set is compared against
  Stage 2's. This is the pipeline's most important guard, because the failure
  it catches is invisible to every other check: if an account is dropped
  between stages, every remaining number still reconciles perfectly and the
  portfolio is simply, quietly, missing an account.
* **The zero-value skip.** An account whose entire reported value is absent or
  zero is omitted -- but recorded, never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..models.enums import Completeness, SkipReason
from .normalizer import NormalizationError, parse_money

ZERO = Decimal("0")


@dataclass(frozen=True)
class SkippedAccount:
    external_account_key: str
    display_name: str
    reason: str
    position_count: int

    def to_json(self) -> dict[str, object]:
        return {
            "external_account_key": self.external_account_key,
            "display_name": self.display_name,
            "reason": self.reason,
            "position_count": self.position_count,
        }


@dataclass
class ValidationReport:
    completeness: Completeness = Completeness.UNKNOWN
    missing_accounts: list[str] = field(default_factory=list)
    unexpected_accounts: list[str] = field(default_factory=list)
    position_count_mismatches: list[str] = field(default_factory=list)
    skipped_accounts: list[SkippedAccount] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.completeness is Completeness.COMPLETE

    @property
    def requires_review(self) -> bool:
        """Whether a human must look, regardless of the arithmetic.

        Deliberately independent of reconciliation: an incomplete extraction
        can reconcile perfectly and still be wrong.
        """
        return bool(self.errors) or not self.is_complete

    def summary(self) -> dict[str, object]:
        return {
            "completeness": self.completeness.value,
            "missing_accounts": list(self.missing_accounts),
            "unexpected_accounts": list(self.unexpected_accounts),
            "position_count_mismatches": list(self.position_count_mismatches),
            "skipped_accounts": [s.to_json() for s in self.skipped_accounts],
            "errors": list(self.errors),
        }


def check_completeness(
    stage1_keys: list[str],
    stage2_keys: list[str],
    expected_counts: dict[str, int | None] | None = None,
    actual_counts: dict[str, int] | None = None,
) -> ValidationReport:
    """Compare what Stage 1 found against what Stage 2 returned.

    A mismatch in either direction forces review. A count mismatch is recorded
    as a warning rather than incompleteness, because Stage 1's count is a
    best-effort observation while its account *set* is authoritative.
    """
    report = ValidationReport()
    first = set(stage1_keys)
    second = set(stage2_keys)

    report.missing_accounts = sorted(first - second)
    report.unexpected_accounts = sorted(second - first)

    if expected_counts and actual_counts:
        for key, expected in expected_counts.items():
            if expected is None or key not in actual_counts:
                continue
            actual = actual_counts[key]
            if expected != actual:
                report.position_count_mismatches.append(
                    f"{key}: stage 1 saw {expected} holdings, stage 2 returned {actual}"
                )

    if report.missing_accounts or report.unexpected_accounts:
        report.completeness = Completeness.PARTIAL
    else:
        report.completeness = Completeness.COMPLETE

    if report.missing_accounts:
        report.errors.append(
            "accounts identified during document analysis were absent from the "
            f"holdings pass: {', '.join(report.missing_accounts)}"
        )
    if report.unexpected_accounts:
        report.errors.append(
            "the holdings pass returned accounts the document analysis did not "
            f"identify: {', '.join(report.unexpected_accounts)}"
        )
    return report


def account_reported_value(position_values: list[Decimal | None]) -> Decimal:
    """Sum the reported values of an account's holdings, treating absent as zero."""
    return sum((v for v in position_values if v is not None), ZERO)


def should_skip_account(position_values: list[Decimal | None]) -> bool:
    """Whether an account carries no reported value at all.

    Evaluated per import from what the source reports, so an account returns
    automatically the moment a later document gives it a value. Note this is an
    *account-level* judgement: a valued account keeps its individual
    zero-quantity holdings as closed positions.
    """
    return account_reported_value(position_values) == ZERO


def partition_accounts(
    accounts: list[tuple[str, str, list[str | None]]],
) -> tuple[list[str], list[SkippedAccount]]:
    """Split accounts into those to import and those to skip.

    Each input is (external_account_key, display_name, raw market-value strings).
    Returns the keys to keep, and a record of every skip with its reason.
    """
    keep: list[str] = []
    skipped: list[SkippedAccount] = []
    for key, name, raw_values in accounts:
        values: list[Decimal | None] = []
        for raw in raw_values:
            try:
                values.append(parse_money(raw, field="market_value"))
            except NormalizationError:
                # An unparseable value is not an absent one: keep the account
                # so the problem surfaces in review rather than vanishing.
                values.append(None)
        if should_skip_account(values):
            skipped.append(
                SkippedAccount(
                    external_account_key=key,
                    display_name=name,
                    reason=SkipReason.ZERO_REPORTED_VALUE.value,
                    position_count=len(raw_values),
                )
            )
        else:
            keep.append(key)
    return keep, skipped
