"""Storing and versioning the investment policy.

The policy round-trips through YAML so it can be edited, diffed and reviewed
outside the app. Both representations are kept: `document_json` is what the
system reads, `raw_yaml` is what the user wrote, including their comments and
ordering. Regenerating YAML from JSON would silently discard both.

Saving never overwrites. Each save inserts a new version and deactivates the
previous one, so "what was my policy when I made that decision?" stays
answerable -- which matters because the recommendation journal will cite it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.advisory import InvestmentPolicy
from ..schemas.policy import PolicyDocument


class PolicyError(ValueError):
    """A policy could not be read or was internally inconsistent."""


@dataclass(frozen=True)
class StoredPolicy:
    record: InvestmentPolicy
    document: PolicyDocument

    @property
    def version(self) -> int:
        return self.record.version


def _decimalize(value: Any) -> Any:
    """Parse YAML numbers as Decimal.

    PyYAML produces floats, and a float target of 0.1 is not 0.1. Since these
    weights feed straight into drift arithmetic, they are converted at the
    boundary rather than trusted.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def parse_yaml(text: str) -> PolicyDocument:
    """Parse and validate a policy from YAML."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"the policy is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("the policy must be a mapping at the top level")
    try:
        return PolicyDocument.model_validate(_decimalize(raw))
    except Exception as exc:
        raise PolicyError(f"the policy is not valid: {exc}") from exc


def to_yaml(document: PolicyDocument) -> str:
    """Render a policy as YAML, with Decimals as plain numbers."""
    payload = document.model_dump(mode="json", exclude_defaults=False)
    return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)


def active_policy(session: Session) -> StoredPolicy | None:
    record = (
        session.execute(
            select(InvestmentPolicy)
            .where(InvestmentPolicy.is_active.is_(True))
            .order_by(InvestmentPolicy.version.desc())
        )
        .scalars()
        .first()
    )
    if record is None:
        return None
    return StoredPolicy(record=record, document=PolicyDocument.model_validate(record.document_json))


def policy_history(session: Session, name: str = "default") -> list[InvestmentPolicy]:
    """Every version, newest first. Nothing is ever deleted."""
    return list(
        session.execute(
            select(InvestmentPolicy)
            .where(InvestmentPolicy.name == name)
            .order_by(InvestmentPolicy.version.desc())
        ).scalars()
    )


def save_policy(
    session: Session, document: PolicyDocument, *, raw_yaml: str | None = None
) -> StoredPolicy:
    """Insert a new version and retire the previous one."""
    previous = (
        session.execute(
            select(InvestmentPolicy)
            .where(InvestmentPolicy.name == document.name)
            .order_by(InvestmentPolicy.version.desc())
        )
        .scalars()
        .all()
    )

    next_version = (previous[0].version + 1) if previous else 1
    for record in previous:
        record.is_active = False

    stored = document.model_copy(update={"version": next_version})
    record = InvestmentPolicy(
        name=stored.name,
        version=next_version,
        is_active=True,
        document_json=stored.model_dump(mode="json"),
        raw_yaml=raw_yaml if raw_yaml is not None else to_yaml(stored),
    )
    session.add(record)
    session.flush()
    return StoredPolicy(record=record, document=stored)


def save_policy_yaml(session: Session, text: str) -> StoredPolicy:
    """Validate YAML and store it, keeping the user's own text verbatim."""
    return save_policy(session, parse_yaml(text), raw_yaml=text)


#: A starting point that states its own assumptions rather than pretending to
#: be a recommendation. Offered when no policy exists yet.
STARTER_POLICY_YAML = """\
# Investment policy statement.
#
# This is a starting point, not advice. Edit the targets to match your own
# plan -- they must sum to 1.0, and the system will refuse a policy that does
# not, because a policy that does not add up would distort every drift figure
# computed from it.

name: default
notes: >-
  Replace this with your own reasoning: why these targets, what would make you
  change them, and what you have decided NOT to do.

# Target weights by asset class. Must sum to 1.0.
targets:
  us_equity: 0.50
  intl_equity: 0.20
  fixed_income: 0.25
  cash_equivalent: 0.05

# How far a class may drift from its target before it is out of band.
# 0.05 means a 50% target is in band between 45% and 55%.
drift_band: 0.05
drift_band_overrides:
  cash_equivalent: 0.03

constraints:
  - id: max-single-holding
    kind: max_single_holding
    limit: 0.10
    description: No single security above 10% of the portfolio.
  - id: min-cash
    kind: min_cash
    limit: 0.02
    description: Keep at least 2% in cash equivalents.

# Which asset classes belong in which kind of account. Advisory only: the
# system reports where holdings actually sit and never moves anything.
tax_placement:
  prefer_tax_deferred:
    - fixed_income
    - real_estate
  prefer_taxable:
    - intl_equity

rebalancing:
  method: threshold
  min_trade_value: 1000
"""
