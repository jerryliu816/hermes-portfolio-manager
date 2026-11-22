"""The user's investment philosophy: versioned prose.

Saved the same way as the policy -- each save inserts a new version and retires
the previous one -- so "what did I believe when I accepted that?" stays
answerable. Unlike the policy, nothing here is validated beyond being present:
it is read by the advisor, never checked by the system.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.advisory import InvestmentPhilosophy


class PhilosophyError(ValueError):
    """A philosophy could not be saved."""


def active_philosophy(session: Session) -> InvestmentPhilosophy | None:
    found: InvestmentPhilosophy | None = (
        session.execute(
            select(InvestmentPhilosophy)
            .where(InvestmentPhilosophy.is_active.is_(True))
            .order_by(InvestmentPhilosophy.version.desc())
        )
        .scalars()
        .first()
    )
    return found


def philosophy_history(session: Session) -> list[InvestmentPhilosophy]:
    """Every version, newest first. Nothing is ever deleted."""
    return list(
        session.execute(
            select(InvestmentPhilosophy).order_by(InvestmentPhilosophy.version.desc())
        ).scalars()
    )


def save_philosophy(session: Session, body_markdown: str) -> InvestmentPhilosophy:
    """Insert a new version and retire the previous one.

    An empty philosophy is refused rather than stored: an advisor told "the
    user's philosophy is ''" would reasonably conclude they have none, which is
    a different thing from them not having written it down yet.
    """
    text = body_markdown.strip()
    if not text:
        raise PhilosophyError("the philosophy is empty; there is nothing to save")
    previous = philosophy_history(session)
    for record in previous:
        record.is_active = False
    record = InvestmentPhilosophy(
        version=(previous[0].version + 1) if previous else 1,
        is_active=True,
        body_markdown=body_markdown,
    )
    session.add(record)
    session.flush()
    return record


#: Offered when none exists. Questions, not answers: a starter that stated a
#: philosophy would be advice wearing the user's name.
STARTER_PHILOSOPHY = """\
# Investment philosophy

Write this in your own words. The advisor reads it before every proposal.

## Goals and horizon
- What is this money for, and when will you need it?

## Risk
- How large a fall could you sit through without selling?

## Beliefs
- e.g. passive vs active, home bias, factor tilts, individual stocks.

## Never
- Things you will not hold or do, whatever the argument.

## Taxes and friction
- How much tax are you willing to realise to get closer to target?
"""
