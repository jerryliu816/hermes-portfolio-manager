"""Prompt content.

Prompts are the least testable part of a GenAI pipeline, but the specific
instructions below each exist because a real document breaks without them.
Asserting their presence stops a future edit from quietly dropping one.
"""

from __future__ import annotations

import pytest

from portfolio_manager.extraction.prompts.base import (
    AGGREGATION_RULES,
    DATE_RULES,
    TRANSCRIPTION_RULES,
)
from portfolio_manager.extraction.prompts.portfolio import (
    OVERVIEW_SYSTEM,
    POSITIONS_SYSTEM,
    positions_user_prompt,
)
from portfolio_manager.extraction.prompts.recovery import RECOVERY_SYSTEM, recovery_user_prompt
from portfolio_manager.extraction.prompts.versions import (
    OVERVIEW_PROMPT_VERSION,
    POSITIONS_PROMPT_VERSION,
    RECOVERY_PROMPT_VERSION,
)


def test_recovery_forbids_editing_values_to_force_reconciliation() -> None:
    """The single most important sentence in the whole prompt set.

    A model told to make numbers reconcile will edit a correctly transcribed
    figure, converting a detectable data problem into an undetectable one.
    """
    assert "DO NOT ALTER A CORRECTLY TRANSCRIBED VALUE" in RECOVERY_SYSTEM
    assert "document is simply inconsistent" in RECOVERY_SYSTEM


def test_transcription_rules_forbid_computing_missing_values() -> None:
    """29% of real holdings have no cost basis; computing one destroys signal."""
    assert "NEVER COMPUTE A MISSING VALUE" in TRANSCRIPTION_RULES
    assert "return null" in TRANSCRIPTION_RULES
    assert 'distinguish "unknown" from "zero"' in TRANSCRIPTION_RULES


def test_transcription_rules_distinguish_absent_from_zero() -> None:
    assert '"--"' in TRANSCRIPTION_RULES
    assert "A zero and an absence are different facts." in TRANSCRIPTION_RULES


def test_transcription_rules_allow_a_missing_ticker() -> None:
    """12 of 86 real rows have no symbol."""
    assert "NO TICKER SYMBOL" in TRANSCRIPTION_RULES
    assert "do not guess a ticker" in TRANSCRIPTION_RULES


def test_transcription_rules_keep_zero_quantity_rows() -> None:
    assert "INCLUDE ZERO-QUANTITY ROWS" in TRANSCRIPTION_RULES


def test_transcription_rules_reject_page_furniture() -> None:
    """The Vanguard file is a browser printout full of numeric page chrome."""
    assert "IGNORE PAGE FURNITURE" in TRANSCRIPTION_RULES
    for noise in ("navigation", "ticker bar", "promotional"):
        assert noise in TRANSCRIPTION_RULES


def test_aggregation_rules_ask_for_the_custodian_not_the_publisher() -> None:
    assert "ACTUALLY HOLDS" in AGGREGATION_RULES
    assert "not the one that produced the document" in AGGREGATION_RULES
    assert '"unknown"' in AGGREGATION_RULES


def test_date_rules_prefer_an_explicit_as_of_over_a_download_stamp() -> None:
    """The real CSV's only date is a download stamp in a footer."""
    assert "footer" in DATE_RULES
    assert "do not substitute today's date" in DATE_RULES


def test_both_stage_prompts_carry_the_shared_rules() -> None:
    for prompt in (OVERVIEW_SYSTEM, POSITIONS_SYSTEM):
        assert "NEVER COMPUTE A MISSING VALUE" in prompt
        assert "ACTUALLY HOLDS" in prompt


def test_overview_prompt_emphasises_finding_every_account() -> None:
    """A missed account is the pipeline's most dangerous failure."""
    assert "ACCOUNT DISCOVERY IS THE CRITICAL OUTPUT" in OVERVIEW_SYSTEM
    assert "EVERY account" in OVERVIEW_SYSTEM


def test_scoped_prompt_lists_only_the_requested_accounts() -> None:
    prompt = positions_user_prompt("f.csv", "csv", ("A1", "B2"), "note")
    assert "- A1" in prompt and "- B2" in prompt
    assert "Ignore every other account" in prompt


def test_unscoped_prompt_asks_for_everything() -> None:
    prompt = positions_user_prompt("f.csv", "csv", (), "note")
    assert "EVERY account" in prompt


def test_recovery_prompt_lists_the_actual_failures() -> None:
    prompt = recovery_user_prompt("f.csv", "csv", ("account X: sum 100 != stated 200",))
    assert "account X: sum 100 != stated 200" in prompt


@pytest.mark.parametrize(
    "version",
    [OVERVIEW_PROMPT_VERSION, POSITIONS_PROMPT_VERSION, RECOVERY_PROMPT_VERSION],
)
def test_prompt_versions_are_recorded(version: str) -> None:
    """Every ExtractionRun stores these, so quality changes stay attributable."""
    assert version and "-" in version


def test_transcription_rules_read_direction_from_a_glyph() -> None:
    """A real holding's loss was shown by a red down arrow and no minus sign.

    Rule 1 says copy the sign as printed, which here means copying nothing --
    so the loss was transcribed as a gain. It is worse than a wrong display:
    where the document reports no cost basis one is derived as
    value - gain_loss, so a flipped sign moves the basis by twice the amount
    and the position reads as a winner.
    """
    assert "DIRECTION SHOWN BY A GLYPH OR COLOUR" in TRANSCRIPTION_RULES
    assert "arrow" in TRANSCRIPTION_RULES
    assert "return null and warn" in TRANSCRIPTION_RULES
