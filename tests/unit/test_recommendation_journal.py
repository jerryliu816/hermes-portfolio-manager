"""The recommendation journal.

Append-only by design: a recommendation deferred in March and accepted in June
has a history worth keeping, and overwriting the earlier decision would erase
the fact that it was once set aside.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from portfolio_manager.services.recommendation import (
    DecisionType,
    RecommendationStatus,
    create_recommendation,
    get_recommendation,
    journal_stats,
    list_recommendations,
    record_decision,
)


def _add(session: Session, title: str = "Rebalance", source: str = "hermes") -> str:
    entry = create_recommendation(
        session, title=title, rationale="because the numbers say so", source=source
    )
    return str(entry.recommendation.id)


def test_a_new_recommendation_is_pending_and_open(session: Session) -> None:
    entry = create_recommendation(session, title="t", rationale="r")
    assert entry.status is RecommendationStatus.PENDING
    assert entry.is_open
    assert entry.latest_decision is None


def test_a_decision_sets_the_status(session: Session) -> None:
    identifier = _add(session)
    entry = record_decision(session, identifier, DecisionType.ACCEPTED, note="did it")
    assert entry is not None
    assert entry.status is RecommendationStatus.ACCEPTED
    assert not entry.is_open
    assert entry.latest_decision is not None
    assert entry.latest_decision.note == "did it"


def test_deferring_leaves_it_open(session: Session) -> None:
    """Setting something aside is not the same as being finished with it."""
    identifier = _add(session)
    entry = record_decision(session, identifier, DecisionType.DEFERRED)
    assert entry is not None
    assert entry.status is RecommendationStatus.DEFERRED
    assert entry.is_open


def test_decisions_append_and_the_history_survives(session: Session) -> None:
    """Deferred in March, accepted in June -- both facts are kept."""
    identifier = _add(session)
    record_decision(session, identifier, DecisionType.DEFERRED, note="not now")
    entry = record_decision(session, identifier, DecisionType.ACCEPTED, note="now")
    assert entry is not None
    assert entry.status is RecommendationStatus.ACCEPTED
    assert [d.decision for d in entry.decisions] == ["deferred", "accepted"]
    assert entry.decisions[0].note == "not now"


def test_deciding_on_a_missing_recommendation_returns_none(session: Session) -> None:
    import uuid

    assert record_decision(session, str(uuid.uuid4()), DecisionType.ACCEPTED) is None


def test_listing_filters_by_derived_status(session: Session) -> None:
    accepted = _add(session, "accepted one")
    _add(session, "pending one")
    rejected = _add(session, "rejected one")
    record_decision(session, accepted, DecisionType.ACCEPTED)
    record_decision(session, rejected, DecisionType.REJECTED)

    titles = {
        e.recommendation.title
        for e in list_recommendations(session, status=RecommendationStatus.PENDING)
    }
    assert titles == {"pending one"}
    assert len(list_recommendations(session, status=RecommendationStatus.ACCEPTED)) == 1


def test_listing_filters_by_source(session: Session) -> None:
    _add(session, "from hermes", source="hermes")
    _add(session, "from me", source="user")
    entries = list_recommendations(session, source="user")
    assert [e.recommendation.title for e in entries] == ["from me"]


def test_stats_count_every_status(session: Session) -> None:
    accepted = _add(session, "a")
    rejected = _add(session, "b")
    deferred = _add(session, "c")
    _add(session, "d")
    record_decision(session, accepted, DecisionType.ACCEPTED)
    record_decision(session, rejected, DecisionType.REJECTED)
    record_decision(session, deferred, DecisionType.DEFERRED)

    stats = journal_stats(session)
    assert (stats.total, stats.accepted, stats.rejected, stats.deferred, stats.pending) == (
        4,
        1,
        1,
        1,
        1,
    )
    assert stats.decided == 2
    assert stats.acceptance_rate == Decimal("0.5")


def test_acceptance_rate_is_none_when_nothing_was_decided(session: Session) -> None:
    """A rate computed from no decisions is a number pretending to be a signal."""
    _add(session)
    stats = journal_stats(session)
    assert stats.acceptance_rate is None
    assert stats.decided == 0


def test_deferred_recommendations_do_not_count_as_decided(session: Session) -> None:
    """The user has not made up their mind, so a rate must not imply they have."""
    deferred = _add(session)
    record_decision(session, deferred, DecisionType.DEFERRED)
    assert journal_stats(session).acceptance_rate is None


@pytest.mark.parametrize("decision", list(DecisionType))
def test_every_decision_type_round_trips(session: Session, decision: DecisionType) -> None:
    identifier = _add(session)
    record_decision(session, identifier, decision)
    entry = get_recommendation(session, identifier)
    assert entry is not None
    assert entry.status.value == decision.value
