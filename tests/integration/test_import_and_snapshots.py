"""Phase 4: importing a document into authoritative state.

Covers the properties that make the system of record trustworthy: identical
re-imports create nothing, re-extraction never overwrites history, and an
incomplete extraction cannot become approved state no matter how well the
numbers add up.
"""

from __future__ import annotations

import datetime as dt
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.config import Settings
from portfolio_manager.extraction.contracts import ExtractionContext, SourceDocument
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider
from portfolio_manager.models.entities import (
    Account,
    PortfolioSnapshot,
    PositionSnapshot,
    Security,
    SourceFile,
)
from portfolio_manager.models.enums import (
    CostBasisSource,
    ImportStatus,
    ValueSource,
)
from portfolio_manager.registry_types import ImportOutcome
from portfolio_manager.services.extraction_run import (
    active_run,
    estimate_cost,
    record_extraction_run,
    run_history,
)
from portfolio_manager.services.importer import import_document
from portfolio_manager.services.snapshot import (
    create_snapshot,
    supersede_overlapping_snapshot,
)
from portfolio_manager.services.source_file import load_stored_bytes, store_source_document

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_synthetic.csv"
)

#: The aggregation whose account identities match the direct-source fixture, so
#: the two can be paired the way the real documents are.
PAIRED_AGGREGATION = (
    Path(__file__).resolve().parents[1] / "fixtures/extraction/fidelity/fullview_anonymized.csv"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a temporary data directory, never the real one."""
    return Settings(data_dir=tmp_path / "data", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def provider() -> MockExtractionProvider:
    return MockExtractionProvider()


def _import(
    session: Session, settings: Settings, provider: MockExtractionProvider, path: Path = FIXTURE
) -> object:
    return import_document(session, path, provider, settings=settings)


def test_import_creates_accounts_positions_and_a_snapshot(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    result = _import(session, settings, provider)
    assert result.outcome is ImportOutcome.IMPORTED
    assert result.created_state
    snapshot = session.query(PortfolioSnapshot).one()
    assert snapshot.as_of_date == dt.date(2026, 8, 26)
    # The zero-value account is skipped, so 4 of the fixture's 5 remain.
    assert session.query(Account).count() == 4
    assert len(snapshot.skipped_accounts_json) == 1
    assert snapshot.skipped_accounts_json[0]["reason"] == "zero_reported_value"


def test_original_bytes_are_preserved_for_re_derivation(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """Extraction is a judgement call; the source must remain re-readable."""
    result = _import(session, settings, provider)
    stored = load_stored_bytes(result.source_file, settings)
    assert stored == FIXTURE.read_bytes()


def test_stored_files_are_owner_only(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """These are personal financial records on disk."""
    _import(session, settings, provider)
    written = list((settings.sources_dir).rglob("*"))
    files = [p for p in written if p.is_file()]
    assert files, "the document should have been written"
    assert oct(files[0].stat().st_mode)[-3:] == "600"


def test_identical_reimport_creates_no_new_state(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """The property that makes an accidental double upload harmless."""
    first = _import(session, settings, provider)
    assert first.outcome is not ImportOutcome.DUPLICATE

    accounts = session.query(Account).count()
    positions = session.query(PositionSnapshot).count()
    snapshots = session.query(PortfolioSnapshot).count()

    second = _import(session, settings, provider)
    assert second.outcome is ImportOutcome.DUPLICATE
    assert "already imported" in second.message
    assert second.snapshot is None

    assert session.query(Account).count() == accounts
    assert session.query(PositionSnapshot).count() == positions
    assert session.query(PortfolioSnapshot).count() == snapshots
    assert session.query(SourceFile).count() == 1


def test_a_renamed_copy_is_still_a_duplicate(
    session: Session, settings: Settings, provider: MockExtractionProvider, tmp_path: Path
) -> None:
    """Dedupe is by content hash, not filename."""
    _import(session, settings, provider)
    copy = tmp_path / "renamed_export.csv"
    shutil.copyfile(FIXTURE, copy)
    again = _import(session, settings, provider, copy)
    assert again.outcome is ImportOutcome.DUPLICATE


def test_positions_record_their_value_provenance(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """Everything from the authoritative source is `primary` until enriched."""
    _import(session, settings, provider)
    sources = {p.value_source for p in session.query(PositionSnapshot).all()}
    assert sources == {ValueSource.PRIMARY.value}


def test_cost_basis_is_reported_derived_or_honestly_unknown(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    _import(session, settings, provider)
    by_source: dict[str, int] = {}
    for position in session.query(PositionSnapshot).all():
        by_source[position.cost_basis_source] = by_source.get(position.cost_basis_source, 0) + 1
    assert by_source.get(CostBasisSource.REPORTED.value, 0) >= 1
    assert by_source.get(CostBasisSource.UNKNOWN.value, 0) >= 1
    for position in session.query(PositionSnapshot).all():
        if position.cost_basis_source == CostBasisSource.UNKNOWN.value:
            assert position.cost_basis is None, "unknown basis must never be imputed"


def test_one_security_is_shared_across_accounts(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """AAA is held natively and in an aggregated account at different prices."""
    _import(session, settings, provider)
    security = session.query(Security).filter_by(symbol="AAA").one()
    positions = session.query(PositionSnapshot).filter_by(security_id=security.id).all()
    assert len(positions) == 2
    assert len({p.price for p in positions}) == 2, "price is a per-position fact"
    assert sum(p.market_value or Decimal(0) for p in positions) == Decimal("40100.00")


def test_symbol_less_holdings_get_a_stable_shared_identity(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    _import(session, settings, provider)
    security = session.query(Security).filter_by(normalized_key="sample-500-index-portfolio").one()
    assert security.symbol is None


def test_accounts_are_classified_on_first_sight_with_masked_keys(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    _import(session, settings, provider)
    roth = session.query(Account).filter_by(display_name="ROTH IRA").one()
    assert roth.tax_treatment == "tax_free"
    assert roth.external_account_key_masked.endswith("2222")
    assert roth.external_account_key_masked != "222222222"


def test_reimport_does_not_reclassify_a_confirmed_account(
    session: Session, settings: Settings, provider: MockExtractionProvider, tmp_path: Path
) -> None:
    """Once a human has confirmed a type, an import must not silently undo it."""
    _import(session, settings, provider)
    account = session.query(Account).filter_by(display_name="ROTH IRA").one()
    account.tax_treatment = "taxable"
    account.classification_source = "user"
    session.flush()

    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext()).overview
    extracted = provider.extract_positions(document, overview, ExtractionContext()).document
    create_snapshot(session, extracted, overview=overview, settings=settings)

    session.refresh(account)
    assert account.tax_treatment == "taxable"
    assert account.classification_source == "user"


def test_incomplete_extraction_cannot_become_approved(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """THE critical guard: clean arithmetic must not buy past a missing account."""
    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext()).overview
    extracted = provider.extract_positions(document, overview, ExtractionContext()).document

    # Drop one account from stage 2 while leaving every surviving number intact.
    dropped = extracted.model_copy(update={"accounts": extracted.accounts[:-1]})

    result = create_snapshot(session, dropped, overview=overview, settings=settings)
    assert not result.validation.is_complete
    assert result.validation.requires_review
    assert result.snapshot.status == ImportStatus.NEEDS_REVIEW.value
    # The arithmetic on what remains is perfectly fine.
    assert result.reconciliation.passed


def test_reextraction_inserts_a_new_run_and_demotes_the_old(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """History is never overwritten."""
    result = _import(session, settings, provider)
    source = result.source_file
    first = active_run(session, source)
    assert first is not None and first.attempt_no == 1 and first.is_active

    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext())
    again = provider.extract_positions(document, overview.overview, ExtractionContext())
    second = record_extraction_run(session, source, again, overview=overview)

    session.refresh(first)
    assert first.is_active is False
    assert second.attempt_no == 2 and second.is_active
    assert [r.attempt_no for r in run_history(session, source)] == [2, 1]


def test_extraction_run_records_provenance_for_later_audit(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    result = _import(session, settings, provider)
    run = result.extraction_run
    assert run is not None
    assert run.provider == "mock"
    assert run.prompt_version and run.schema_version
    assert "summary" in run.reconciliation_json


def test_superseding_marks_the_earlier_snapshot_not_the_later(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """A corrected re-export has a new hash, so dedupe cannot catch it."""
    document = SourceDocument.from_path(FIXTURE)
    overview = provider.understand_document(document, ExtractionContext()).overview
    extracted = provider.extract_positions(document, overview, ExtractionContext()).document

    first = create_snapshot(session, extracted, overview=overview, settings=settings).snapshot
    second = create_snapshot(session, extracted, overview=overview, settings=settings).snapshot

    superseded = supersede_overlapping_snapshot(session, second)
    assert superseded is not None and superseded.id == first.id
    session.refresh(first)
    assert first.superseded_by == second.id
    assert second.superseded_by is None


def test_cost_estimate_uses_published_rates() -> None:
    cost = estimate_cost(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 10}
    )
    assert cost.estimated_usd == Decimal("30.0000")
    assert cost.cached_tokens == 10


def test_storing_the_same_document_twice_writes_one_file(
    session: Session, settings: Settings
) -> None:
    document = SourceDocument.from_path(FIXTURE)
    first = store_source_document(session, document, settings=settings)
    second = store_source_document(session, document, settings=settings)
    assert first.is_new and not second.is_new
    assert first.source_file.id == second.source_file.id
    assert len([p for p in settings.sources_dir.rglob("*") if p.is_file()]) == 1


def test_a_second_document_joins_the_snapshot_rather_than_competing_with_it(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """Two documents for one date are one portfolio, not two.

    Before this, each import opened its own snapshot, so importing an
    aggregation and then a direct source left the portfolio showing whichever
    file happened to be imported last -- on the real corpus, a 39% understatement
    that no reconciliation check could see, because each snapshot was internally
    consistent.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    snapshot = first.snapshot.snapshot
    accounts_before = len(snapshot.account_snapshots)
    assert first.multi_source is None, "the first document opens the snapshot"

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    # The fixtures were captured a day apart; this test is about two documents
    # describing the SAME date, so say so explicitly.
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    direct = direct.model_copy(update={"as_of_date": snapshot.as_of_date.strftime("%m/%d/%Y")})

    # Delivered as a PDF the mock replays from the fixture.
    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% a placeholder the mock never parses\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    second = import_document(session, pdf, provider, settings=settings)

    assert second.multi_source is not None, "a second document for the date must join"
    assert session.query(PortfolioSnapshot).count() == 1, "no competing snapshot"
    assert second.snapshot is not None
    assert second.snapshot.snapshot.id == snapshot.id
    # The direct source supplies its own accounts; the aggregation still covers
    # every account it alone reports.
    assert len(second.snapshot.snapshot.account_snapshots) == accounts_before
    assert second.multi_source.precedence.decisions


def test_a_joining_document_still_reports_its_own_warnings(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """A second source must not be quieter than a first.

    Extraction warnings are the most valuable thing a run produces -- a loss
    shown only by a red arrow, a truncated fund name. Routing a document through
    the join path is no reason to lose them.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None
    on_date = first.snapshot.snapshot.as_of_date.strftime("%m/%d/%Y")

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    direct = direct.model_copy(
        update={"warnings": ["a gain printed without a sign"], "as_of_date": on_date}
    )

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    result = import_document(session, pdf, provider, settings=settings)
    assert result.snapshot is not None
    assert "a gain printed without a sign" in result.snapshot.warnings


def test_the_import_total_counts_separately_reported_cash(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """A settlement fund shown in its own panel is still the owner's money.

    The import summary reported holdings alone and called it "total", which
    contradicted both the document's stated figure and the value the rest of the
    application computes. On the real corpus that headline understated the
    portfolio by 5%.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    direct = direct.model_copy(
        update={"as_of_date": first.snapshot.snapshot.as_of_date.strftime("%m/%d/%Y")}
    )
    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)
    result = import_document(session, pdf, provider, settings=settings)

    assert result.snapshot is not None
    cash = result.snapshot.cash_value
    assert cash > Decimal("0"), "the fixture must carry settlement cash to test this"
    assert result.snapshot.total_value == result.snapshot.holdings_value + cash, (
        "the headline total must include cash that is reported outside the holdings table"
    )


def test_as_of_files_two_documents_from_different_days_as_one_portfolio(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """The reason the override exists.

    An aggregation and a direct source are rarely exported on the same day. By
    date alone they open separate snapshots and the portfolio shows only one of
    them; placed on a common date they combine by precedence.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None
    on_date = first.snapshot.snapshot.as_of_date

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    stated = dt.date(2026, 8, 25)
    direct = direct.model_copy(update={"as_of_date": stated.strftime("%m/%d/%Y")})
    assert stated != on_date, "the fixtures must disagree for this test to mean anything"

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    # Without the override the document opens its own snapshot.
    without = import_document(session, pdf, provider, settings=settings)
    assert without.multi_source is None
    assert session.query(PortfolioSnapshot).count() == 2

    # With it, a second copy joins the aggregation's snapshot instead.
    other = tmp_path / "direct2.pdf"
    other.write_bytes(b"%PDF-1.4\n% a different placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(other).sha256, direct)
    joined = import_document(session, other, provider, as_of_date=on_date, settings=settings)

    assert joined.multi_source is not None
    assert joined.snapshot is not None
    assert joined.snapshot.snapshot.id == first.snapshot.snapshot.id


def test_an_overridden_date_says_what_it_overrode(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """A figure filed under a date it does not state must carry that fact."""
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None
    on_date = first.snapshot.snapshot.as_of_date

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    stated = on_date - dt.timedelta(days=2)
    direct = direct.model_copy(update={"as_of_date": stated.strftime("%m/%d/%Y")})

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    result = import_document(session, pdf, provider, as_of_date=on_date, settings=settings)
    assert result.snapshot is not None
    qualifier = [w for w in result.snapshot.warnings if stated.isoformat() in w]
    assert qualifier, f"expected the stated date to be reported; got {result.snapshot.warnings}"
    assert "2 day(s) apart" in qualifier[0]


def test_as_of_refuses_to_move_a_document_further_than_the_limit(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """Filing two documents together is legitimate; rewriting history is not.

    The arithmetic cannot tell the difference -- a month-old statement
    reconciles perfectly against itself -- so the distance is bounded, and
    exceeding it has to be deliberate.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument
    from portfolio_manager.services.importer import DateOverrideError

    provider = MockExtractionProvider()
    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    stated = dt.date(2026, 8, 25)
    direct = direct.model_copy(update={"as_of_date": stated.strftime("%m/%d/%Y")})

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    far = stated + dt.timedelta(days=settings.snapshot_date_tolerance_days + 1)
    with pytest.raises(DateOverrideError) as excinfo:
        import_document(session, pdf, provider, as_of_date=far, settings=settings)
    assert excinfo.value.stated == stated
    assert excinfo.value.requested == far

    # Nothing was written by the refused import.
    assert session.query(PortfolioSnapshot).count() == 0

    # And the same move is allowed when it is made deliberately.
    forced = import_document(
        session, pdf, provider, as_of_date=far, force_as_of=True, settings=settings
    )
    assert forced.snapshot is not None
    assert forced.snapshot.snapshot.as_of_date == far


def test_a_move_within_the_limit_is_allowed_without_forcing(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """The common case -- documents a few days apart -- needs no extra flag."""
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    stated = dt.date(2026, 8, 25)
    direct = direct.model_copy(update={"as_of_date": stated.strftime("%m/%d/%Y")})

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    near = stated + dt.timedelta(days=settings.snapshot_date_tolerance_days)
    result = import_document(session, pdf, provider, as_of_date=near, settings=settings)
    assert result.snapshot is not None
    assert result.snapshot.snapshot.as_of_date == near


def test_a_document_landing_beside_a_live_snapshot_says_so(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """Two documents exported a day apart must not part company in silence.

    Joining is by exact date, and `--as-of` is how a pair exported on different
    days becomes one portfolio. But nothing forced the owner to know that: a
    direct source dated one day off the aggregation opened its own snapshot,
    and both halves then described themselves as complete. The portfolio showed
    whichever was newer -- the aggregation alone, stale and cashless -- with no
    figure on screen revealing that the other half existed.

    So a new snapshot opening within the override tolerance of a live one now
    carries a warning naming that date.
    """
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None
    landed_on = first.snapshot.snapshot.as_of_date

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    a_day_later = landed_on + dt.timedelta(days=1)
    direct = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    direct = direct.model_copy(update={"as_of_date": a_day_later.strftime("%m/%d/%Y")})

    pdf = tmp_path / "direct.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% a placeholder the mock never parses\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, direct)

    second = import_document(session, pdf, provider, settings=settings)

    assert second.multi_source is None, "a different date genuinely is a different snapshot"
    assert second.snapshot is not None
    warnings = " ".join(second.snapshot.warnings)
    assert landed_on.isoformat() in warnings, (
        "the new snapshot must name the live snapshot it landed beside; "
        f"got {second.snapshot.warnings!r}"
    )
    assert "--as-of" in warnings, "and must say how to file the two as one portfolio"


def test_removing_a_snapshot_lets_its_document_be_imported_again(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """The whole point: a mis-filed document must become importable again.

    Imports deduplicate by content hash, so the retry that puts a mis-dated
    document on the right date does nothing at all until the first attempt is
    unfiled. Before this that took hand-written SQL against the system of
    record, which is the last place anyone should be typing DELETE.
    """
    from portfolio_manager.services.snapshot_removal import (
        find_snapshot,
        plan_removal,
        remove_snapshot,
    )

    first = import_document(session, FIXTURE, provider, settings=settings)
    assert first.outcome is ImportOutcome.IMPORTED
    landed_on = first.snapshot.snapshot.as_of_date

    again = import_document(session, FIXTURE, provider, settings=settings)
    assert again.outcome is ImportOutcome.DUPLICATE, "content-hash dedupe, as designed"

    snapshot = find_snapshot(session, landed_on.isoformat())
    plan = plan_removal(session, snapshot)
    assert [d.original_filename for d in plan.documents] == [FIXTURE.name]
    remove_snapshot(session, plan)

    retry = import_document(session, FIXTURE, provider, settings=settings)
    assert retry.outcome is ImportOutcome.IMPORTED, "the document is importable again"
    assert session.query(PortfolioSnapshot).count() == 1


def test_removal_leaves_the_preserved_document_on_disk(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """The bytes are the one artefact that cannot be reconstructed.

    The database records what a document was read to say, never the document,
    so removal drops the record and leaves the file. That costs a little space
    and makes the operation recoverable.
    """
    from portfolio_manager.services.snapshot_removal import (
        find_snapshot,
        plan_removal,
        remove_snapshot,
    )

    result = import_document(session, FIXTURE, provider, settings=settings)
    stored = Path(result.source_file.stored_path)
    if not stored.is_absolute():
        from portfolio_manager.config import REPO_ROOT

        stored = REPO_ROOT / stored
    assert stored.exists()

    snapshot = find_snapshot(session, str(result.snapshot.snapshot.id)[:8])
    remove_snapshot(session, plan_removal(session, snapshot))

    assert stored.exists(), "removal must not destroy the only copy of the document"


def test_removal_refuses_while_the_journal_still_cites_the_snapshot(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """A recommendation outlives the snapshot it cites, but not silently.

    The foreign key nulls the link rather than deleting the entry, so the
    journal would quietly lose the portfolio each suggestion was written
    against. That is a decision for the owner, not a side effect.
    """
    from portfolio_manager.models.advisory import Recommendation
    from portfolio_manager.services.snapshot_removal import (
        SnapshotRemovalError,
        find_snapshot,
        plan_removal,
        remove_snapshot,
    )

    result = import_document(session, FIXTURE, provider, settings=settings)
    snapshot_id = result.snapshot.snapshot.id
    session.add(
        Recommendation(
            portfolio_snapshot_id=snapshot_id,
            title="rebalance the bond sleeve",
            rationale="a fixture, not advice",
        )
    )
    session.flush()

    snapshot = find_snapshot(session, str(snapshot_id))
    plan = plan_removal(session, snapshot)
    assert plan.recommendation_count == 1
    assert plan.needs_force

    with pytest.raises(SnapshotRemovalError, match="recommendation"):
        remove_snapshot(session, plan)

    remove_snapshot(session, plan, force=True)
    assert session.query(PortfolioSnapshot).count() == 0
    assert session.query(Recommendation).count() == 1, "the entry survives, unlinked"


def test_an_ambiguous_reference_is_refused_rather_than_guessed(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """This deletes things, so a near-miss must not resolve to something."""
    from portfolio_manager.services.snapshot_removal import SnapshotRemovalError, find_snapshot

    import_document(session, FIXTURE, provider, settings=settings)

    with pytest.raises(SnapshotRemovalError, match="no snapshot matches"):
        find_snapshot(session, "1900-01-01")
    with pytest.raises(SnapshotRemovalError, match="no snapshot matches"):
        find_snapshot(session, "not-an-id")


def test_removal_never_deletes_accounts_or_securities(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """They are shared across snapshots and outlive any one of them."""
    from portfolio_manager.services.snapshot_removal import (
        find_snapshot,
        plan_removal,
        remove_snapshot,
    )

    result = import_document(session, FIXTURE, provider, settings=settings)
    accounts_before = session.query(Account).count()
    securities_before = session.query(Security).count()
    assert accounts_before and securities_before

    snapshot = find_snapshot(session, str(result.snapshot.snapshot.id))
    remove_snapshot(session, plan_removal(session, snapshot))

    assert session.query(Account).count() == accounts_before
    assert session.query(Security).count() == securities_before
    assert session.query(PositionSnapshot).count() == 0, "positions go with the snapshot"


def _direct_source_pdf(
    provider: MockExtractionProvider, tmp_path: Path, as_of: dt.date, name: str = "direct.pdf"
) -> Path:
    """The Vanguard-shaped direct source, replayed from its fixture as a PDF."""
    import json

    from portfolio_manager.extraction.canonical import ExtractedPortfolioDocument

    pair_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/vanguard/direct_source_anonymized.json"
    )
    document = ExtractedPortfolioDocument.model_validate(json.loads(pair_path.read_text()))
    document = document.model_copy(update={"as_of_date": as_of.strftime("%m/%d/%Y")})
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4\n% a placeholder the mock never parses\n")
    provider.register_pdf_fixture(SourceDocument.from_path(pdf).sha256, document)
    return pdf


def test_two_documents_at_once_need_no_date_typed(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """The pair becomes one portfolio without --as-of being involved."""
    from portfolio_manager.services.importer import import_documents

    provider = MockExtractionProvider()
    # The fixtures state dates a day apart, exactly as the real exports do.
    pdf = _direct_source_pdf(provider, tmp_path, dt.date(2026, 8, 27))

    outcome = import_documents(session, [PAIRED_AGGREGATION, pdf], provider, settings=settings)

    assert session.query(PortfolioSnapshot).count() == 1, "one portfolio, not two"
    assert outcome.as_of_date is not None
    assert all(r.outcome is ImportOutcome.IMPORTED for r in outcome.results)
    joined = [r for r in outcome.results if r.multi_source is not None]
    assert len(joined) == 1, "the second document joined rather than competing"


def test_the_widest_document_is_filed_first_whatever_order_you_list_them(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """The ordering is the feature, not a detail.

    A joining document may only supply accounts the snapshot already has. Given
    the direct source first, the aggregation's own accounts are unmatched and
    are not added -- measured on this fixture pair, 2 accounts survive instead
    of 10. Nobody should have to know that, so the order is chosen from what
    the documents turned out to contain rather than from the command line.
    """
    from portfolio_manager.services.importer import import_documents

    provider = MockExtractionProvider()
    pdf = _direct_source_pdf(provider, tmp_path, dt.date(2026, 8, 27))

    # Listed the losing way round on purpose.
    outcome = import_documents(session, [pdf, PAIRED_AGGREGATION], provider, settings=settings)

    assert outcome.reordered, "the request order was not the order used"
    assert outcome.order[0] == PAIRED_AGGREGATION.name, "the aggregation covers more accounts"

    snapshot = session.query(PortfolioSnapshot).one()
    assert len(snapshot.account_snapshots) == 10
    for result in outcome.results:
        if result.multi_source is not None:
            assert not result.multi_source.unmatched_accounts, "nothing was left behind"


def test_one_document_still_behaves_exactly_as_it_did(
    session: Session, settings: Settings, provider: MockExtractionProvider
) -> None:
    """A single path must not acquire multi-document behaviour."""
    from portfolio_manager.services.importer import import_documents

    outcome = import_documents(session, [FIXTURE], provider, settings=settings)

    assert len(outcome.results) == 1
    assert not outcome.reordered
    assert outcome.results[0].outcome is ImportOutcome.IMPORTED
    assert session.query(PortfolioSnapshot).count() == 1


def test_a_duplicate_among_several_does_not_stop_the_others(
    session: Session, settings: Settings, tmp_path: Path
) -> None:
    """Re-running the same command after adding one new file must work."""
    from portfolio_manager.services.importer import import_documents

    provider = MockExtractionProvider()
    first = import_document(session, PAIRED_AGGREGATION, provider, settings=settings)
    assert first.snapshot is not None
    pdf = _direct_source_pdf(provider, tmp_path, first.snapshot.snapshot.as_of_date)

    outcome = import_documents(session, [PAIRED_AGGREGATION, pdf], provider, settings=settings)

    outcomes = {r.outcome for r in outcome.results}
    assert ImportOutcome.DUPLICATE in outcomes, "the already-imported file is reported as such"
    assert ImportOutcome.IMPORTED in outcomes, "and the new one still lands"
    assert session.query(PortfolioSnapshot).count() == 1
