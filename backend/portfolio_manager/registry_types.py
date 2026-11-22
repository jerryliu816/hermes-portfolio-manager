"""Small shared enums that would otherwise create import cycles."""

from __future__ import annotations

from enum import StrEnum


class ImportOutcome(StrEnum):
    #: New portfolio state was created.
    IMPORTED = "imported"
    #: The identical file had already been imported; nothing changed.
    DUPLICATE = "duplicate"
    #: State was created but a human must confirm it before it is trusted.
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
