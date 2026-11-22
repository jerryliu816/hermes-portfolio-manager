"""Recovery prompt: a bounded second look after reconciliation fails.

The single most important line here is the instruction NOT to alter correct
values to force agreement. A model asked to "make the numbers reconcile" will
happily edit a correctly transcribed figure, converting a detectable data
problem into an undetectable one.
"""

from __future__ import annotations

from .base import NO_APOLOGY_RULE, TRANSCRIPTION_RULES

RECOVERY_SYSTEM = f"""\
A previous transcription of this document failed arithmetic verification. Your
task is to re-read the document and determine what is actually printed.

{TRANSCRIPTION_RULES}

HOW TO APPROACH THE FAILURES

The failures listed below are checks of the form "quantity x price should equal
value" or "the holdings should sum to the stated account total". A failure has
exactly three possible causes:

  1. The previous pass misread a digit, transposed figures, missed a holding,
     or attributed one to the wrong account. Correct it.
  2. A holding was missed entirely, so the account total does not add up.
     Find it.
  3. THE DOCUMENT ITSELF IS INCONSISTENT. This is common and legitimate:
     brokerages round, aggregation feeds go stale, and a pre-split price can
     sit beside a post-split one. In that case the correct action is to
     transcribe what is printed and explain the discrepancy in `warnings`.

DO NOT ALTER A CORRECTLY TRANSCRIBED VALUE IN ORDER TO MAKE THE ARITHMETIC
AGREE. An unexplained mismatch that you report honestly is a useful finding
that a human will review. A mismatch you silently "fix" by editing a good
number is a corrupted financial record that nobody will ever catch.

If, after re-reading, you conclude the previous transcription was right and the
document is simply inconsistent, say exactly that in `warnings` and return the
same values.

{NO_APOLOGY_RULE}
"""


def recovery_user_prompt(filename: str, kind: str, failures: tuple[str, ...]) -> str:
    listed = "\n".join(f"  - {f}" for f in failures) or "  - (none recorded)"
    return (
        f"Re-read this {kind.upper()} document ({filename}).\n\n"
        f"These verification checks failed against the previous transcription:\n"
        f"{listed}\n\n"
        "Re-transcribe every account and holding. Where a listed failure reflects "
        "a genuine inconsistency in the document rather than a transcription "
        "error, keep the printed values and explain it in warnings."
    )
