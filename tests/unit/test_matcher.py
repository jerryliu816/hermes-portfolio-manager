"""Matching enrichment accounts to authoritative ones.

The strictness here is the whole point: a wrong match would overwrite one
account's holdings with another's. The case that forces it is real -- two 529
accounts sharing the base number 900000002, distinguished only by an -01/-02
suffix. Matching on the base alone would swap two children's college funds.
"""

from __future__ import annotations

from portfolio_manager.ingestion.matcher import (
    AccountRef,
    extract_identifiers,
    match_accounts,
    normalize_identifier,
)

SAM = AccountRef(
    "e5c74c20", "Sam Sample-Individual 529 College Savings Account, Pat Q Sample-900000002-01"
)
QUINN = AccountRef(
    "1b76a428",
    "Quinn Sample-Individual 529 College Savings Account, Pat Q Sample-900000002-02",
)
ROLLOVER = AccountRef("20c08095", "Pat Q Sample - Rollover IRA Brokerage Account - 19000011")
JOINT = AccountRef("Z10000077", "Joint WROS - TOD")
MASKED = AccountRef("56b2c4bf", "**********rust")

REAL_AUTHORITATIVE = [SAM, QUINN, ROLLOVER, JOINT, MASKED]


def test_source_masking_is_stripped_from_identifiers() -> None:
    assert normalize_identifier("19000011*") == "19000011"
    assert normalize_identifier("  82000033#  ") == "82000033"


def test_identifiers_are_extracted_in_both_specific_and_bare_forms() -> None:
    found = extract_identifiers("Pat Q Sample-900000002-01")
    assert "900000002-01" in found
    assert "900000002" in found


def test_short_numbers_are_not_treated_as_account_ids() -> None:
    """'529' and '401' must not become match keys."""
    assert extract_identifiers("Individual 529 College Savings") == set()


def test_a_suffixed_identifier_matches_the_right_child() -> None:
    """The case that motivates the whole tiering rule."""
    report = match_accounts(
        REAL_AUTHORITATIVE,
        [
            AccountRef("900000002-01", "Sam Sample 529"),
            AccountRef("900000002-02", "Quinn Sample 529"),
        ],
    )
    assert not report.ambiguous
    pairs = {m.enrichment.key: m.authoritative.key for m in report.matched}
    assert pairs == {"900000002-01": SAM.key, "900000002-02": QUINN.key}


def test_a_bare_shared_identifier_is_ambiguous_not_guessed() -> None:
    """Without the suffix the two 529s are genuinely indistinguishable."""
    report = match_accounts(REAL_AUTHORITATIVE, [AccountRef("900000002", "a 529")])
    assert not report.matched
    assert len(report.ambiguous) == 1
    enrichment_ref, candidates = report.ambiguous[0]
    assert enrichment_ref.key == "900000002"
    assert {c.key for c in candidates} == {SAM.key, QUINN.key}
    assert report.needs_manual_mapping


def test_masked_enrichment_key_matches_the_embedded_number() -> None:
    report = match_accounts(REAL_AUTHORITATIVE, [AccountRef("19000011*", "")])
    assert [m.authoritative.key for m in report.matched] == [ROLLOVER.key]


def test_an_account_only_the_enrichment_knows_about_is_reported() -> None:
    """Silently ignoring it would hide real money."""
    stranger = AccountRef("99999999", "Some Other Brokerage")
    report = match_accounts(REAL_AUTHORITATIVE, [stranger])
    assert not report.matched
    assert [r.key for r in report.unmatched_enrichment] == ["99999999"]
    payload = report.to_json()
    assert payload["unmatched_enrichment"][0]["key"] == "99999999"  # type: ignore[index]


def test_accounts_the_document_says_nothing_about_are_flagged_unrefreshed() -> None:
    report = match_accounts(REAL_AUTHORITATIVE, [AccountRef("19000011*", "")])
    unrefreshed = {r.key for r in report.unrefreshed}
    assert SAM.key in unrefreshed
    assert JOINT.key in unrefreshed
    assert ROLLOVER.key not in unrefreshed


def test_an_account_with_no_identifier_never_matches() -> None:
    """'**********rust' is masked by the source; it needs manual mapping."""
    report = match_accounts([MASKED], [AccountRef("12345678", "Treasury Bills")])
    assert not report.matched
    assert report.unmatched_enrichment


def test_a_user_confirmed_mapping_overrides_the_uniqueness_rule() -> None:
    """A person may resolve what the algorithm rightly refuses to guess."""
    report = match_accounts(
        REAL_AUTHORITATIVE,
        [AccountRef("900000002", "a 529")],
        confirmed={"900000002": SAM.key},
    )
    assert not report.ambiguous
    assert [m.authoritative.key for m in report.matched] == [SAM.key]
    assert report.matched[0].identifier == "user-confirmed"


def test_all_five_real_vanguard_accounts_match_correctly() -> None:
    """End-to-end against the actual account names from both documents."""
    authoritative = REAL_AUTHORITATIVE + [
        AccountRef("c1d2e3f4", "Pat Q Sample, Alex Rivera - Brokerage Account - 77000022"),
        AccountRef("e11cdc2c", "Pat Q Sample, Robin Sample - Brokerage Account - 82000033"),
    ]
    enrichment = [
        AccountRef("19000011*", "Rollover IRA Brokerage Account"),
        AccountRef("77000022*", "Brokerage Account"),
        AccountRef("900000002-02", "Quinn Sample 529"),
        AccountRef("900000002-01", "Sam Sample 529"),
        AccountRef("82000033*", "Brokerage Account"),
    ]
    report = match_accounts(authoritative, enrichment)
    assert len(report.matched) == 5
    assert not report.ambiguous
    assert not report.unmatched_enrichment
    # Joint WROS and the masked account are Fidelity-only; they stay primary.
    assert {r.key for r in report.unrefreshed} == {JOINT.key, MASKED.key}
