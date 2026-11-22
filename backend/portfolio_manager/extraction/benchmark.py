"""Scoring an extraction provider against a corpus of known-good readings.

This exists to answer one question objectively: **is a different provider good
enough to replace the current one?** Without it that decision is made on
impressions, and impressions about extraction quality are unreliable -- a
provider that gets 95% of positions right looks excellent and is unusable,
because the missing 5% is a portfolio that silently does not add up.

So the score is deliberately unforgiving:

* An account that is missed costs more than a field that is wrong. A wrong
  number is visible in reconciliation; a missing account is not.
* Field accuracy is measured only over positions that matched, and the match
  rate is reported alongside it -- otherwise a provider that returns three
  perfect positions out of eighty scores 100%.
* A document counts as passed only if every account and position is found AND
  every field matches. "Mostly right" is not a category.

The expected readings are produced by a deterministic parse of each fixture,
reviewed once and committed, so the baseline is not itself a model's opinion.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..ingestion.normalizer import NormalizationError, normalize_security_key, parse_money
from .canonical import ExtractedAccount, ExtractedPortfolioDocument
from .contracts import (
    DocumentExtractionProvider,
    ExtractionContext,
    ExtractionError,
    SourceDocument,
)

#: Cost per million tokens, Claude Opus 5 list pricing. Only used to put a
#: dollar figure on a benchmark run; not a billing source of truth.
INPUT_COST_PER_MTOK = Decimal("5")
OUTPUT_COST_PER_MTOK = Decimal("25")

#: Fields compared position by position. Money is compared as a Decimal so
#: "$1,000.00" and "1000" count as equal -- the normalizer's job, not the
#: provider's.
MONEY_FIELDS = ("quantity", "price", "market_value", "cost_basis", "unrealized_gain_loss")


@dataclass
class DocumentScore:
    name: str
    ok: bool
    error: str | None = None

    expected_accounts: int = 0
    found_accounts: int = 0
    matched_accounts: int = 0
    missing_accounts: list[str] = field(default_factory=list)
    invented_accounts: list[str] = field(default_factory=list)

    expected_positions: int = 0
    found_positions: int = 0
    matched_positions: int = 0

    fields_compared: int = 0
    fields_correct: int = 0
    field_errors: list[str] = field(default_factory=list)

    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def account_recall(self) -> Decimal:
        if not self.expected_accounts:
            return Decimal(1)
        return Decimal(self.matched_accounts) / Decimal(self.expected_accounts)

    @property
    def position_recall(self) -> Decimal:
        if not self.expected_positions:
            return Decimal(1)
        return Decimal(self.matched_positions) / Decimal(self.expected_positions)

    @property
    def field_accuracy(self) -> Decimal:
        """Accuracy over MATCHED positions only.

        Meaningless without `position_recall` beside it, which is why every
        report prints them together.
        """
        if not self.fields_compared:
            return Decimal(1)
        return Decimal(self.fields_correct) / Decimal(self.fields_compared)

    @property
    def passed(self) -> bool:
        """Everything found, everything correct. There is no partial credit."""
        return (
            self.ok
            and not self.missing_accounts
            and not self.invented_accounts
            and self.matched_positions == self.expected_positions
            and self.fields_correct == self.fields_compared
        )

    @property
    def estimated_cost(self) -> Decimal:
        return (
            Decimal(self.input_tokens) * INPUT_COST_PER_MTOK
            + Decimal(self.output_tokens) * OUTPUT_COST_PER_MTOK
        ) / Decimal(1_000_000)


@dataclass
class BenchmarkReport:
    provider: str
    model: str | None
    documents: list[DocumentScore] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for d in self.documents if d.passed)

    @property
    def failed(self) -> int:
        return len(self.documents) - self.passed

    @property
    def errored(self) -> list[DocumentScore]:
        return [d for d in self.documents if not d.ok]

    def _weighted(self, numerator: str, denominator: str) -> Decimal:
        top = sum(getattr(d, numerator) for d in self.documents)
        bottom = sum(getattr(d, denominator) for d in self.documents)
        return Decimal(1) if bottom == 0 else Decimal(top) / Decimal(bottom)

    @property
    def account_recall(self) -> Decimal:
        return self._weighted("matched_accounts", "expected_accounts")

    @property
    def position_recall(self) -> Decimal:
        return self._weighted("matched_positions", "expected_positions")

    @property
    def field_accuracy(self) -> Decimal:
        return self._weighted("fields_correct", "fields_compared")

    @property
    def total_cost(self) -> Decimal:
        return sum((d.estimated_cost for d in self.documents), Decimal(0))

    @property
    def total_latency_ms(self) -> int:
        return sum(d.latency_ms for d in self.documents)

    @property
    def cache_hit_rate(self) -> Decimal:
        cached = sum(d.cached_tokens for d in self.documents)
        total = sum(d.input_tokens for d in self.documents) + cached
        return Decimal(0) if total == 0 else Decimal(cached) / Decimal(total)

    @property
    def is_acceptable(self) -> bool:
        """Whether this provider could replace the current one.

        Every document must pass. A provider that fails one document in a
        ten-document corpus fails one import in ten, and an import that is
        wrong without saying so is worse than one that refuses.
        """
        return bool(self.documents) and self.failed == 0


@dataclass(frozen=True)
class Fixture:
    name: str
    document: Path
    expected: Path

    def load_expected(self) -> ExtractedPortfolioDocument:
        return ExtractedPortfolioDocument.model_validate(json.loads(self.expected.read_text()))


def discover_fixtures(root: Path) -> list[Fixture]:
    """Every document in the corpus that has an expected reading beside it.

    A document without an expected reading is skipped rather than scored as a
    failure: an unreviewed fixture is not a benchmark case.
    """
    found: list[Fixture] = []
    for expected in sorted(root.rglob("*.expected.json")):
        stem = expected.name.removesuffix(".expected.json")
        for candidate in sorted(expected.parent.glob(f"{stem}.*")):
            if candidate.name.endswith(".expected.json"):
                continue
            found.append(Fixture(name=stem, document=candidate, expected=expected))
            break
    return found


def _position_key(symbol: str | None, description: str) -> str:
    try:
        return normalize_security_key(symbol, description)
    except NormalizationError:
        return (symbol or description or "?").strip().upper()


def _money_equal(left: str | None, right: str | None) -> bool:
    """Compare money by value, not by spelling.

    "$1,000.00" and "1000" are the same amount; normalising them is the
    normalizer's job, and holding a provider to a formatting convention would
    measure the wrong thing.
    """
    try:
        return parse_money(left) == parse_money(right)
    except NormalizationError:
        return (left or "") == (right or "")


def _accounts_by_key(document: ExtractedPortfolioDocument) -> dict[str, ExtractedAccount]:
    return {a.external_account_key: a for a in document.accounts}


def score_document(
    fixture: Fixture,
    provider: DocumentExtractionProvider,
) -> DocumentScore:
    """Run one document through a provider and score the result."""
    score = DocumentScore(name=fixture.name, ok=False)
    expected = fixture.load_expected()
    score.expected_accounts = len(expected.accounts)
    score.expected_positions = sum(len(a.positions) for a in expected.accounts)

    document = SourceDocument.from_path(fixture.document)
    started = time.perf_counter()
    try:
        overview = provider.understand_document(document, ExtractionContext())
        result = provider.extract_positions(document, overview.overview, ExtractionContext())
    except ExtractionError as exc:
        score.latency_ms = int((time.perf_counter() - started) * 1000)
        score.error = str(exc)
        return score

    score.ok = True
    score.latency_ms = int((time.perf_counter() - started) * 1000)
    usage = {**overview.token_usage}
    for key, value in result.token_usage.items():
        usage[key] = usage.get(key, 0) + value
    score.input_tokens = int(usage.get("input_tokens", 0))
    score.output_tokens = int(usage.get("output_tokens", 0))
    score.cached_tokens = int(usage.get("cache_read_input_tokens", 0))

    actual = result.document
    score.found_accounts = len(actual.accounts)
    score.found_positions = sum(len(a.positions) for a in actual.accounts)

    expected_accounts = _accounts_by_key(expected)
    actual_accounts = _accounts_by_key(actual)
    score.missing_accounts = sorted(set(expected_accounts) - set(actual_accounts))
    score.invented_accounts = sorted(set(actual_accounts) - set(expected_accounts))
    score.matched_accounts = len(set(expected_accounts) & set(actual_accounts))

    for key, expected_account in expected_accounts.items():
        actual_account = actual_accounts.get(key)
        if actual_account is None:
            continue
        actual_positions = {
            _position_key(p.symbol, p.description): p for p in actual_account.positions
        }
        for expected_position in expected_account.positions:
            position_key = _position_key(expected_position.symbol, expected_position.description)
            actual_position = actual_positions.get(position_key)
            if actual_position is None:
                continue
            score.matched_positions += 1
            for name in MONEY_FIELDS:
                score.fields_compared += 1
                if _money_equal(getattr(expected_position, name), getattr(actual_position, name)):
                    score.fields_correct += 1
                elif len(score.field_errors) < 20:
                    score.field_errors.append(
                        f"{key[:12]}/{position_key}.{name}: expected "
                        f"{getattr(expected_position, name)!r}, got "
                        f"{getattr(actual_position, name)!r}"
                    )
            # The symbol is not money but getting it wrong breaks identity.
            score.fields_compared += 1
            if (expected_position.symbol or None) == (actual_position.symbol or None):
                score.fields_correct += 1
            elif len(score.field_errors) < 20:
                score.field_errors.append(
                    f"{key[:12]}/{position_key}.symbol: expected "
                    f"{expected_position.symbol!r}, got {actual_position.symbol!r}"
                )

    return score


def run_benchmark(
    fixtures: list[Fixture],
    provider: DocumentExtractionProvider,
    *,
    model: str | None = None,
) -> BenchmarkReport:
    report = BenchmarkReport(provider=provider.name, model=model)
    for fixture in fixtures:
        report.documents.append(score_document(fixture, provider))
    return report
