"""Matching an enrichment document's accounts to the authoritative ones.

An enrichment source names its accounts differently from the aggregation that
reports them second-hand. Vanguard writes `19000011*`; the same account appears
inside the Fidelity export as
`"Pat Q Sample - Rollover IRA Brokerage Account - 19000011"`.

The rule is deliberately strict: **a match is used only when it is unique on
both sides.** Anything else is surfaced for a one-time human confirmation
rather than guessed, because a wrong match would overwrite one account's
holdings with another's.

The case that forces this: both 529 accounts share the base number 900000002
and are distinguished only by an `-01` / `-02` suffix. Matching on the base
alone would silently swap two children's college funds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A suffixed identifier (900000002-01) is more specific than a bare one and is
#: therefore preferred. Six digits is the shortest real account number observed.
_SUFFIXED = re.compile(r"\b(\d{6,})[-–](\d{1,3})\b")
_BARE = re.compile(r"\b(\d{6,})\b")

#: Sources mask their own identifiers in various ways; none of these carry
#: matchable information.
_MASK_CHARS = "*# \t–—"


def normalize_identifier(raw: str) -> str:
    """Strip the decoration a source applies to its own account numbers."""
    return (raw or "").strip().strip(_MASK_CHARS).strip()


def extract_identifiers(*texts: str) -> set[str]:
    """Every account-number-shaped token in the given text, suffixed and bare.

    Both forms are returned so that a suffixed enrichment key matches a
    suffixed authoritative name exactly, while a bare enrichment key against
    two suffixed names correctly produces an ambiguity rather than a match.
    """
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        cleaned = text.replace("–", "-").replace("—", "-")
        for base, suffix in _SUFFIXED.findall(cleaned):
            found.add(f"{base}-{suffix}")
            found.add(base)
        for base in _BARE.findall(cleaned):
            found.add(base)
    return found


@dataclass(frozen=True)
class AccountRef:
    """Either side of a candidate match."""

    key: str
    display_name: str

    def identifiers(self) -> set[str]:
        return extract_identifiers(self.display_name, self.key)


@dataclass(frozen=True)
class AccountMatch:
    authoritative: AccountRef
    enrichment: AccountRef
    identifier: str


@dataclass
class MatchReport:
    matched: list[AccountMatch] = field(default_factory=list)
    #: Enrichment accounts whose identifier resolves to more than one candidate.
    ambiguous: list[tuple[AccountRef, list[AccountRef]]] = field(default_factory=list)
    #: Enrichment accounts the authoritative source does not cover at all.
    #: Reported prominently -- ignoring one silently would hide real money.
    unmatched_enrichment: list[AccountRef] = field(default_factory=list)
    #: Authoritative accounts this document said nothing about; they keep their
    #: primary values and are flagged as not refreshed.
    unrefreshed: list[AccountRef] = field(default_factory=list)

    @property
    def needs_manual_mapping(self) -> bool:
        return bool(self.ambiguous)

    def to_json(self) -> dict[str, object]:
        return {
            "matched": [
                {
                    "authoritative": m.authoritative.display_name,
                    "enrichment": m.enrichment.key,
                    "identifier": m.identifier,
                }
                for m in self.matched
            ],
            "ambiguous": [
                {
                    "enrichment": ref.key,
                    "enrichment_name": ref.display_name,
                    "candidates": [c.display_name for c in candidates],
                }
                for ref, candidates in self.ambiguous
            ],
            "unmatched_enrichment": [
                {"key": r.key, "display_name": r.display_name} for r in self.unmatched_enrichment
            ],
            "unrefreshed": [r.display_name for r in self.unrefreshed],
        }


def match_accounts(
    authoritative: list[AccountRef],
    enrichment: list[AccountRef],
    confirmed: dict[str, str] | None = None,
) -> MatchReport:
    """Pair enrichment accounts with authoritative ones.

    `confirmed` maps an enrichment key to an authoritative key, recording a
    mapping a human has already approved. Those bypass the uniqueness rule --
    a person is allowed to resolve what the algorithm refuses to guess.
    """
    confirmed = confirmed or {}
    report = MatchReport()
    by_auth_key = {ref.key: ref for ref in authoritative}
    auth_identifiers = {ref.key: ref.identifiers() for ref in authoritative}

    claimed: set[str] = set()

    for enrichment_ref in enrichment:
        # A human-confirmed mapping wins outright.
        target_key = confirmed.get(enrichment_ref.key)
        if target_key and target_key in by_auth_key:
            report.matched.append(
                AccountMatch(by_auth_key[target_key], enrichment_ref, "user-confirmed")
            )
            claimed.add(target_key)
            continue

        identifier = normalize_identifier(enrichment_ref.key)
        candidate_ids = extract_identifiers(identifier, enrichment_ref.display_name)
        if identifier:
            candidate_ids.add(identifier)

        # Try the most specific identifiers first. A suffixed number
        # (900000002-01) distinguishes two accounts that share a base number,
        # so falling straight to the bare form would report a false ambiguity
        # for accounts that are in fact uniquely identified.
        tiers = [
            {i for i in candidate_ids if "-" in i},
            {i for i in candidate_ids if "-" not in i},
        ]

        matches: list[tuple[AccountRef, str]] = []
        for tier in tiers:
            if not tier:
                continue
            matches = [
                (auth_ref, max(shared, key=len))
                for auth_ref in authoritative
                if (shared := tier & auth_identifiers[auth_ref.key])
            ]
            if matches:
                break

        if len(matches) == 1:
            auth_ref, shared_id = matches[0]
            report.matched.append(AccountMatch(auth_ref, enrichment_ref, shared_id))
            claimed.add(auth_ref.key)
        elif len(matches) > 1:
            report.ambiguous.append((enrichment_ref, [m[0] for m in matches]))
        else:
            report.unmatched_enrichment.append(enrichment_ref)

    report.unrefreshed = [ref for ref in authoritative if ref.key not in claimed]
    return report
