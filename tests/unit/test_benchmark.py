"""The extraction benchmark.

A scorer that always returns 100% is worse than no scorer, so most of these
tests feed it a deliberately wrong provider and check that it notices. The
mock scoring perfectly is the least interesting fact here -- it produced the
baseline.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_manager.extraction.benchmark import (
    discover_fixtures,
    run_benchmark,
    score_document,
)
from portfolio_manager.extraction.canonical import (
    DocumentOverview,
)
from portfolio_manager.extraction.contracts import (
    ExtractionContext,
    ExtractionError,
    ExtractionErrorKind,
    ExtractionResult,
    OverviewResult,
    SourceDocument,
)
from portfolio_manager.extraction.providers.mock_provider import MockExtractionProvider

CORPUS = Path(__file__).resolve().parents[1] / "fixtures/benchmark"


class _Mutating(MockExtractionProvider):
    """A provider that reads correctly, then damages the result.

    Stands in for the failure modes that matter: dropping an account, dropping
    holdings, and getting a number wrong.
    """

    def __init__(self, mutate: str) -> None:
        super().__init__()
        self._mutate = mutate

    @property
    def name(self) -> str:
        return f"mutating-{self._mutate}"

    def extract_positions(
        self,
        document: SourceDocument,
        overview: DocumentOverview,
        context: ExtractionContext,
    ) -> ExtractionResult:
        result = super().extract_positions(document, overview, context)
        parsed = result.document
        if self._mutate == "drop_account" and parsed.accounts:
            parsed = parsed.model_copy(update={"accounts": parsed.accounts[:-1]})
        elif self._mutate == "drop_positions" and parsed.accounts:
            first = parsed.accounts[0]
            parsed = parsed.model_copy(
                update={
                    "accounts": [
                        first.model_copy(update={"positions": first.positions[:1]}),
                        *parsed.accounts[1:],
                    ]
                }
            )
        elif self._mutate == "wrong_value" and parsed.accounts:
            first = parsed.accounts[0]
            damaged = first.positions[0].model_copy(update={"market_value": "$999999.99"})
            parsed = parsed.model_copy(
                update={
                    "accounts": [
                        first.model_copy(update={"positions": [damaged, *first.positions[1:]]}),
                        *parsed.accounts[1:],
                    ]
                }
            )
        elif self._mutate == "invent_account":
            extra = parsed.accounts[0].model_copy(
                update={"external_account_key": "INVENTED", "positions": []}
            )
            parsed = parsed.model_copy(update={"accounts": [*parsed.accounts, extra]})
        elif self._mutate == "reformat":
            # Same amounts, different spelling. Must NOT count as an error.
            first = parsed.accounts[0]
            original = first.positions[0]
            value = (original.market_value or "").replace("$", "").replace(",", "")
            reformatted = original.model_copy(update={"market_value": value or None})
            parsed = parsed.model_copy(
                update={
                    "accounts": [
                        first.model_copy(update={"positions": [reformatted, *first.positions[1:]]}),
                        *parsed.accounts[1:],
                    ]
                }
            )
        return ExtractionResult(
            document=parsed,
            provider=self.name,
            model=None,
            prompt_version=result.prompt_version,
            schema_version=result.schema_version,
            latency_ms=result.latency_ms,
        )


class _Failing(MockExtractionProvider):
    @property
    def name(self) -> str:
        return "failing"

    def understand_document(
        self, document: SourceDocument, context: ExtractionContext
    ) -> OverviewResult:
        raise ExtractionError(ExtractionErrorKind.TIMEOUT, "the provider timed out")


def test_the_corpus_is_discovered() -> None:
    fixtures = discover_fixtures(CORPUS)
    assert len(fixtures) >= 7
    names = {f.name for f in fixtures}
    assert {"symbol_less_529", "split_price_divergence", "zero_value_account"} <= names
    for fixture in fixtures:
        assert fixture.document.exists() and fixture.expected.exists()


def test_a_document_with_no_expected_reading_is_not_scored(tmp_path: Path) -> None:
    """An unreviewed fixture is not a benchmark case."""
    (tmp_path / "orphan.csv").write_text("a,b\n1,2\n")
    assert discover_fixtures(tmp_path) == []


def test_the_baseline_provider_scores_perfectly() -> None:
    report = run_benchmark(discover_fixtures(CORPUS), MockExtractionProvider())
    assert report.is_acceptable
    assert report.failed == 0
    assert report.field_accuracy == Decimal(1)


@pytest.mark.parametrize(
    "mutation", ["drop_account", "drop_positions", "wrong_value", "invent_account"]
)
def test_a_damaged_provider_is_caught(mutation: str) -> None:
    """The whole point: the scorer must notice each way of being wrong."""
    report = run_benchmark(discover_fixtures(CORPUS), _Mutating(mutation))
    assert not report.is_acceptable, f"{mutation} went undetected"
    assert report.failed > 0


def test_a_dropped_account_is_named() -> None:
    """A missing account costs more than a wrong number, so it is reported."""
    fixture = next(f for f in discover_fixtures(CORPUS) if f.name == "zero_value_account")
    score = score_document(fixture, _Mutating("drop_account"))
    assert score.missing_accounts
    assert not score.passed
    assert score.account_recall < Decimal(1)


def test_an_invented_account_is_named() -> None:
    fixture = next(f for f in discover_fixtures(CORPUS) if f.name == "minimal")
    score = score_document(fixture, _Mutating("invent_account"))
    assert score.invented_accounts == ["INVENTED"]
    assert not score.passed


def test_money_is_compared_by_value_not_spelling() -> None:
    """Holding a provider to a formatting convention would measure the wrong thing."""
    report = run_benchmark(discover_fixtures(CORPUS), _Mutating("reformat"))
    assert report.is_acceptable, "reformatting the same amount must not count as an error"


def test_field_accuracy_is_reported_beside_position_recall() -> None:
    """Perfect accuracy over three of eighty positions is not 100% correct."""
    fixture = next(f for f in discover_fixtures(CORPUS) if f.name == "null_sentinels")
    score = score_document(fixture, _Mutating("drop_positions"))
    assert score.field_accuracy == Decimal(1), "the kept position is still perfect"
    assert score.position_recall < Decimal(1), "but most were not returned"
    assert not score.passed, "and that must not pass"


def test_a_provider_error_is_recorded_not_raised() -> None:
    """One unreadable document must not abort the whole run."""
    report = run_benchmark(discover_fixtures(CORPUS), _Failing())
    assert not report.is_acceptable
    assert len(report.errored) == len(report.documents)
    assert all("timed out" in (d.error or "") for d in report.documents)


def test_cost_is_estimated_from_token_usage() -> None:
    from portfolio_manager.extraction.benchmark import DocumentScore

    score = DocumentScore(name="x", ok=True, input_tokens=1_000_000, output_tokens=1_000_000)
    assert score.estimated_cost == Decimal(30)


def test_an_empty_corpus_is_not_acceptable() -> None:
    """Zero documents passing zero checks is not evidence of anything."""
    report = run_benchmark([], MockExtractionProvider())
    assert not report.is_acceptable
