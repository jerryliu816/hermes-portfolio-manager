"""Prompt versioning.

Every ExtractionRun records the prompt version that produced it, so a change in
extraction quality can always be attributed. Bump when wording changes in a way
that could alter output; the benchmark command compares versions.
"""

from __future__ import annotations

#: Stage 1 - document understanding and account discovery.
OVERVIEW_PROMPT_VERSION = "overview-1.3"

#: Stage 2 - position transcription.
POSITIONS_PROMPT_VERSION = "positions-1.3"

#: Bounded recovery pass after a reconciliation failure.
RECOVERY_PROMPT_VERSION = "recovery-1.2"

# 1.3 / 1.3 / 1.2: the example figures in the shared transcription rules and
# the position field descriptions were real holdings, and are now invented.
# The rules and the formats they illustrate are unchanged; the benchmark scored
# identically before and after.
