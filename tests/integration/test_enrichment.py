"""Phase 5: refreshing a snapshot from an optional secondary document.

The structural guarantee is that enrichment cannot change **what exists** --
only the measurements of what already exists. Portfolio *value* legitimately
changes, because refreshing a stale price is the entire point; what must not
change is the set of accounts and holdings.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.extraction.canonical import (
    ExtractedAccount,
    ExtractedPortfolioDocument,
    ExtractedPosition,
)
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.models.entities import (
    Account,
    AccountMapping,
    PositionSnapshot,
    Security,
)
from portfolio_manager.models.enums import CostBasisSource, ValueSource
from portfolio_manager.services.enrichment import enrich_snapshot
from portfolio_manager.services.importer import import_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)

#: The aggregated account in the fixture, as the enrichment source names it.
AGGREGATED_KEY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def imported(session: Session, settings: Settings) -> object:
    return import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)


def _position(**kw: object) -> ExtractedPosition:
    base: dict[str, object] = {
        "symbol": "AAA",
        "description": "Alpha Index ETF",
        "quantity": "10.000",
        "price": "$2200.00",
        "market_value": "$22000.00",
        "cost_basis": "$12000.00",
        "unrealized_gain_loss": "$10000.00",
    }
    base.update(kw)
    return ExtractedPosition(**base)  # type: ignore[arg-type]


def _enrichment(
    positions: list[ExtractedPosition] | None = None,
    *,
    key: str = "90000001",
    reported_value: str | None = None,
) -> ExtractedPortfolioDocument:
    return ExtractedPortfolioDocument(
        document_type="holdings_web_page",
        producing_institution="SampleVest",
        as_of_date="08/26/2026",
        as_of_date_source="Value as of 08/26/2026",
        reported_total_value=None,
        accounts=[
            ExtractedAccount(
                external_account_key=key,
                display_name="Pat Q Sample, Alex Sample - Brokerage Account",
                institution="SampleVest",
                is_aggregated=False,
                account_type_hint="Brokerage",
                reported_value=reported_value,
                positions=positions if positions is not None else [_position()],
            )
        ],
        warnings=[],
    )


def _snapshot_shape(session: Session) -> tuple[int, set[tuple[str, str]]]:
    rows = session.query(PositionSnapshot).all()
    identity = {(str(p.account_id), str(p.security_id)) for p in rows}
    return len(rows), identity


def test_enrichment_cannot_change_what_exists(
    session: Session, settings: Settings, imported: object
) -> None:
    """THE structural guarantee: same accounts, same holdings, always."""
    before_count, before_identity = _snapshot_shape(session)
    before_accounts = session.query(Account).count()

    enrich_snapshot(session, imported.snapshot.snapshot, _enrichment(), settings=settings)

    after_count, after_identity = _snapshot_shape(session)
    assert after_count == before_count
    assert after_identity == before_identity
    assert session.query(Account).count() == before_accounts


def test_a_stale_price_is_refreshed_and_marked(
    session: Session, settings: Settings, imported: object
) -> None:
    """Refreshing values is the point; provenance records that it happened."""
    security = session.query(Security).filter_by(symbol="AAA").one()
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    position = (
        session.query(PositionSnapshot)
        .filter_by(account_id=aggregated.id, security_id=security.id)
        .one()
    )
    assert position.price == Decimal("2000.000000")
    assert position.value_source == ValueSource.PRIMARY.value

    result = enrich_snapshot(session, imported.snapshot.snapshot, _enrichment(), settings=settings)
    session.refresh(position)

    assert result.refreshed_positions == 1
    assert position.price == Decimal("2200.000000")
    assert position.market_value == Decimal("22000.000000")
    assert position.value_source == ValueSource.ENRICHED.value


def test_the_measurement_tuple_is_replaced_as_a_unit(
    session: Session, settings: Settings, imported: object
) -> None:
    """A partial overwrite would leave quantity x price != value.

    Two sources can disagree on share basis -- one pre-split, one post -- and
    each is internally consistent only as a complete set.
    """
    enrichment = _enrichment(
        [_position(quantity="1.000", price="$22000.00", market_value="$22000.00")]
    )
    enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)

    security = session.query(Security).filter_by(symbol="AAA").one()
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    position = (
        session.query(PositionSnapshot)
        .filter_by(account_id=aggregated.id, security_id=security.id)
        .one()
    )
    assert position.quantity == Decimal("1.000000")
    assert position.price == Decimal("22000.000000")
    assert position.quantity * position.price == position.market_value


def test_enrichment_that_does_not_reconcile_is_rejected_wholesale(
    session: Session, settings: Settings, imported: object
) -> None:
    """Bad enrichment leaves the holding untouched rather than half-updated."""
    security = session.query(Security).filter_by(symbol="AAA").one()
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    position = (
        session.query(PositionSnapshot)
        .filter_by(account_id=aggregated.id, security_id=security.id)
        .one()
    )
    original_price = position.price

    bad = _enrichment([_position(quantity="10.000", price="$100.00", market_value="$99999.00")])
    result = enrich_snapshot(session, imported.snapshot.snapshot, bad, settings=settings)
    session.refresh(position)

    assert result.refreshed_positions == 0
    assert position.price == original_price
    assert position.value_source == ValueSource.PRIMARY.value
    assert any("did not reconcile" in w for w in result.warnings)


def test_a_holding_only_the_enrichment_knows_about_is_ignored_but_counted(
    session: Session, settings: Settings, imported: object
) -> None:
    """The authoritative source decides what exists."""
    before_count, _ = _snapshot_shape(session)
    enrichment = _enrichment(
        [
            _position(),
            _position(symbol="ZZZ", description="Zeta Fund", market_value="$5000.00"),
        ]
    )
    result = enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)
    assert result.ignored_enrichment_positions == 1
    assert _snapshot_shape(session)[0] == before_count
    assert "not covered by the authoritative source" in result.summary()


def test_holdings_the_enrichment_omits_keep_their_primary_values(
    session: Session, settings: Settings, imported: object
) -> None:
    result = enrich_snapshot(
        session, imported.snapshot.snapshot, _enrichment([]), settings=settings
    )
    assert result.refreshed_positions == 0
    assert result.not_refreshed_positions == 1
    remaining = (
        session.query(PositionSnapshot).filter_by(value_source=ValueSource.PRIMARY.value).count()
    )
    assert remaining == session.query(PositionSnapshot).count()


def test_settlement_cash_is_carried_not_discarded(
    session: Session, settings: Settings, imported: object
) -> None:
    """The real Vanguard gap equals the money-market balance to the cent."""
    enrichment = _enrichment(reported_value="$30000.00")
    result = enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)
    assert result.total_cash_carried == Decimal("8000.00")
    assert "cash balances carried" in result.summary()


def test_an_account_the_portfolio_does_not_cover_is_reported(
    session: Session, settings: Settings, imported: object
) -> None:
    """Never silently dropped -- that would hide real money."""
    snapshot = imported.snapshot.snapshot
    result = enrich_snapshot(session, snapshot, _enrichment(key="55555555"), settings=settings)
    assert [r.key for r in result.matching.unmatched_enrichment] == ["55555555"]
    session.refresh(snapshot)
    assert snapshot.unmatched_enrichment_json
    assert "NOT included" in snapshot.unmatched_enrichment_json[0]["note"]


def test_a_confirmed_mapping_resolves_what_matching_refuses_to_guess(
    session: Session, settings: Settings, imported: object
) -> None:
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    session.add(
        AccountMapping(
            account_id=aggregated.id,
            enrichment_institution="SampleVest",
            enrichment_key="55555555",
        )
    )
    session.flush()

    result = enrich_snapshot(
        session, imported.snapshot.snapshot, _enrichment(key="55555555"), settings=settings
    )
    assert result.refreshed_positions == 1
    assert not result.matching.unmatched_enrichment


def test_enrichment_fills_in_a_missing_cost_basis(
    session: Session, settings: Settings, imported: object
) -> None:
    """Aggregated rows arrive with no basis; enrichment can supply one."""
    security = session.query(Security).filter_by(symbol="AAA").one()
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    position = (
        session.query(PositionSnapshot)
        .filter_by(account_id=aggregated.id, security_id=security.id)
        .one()
    )
    position.cost_basis = None
    position.cost_basis_source = CostBasisSource.UNKNOWN.value
    session.flush()

    enrich_snapshot(session, imported.snapshot.snapshot, _enrichment(), settings=settings)
    session.refresh(position)
    assert position.cost_basis == Decimal("12000.000000")
    assert position.cost_basis_source == CostBasisSource.REPORTED.value


def test_an_account_the_sources_disagree_about_is_left_entirely_alone(
    session: Session, settings: Settings, imported: object
) -> None:
    """The failure the real documents exhibit.

    Fidelity lists 22 holdings in one Rollover IRA where Vanguard lists 12.
    Refreshing only the overlap leaves the rest stale, producing a portfolio of
    mixed vintage whose total is as-of no particular moment. Measured on the
    real pair, doing so moved the portfolio by $420,854 -- far more than price
    staleness alone can justify. A partially refreshed account is worse than an
    unrefreshed one, because it looks current.
    """
    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    original = {
        p.id: p.market_value
        for p in session.query(PositionSnapshot).filter_by(account_id=aggregated.id).all()
    }

    # The enrichment refreshes the one holding it knows, but claims an account
    # total far above what that implies -- so it must be describing more.
    enrichment = _enrichment(reported_value="$500000.00")
    result = enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)

    assert result.refreshed_positions == 0
    assert aggregated.display_name in result.rejected_accounts
    assert any(aggregated.display_name in w for w in result.warnings)
    assert any("left on its original values" in w for w in result.warnings)
    for position in session.query(PositionSnapshot).filter_by(account_id=aggregated.id):
        assert position.market_value == original[position.id]
        assert position.value_source == ValueSource.PRIMARY.value


def test_an_account_where_nothing_matches_is_reported_not_silent(
    session: Session, settings: Settings, imported: object
) -> None:
    """Symbol-less holdings match on description, which differs between sources.

    Both real 529 accounts hit this: neither source gives their holdings a
    ticker, and their descriptions do not agree.
    """
    enrichment = _enrichment([_position(symbol="QQQ", description="Totally Different Fund")])
    result = enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)
    assert result.refreshed_positions == 0
    assert any("could be matched" in w for w in result.warnings)
    assert any("manual review" in w for w in result.warnings)


def test_cash_residual_comes_from_the_enrichment_document_alone(
    session: Session, settings: Settings, imported: object
) -> None:
    """Otherwise an unmatched holding is misrecorded as cash.

    Computing the residual against the matched subset rather than against the
    enrichment document's own holdings would have reported an entire account's
    value as a settlement balance.
    """
    enrichment = _enrichment(
        [
            _position(),
            _position(symbol="ZZZ", description="Zeta Fund", market_value="$5000.00"),
        ],
        reported_value="$29000.00",
    )
    result = enrich_snapshot(session, imported.snapshot.snapshot, enrichment, settings=settings)
    # The enrichment doc states 29000 and lists 22000 + 5000 = 27000 of
    # holdings, so its own internal gap -- the settlement balance -- is 2000.
    # Computing it against our matched subset instead would have reported 7000,
    # misrecording the 5000 holding we do not carry as cash.
    assert list(result.cash_residuals.values()) == [Decimal("2000.00")]


def test_a_fully_covered_account_enriches_cleanly(
    session: Session, settings: Settings, imported: object
) -> None:
    """The happy path: both sources agree on composition, so values refresh."""
    result = enrich_snapshot(session, imported.snapshot.snapshot, _enrichment(), settings=settings)
    assert result.refreshed_positions == 1
    assert not result.rejected_accounts


def test_precedence_replaces_an_account_wholesale(
    session: Session, settings: Settings, imported: object
) -> None:
    """A winning document supplies what the account CONTAINS, not just values.

    This is what separates precedence from enrichment: the aggregation and the
    direct source can disagree about which holdings exist at all.
    """
    from portfolio_manager.services.multi_source import add_document_to_snapshot

    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    assert aggregated.is_aggregated

    direct = ExtractedPortfolioDocument(
        document_type="account_statement",
        producing_institution="SampleVest",
        as_of_date="08/26/2026",
        as_of_date_source="Value as of 08/26/2026",
        reported_total_value=None,
        accounts=[
            ExtractedAccount(
                external_account_key="90000001",
                display_name="Pat Q Sample, Alex Sample - Brokerage Account",
                institution="SampleVest",
                is_aggregated=False,
                account_type_hint="Brokerage",
                reported_value="$61000.00",
                cash_balance="$1000.00",
                positions=[
                    _position(
                        symbol="NEW",
                        description="Newly Bought Fund",
                        quantity="100",
                        price="$400.00",
                        market_value="$40000.00",
                        cost_basis=None,
                        unrealized_gain_loss=None,
                    ),
                    _position(
                        symbol="AAA",
                        description="Alpha Index ETF",
                        quantity="10",
                        price="$2000.00",
                        market_value="$20000.00",
                        cost_basis=None,
                        unrealized_gain_loss=None,
                    ),
                ],
            )
        ],
        warnings=[],
    )

    result = add_document_to_snapshot(
        session, imported.snapshot.snapshot, direct, settings=settings
    )

    assert result.precedence.replaced_accounts == 1
    decision = result.precedence.decisions[0]
    assert decision.winner == "SampleVest"
    assert "directly" in decision.reason

    # The account now holds what the direct source says, including a holding
    # the aggregation never knew about.
    symbols = {
        session.get(Security, p.security_id).symbol  # type: ignore[union-attr]
        for p in session.query(PositionSnapshot).filter_by(account_id=aggregated.id)
    }
    assert symbols == {"NEW", "AAA"}

    # The choice is remembered so it is not re-derived every import.
    session.refresh(aggregated)
    assert aggregated.preferred_source == "SampleVest"


def test_separately_reported_cash_counts_toward_the_account(
    session: Session, settings: Settings, imported: object
) -> None:
    """Settlement cash sits outside the holdings table but is real money."""
    from portfolio_manager.models.entities import AccountSnapshot
    from portfolio_manager.services.multi_source import add_document_to_snapshot

    aggregated = session.query(Account).filter_by(external_account_key=AGGREGATED_KEY).one()
    direct = ExtractedPortfolioDocument(
        document_type="account_statement",
        producing_institution="SampleVest",
        as_of_date="08/26/2026",
        as_of_date_source="x",
        reported_total_value=None,
        accounts=[
            ExtractedAccount(
                external_account_key="90000001",
                display_name="Pat Q Sample, Alex Sample - Brokerage Account",
                institution="SampleVest",
                is_aggregated=False,
                account_type_hint=None,
                reported_value="$25000.00",
                cash_balance="$5000.00",
                positions=[_position(market_value="$20000.00")],
            )
        ],
        warnings=[],
    )
    add_document_to_snapshot(session, imported.snapshot.snapshot, direct, settings=settings)

    account_snapshot = session.query(AccountSnapshot).filter_by(account_id=aggregated.id).one()
    assert account_snapshot.computed_value == Decimal("20000.00")
    assert account_snapshot.cash_balance == Decimal("5000.00")
    assert account_snapshot.total_value == Decimal("25000.00")


def test_aggregated_accounts_are_marked_stale_on_import(
    session: Session, settings: Settings, imported: object
) -> None:
    """Their as-of date is unknown; the file's own date says nothing about them."""
    from portfolio_manager.models.entities import AccountSnapshot

    rows = session.query(AccountSnapshot).all()
    by_account = {session.get(Account, r.account_id): r for r in rows}
    for account, row in by_account.items():
        assert account is not None
        if account.is_aggregated:
            assert row.is_stale, f"{account.display_name} should be flagged stale"
            assert row.as_of_confidence == "unknown"
        else:
            assert not row.is_stale
            assert row.as_of_confidence == "stated"
