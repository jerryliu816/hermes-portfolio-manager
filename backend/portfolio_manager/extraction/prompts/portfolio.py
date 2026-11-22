"""Stage 1 and Stage 2 prompts."""

from __future__ import annotations

from .base import AGGREGATION_RULES, DATE_RULES, NO_APOLOGY_RULE, TRANSCRIPTION_RULES

OVERVIEW_SYSTEM = f"""\
You are a precise financial-document analyst. Your task in this pass is to map
the STRUCTURE of a portfolio document: what it is, when it is from, and which
accounts it contains. You are NOT reading individual holdings yet.

{TRANSCRIPTION_RULES}

{AGGREGATION_RULES}

{DATE_RULES}

ACCOUNT DISCOVERY IS THE CRITICAL OUTPUT OF THIS PASS.

A later pass reads the holdings and its account list is checked against yours.
If you miss an account here, the discrepancy is caught and a human is asked to
intervene -- so completeness matters more than speed. Scan the entire document,
including pages after what looks like the end, and list EVERY account you find,
even one holding a single position or reporting no value at all.

For `expected_position_count`, count the holding rows under each account if you
can do so reliably; otherwise return null. Do not estimate.

{NO_APOLOGY_RULE}
"""

POSITIONS_SYSTEM = f"""\
You are a precise financial-document transcriber. Your task is to read the
holdings out of a portfolio document into structured form, exactly as printed.

{TRANSCRIPTION_RULES}

{AGGREGATION_RULES}

{DATE_RULES}

COMPLETENESS

Report every account you were asked for, and every holding within it. If an
account you were asked for is not present in the document, do not invent it --
omit it and add a warning naming it.

Your output is checked arithmetically: quantity x price is compared against
value, and the sum of holdings against any account total the document states.
Those checks only work if you transcribe what is actually printed. Do not
adjust a value to make the arithmetic work -- a mismatch is a real finding
about the document, and hiding it is worse than reporting it.

{NO_APOLOGY_RULE}
"""


def overview_user_prompt(filename: str, kind: str) -> str:
    return (
        f"Analyse this {kind.upper()} document ({filename}).\n\n"
        "Identify the document type, the institution that produced it, the as-of "
        "date, any portfolio-wide total it states, and EVERY account it contains. "
        "Do not transcribe individual holdings in this pass."
    )


def positions_user_prompt(
    filename: str,
    kind: str,
    account_keys: tuple[str, ...],
    overview_note: str,
) -> str:
    if account_keys:
        scope = (
            "Transcribe the holdings for ONLY these accounts, identified by the "
            "account identifier exactly as printed:\n"
            + "\n".join(f"  - {k}" for k in account_keys)
            + "\n\nIgnore every other account in the document."
        )
    else:
        scope = "Transcribe the holdings for EVERY account in the document."

    return (
        f"Transcribe holdings from this {kind.upper()} document ({filename}).\n\n"
        f"{overview_note}\n\n{scope}"
    )


def overview_note(document_type: str, institutions: list[str], account_count: int) -> str:
    inst = ", ".join(institutions) if institutions else "unknown"
    return (
        f"A structural pass already identified this as a {document_type} "
        f"containing {account_count} account(s) across: {inst}. "
        "Use that as orientation, but trust the document over this summary."
    )
