"""The extraction wire schema.

The money-as-string rule is the load-bearing decision here, so it is asserted
directly rather than assumed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from portfolio_manager.extraction.canonical import (
    SCHEMA_VERSION,
    DocumentOverview,
    ExtractedAccount,
    ExtractedPortfolioDocument,
    ExtractedPosition,
)


def _position(**kw: object) -> ExtractedPosition:
    base: dict[str, object] = {
        "symbol": "AAA",
        "description": "Alpha Index ETF",
        "quantity": "100.500",
        "price": "$200.00",
        "market_value": "$20100.00",
        "cost_basis": None,
        "unrealized_gain_loss": None,
    }
    base.update(kw)
    return ExtractedPosition(**base)  # type: ignore[arg-type]


def test_all_money_fields_are_strings_in_the_json_schema() -> None:
    """No monetary or quantity field may be typed `number` on the wire.

    A float cannot represent money, and a Pydantic Decimal would emit an
    `anyOf` that strict structured-output generation does not accept.
    """
    schema = ExtractedPosition.model_json_schema()
    money_fields = {
        "quantity",
        "price",
        "market_value",
        "cost_basis",
        "unrealized_gain_loss",
    }
    for field in money_fields:
        spec = schema["properties"][field]
        types = (
            {spec["type"]} if "type" in spec else {opt.get("type") for opt in spec.get("anyOf", [])}
        )
        assert "number" not in types, f"{field} must not be numeric on the wire, got {types}"
        assert "string" in types, f"{field} must be a string on the wire, got {types}"


def test_money_strings_are_preserved_verbatim() -> None:
    """The value the model emitted survives transport byte-for-byte."""
    pos = _position(market_value="$118,936.42", unrealized_gain_loss="+$44725.87")
    assert pos.market_value == "$118,936.42"
    assert pos.unrealized_gain_loss == "+$44725.87"


def test_symbol_may_be_null() -> None:
    """529 and managed-portfolio holdings genuinely have no ticker."""
    pos = _position(symbol=None, description="Sample 500 Index Portfolio")
    assert pos.symbol is None


def test_absent_values_are_null_not_zero() -> None:
    """A missing cost basis is distinguishable from a zero cost basis."""
    absent = _position(cost_basis=None)
    zero = _position(cost_basis="$0.00")
    assert absent.cost_basis is None
    assert zero.cost_basis == "$0.00"
    assert absent.cost_basis != zero.cost_basis


def test_unknown_fields_are_rejected() -> None:
    """A provider inventing a field is a bug, not something to absorb."""
    with pytest.raises(ValidationError):
        ExtractedPosition(  # type: ignore[call-arg]
            symbol="AAA",
            description="Alpha",
            quantity=None,
            price=None,
            market_value=None,
            cost_basis=None,
            unrealized_gain_loss=None,
            hallucinated_field="oops",
        )


def test_account_carries_its_own_institution_and_aggregation_flag() -> None:
    """Institution is per account: one export can span several institutions."""
    acct = ExtractedAccount(
        external_account_key="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        display_name="Sample - Brokerage Account - 90000001",
        institution="Vanguard",
        is_aggregated=True,
        account_type_hint=None,
        reported_value=None,
        positions=[_position()],
    )
    assert acct.institution == "Vanguard"
    assert acct.is_aggregated is True


def test_document_defaults_to_current_schema_version() -> None:
    doc = ExtractedPortfolioDocument(
        document_type="positions_export",
        producing_institution="Fidelity",
        as_of_date=None,
        as_of_date_source=None,
        reported_total_value=None,
        accounts=[],
        warnings=[],
    )
    assert doc.schema_version == SCHEMA_VERSION


def test_overview_and_document_agree_on_schema_version() -> None:
    """Both stages must be interpretable under the same version."""
    overview = DocumentOverview(
        document_type="positions_export",
        producing_institution="Fidelity",
        as_of_date=None,
        as_of_date_source=None,
        reported_total_value=None,
        accounts=[],
        warnings=[],
    )
    assert overview.schema_version == SCHEMA_VERSION
