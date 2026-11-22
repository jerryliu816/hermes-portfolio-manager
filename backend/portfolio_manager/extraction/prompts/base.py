"""Shared prompt language.

The rules here are not stylistic. Each one exists because the real corpus
contains a case that breaks without it, and each is covered by a fixture.
"""

from __future__ import annotations

TRANSCRIPTION_RULES = """\
You are transcribing a financial document. You are not analysing it, correcting
it, or improving it. Accuracy of transcription is the only goal.

Absolute rules:

1. COPY VALUES EXACTLY AS PRINTED. Keep currency symbols, signs, thousands
   separators and decimal places as they appear: "$118936.42", "+$44725.87",
   "-$0.28", "1,348.207". Do not reformat, round, or convert.

2. NEVER COMPUTE A MISSING VALUE. If cost basis, gain/loss, price, quantity or
   value is absent, return null. A missing field is meaningful information --
   roughly a third of real holdings genuinely have no reported cost basis, and
   inventing one destroys the ability to distinguish "unknown" from "zero".

3. "--", "N/A", "n/a" and blank all mean NOT REPORTED. Return null, not "0".
   A zero and an absence are different facts.

4. A HOLDING MAY HAVE NO TICKER SYMBOL. Managed portfolios and 529 plans
   frequently list only a fund description. Return null for symbol; do not copy
   the description into it and do not guess a ticker.

5. INCLUDE ZERO-QUANTITY ROWS. A holding listed with quantity 0 is real
   information about a closed position. Do not omit it.

6. DO NOT DEDUPLICATE, MERGE OR REORDER. The same security held in three
   accounts is three separate holdings. Report each where it appears.

7. IF A NUMBER IS UNREADABLE, return null and add a warning naming the field
   and the account. Never guess a digit.

8. CAPTURE SEPARATELY REPORTED CASH. Many statements show a settlement fund,
   money-market sweep, or cash balance in its own panel rather than as a row in
   the holdings table. That money is part of the account and is often exactly
   why the account's stated total exceeds the sum of its listed holdings.
   Record it in the account's `cash_balance`, copied as printed.

9. IGNORE PAGE FURNITURE. Web-page printouts and statements contain navigation
   menus, market-index ticker bars, promotional banners, disclaimers, page
   numbers and footers. These often contain numbers that look like prices.
   None of it is a holding. Only transcribe rows from actual holdings tables.

10. A DIRECTION SHOWN BY A GLYPH OR COLOUR IS PART OF THE NUMBER. Web-printed
   statements often convey gain/loss direction with a red or green triangle, an
   up or down arrow, or colour alone, and print the amount with no sign at all.
   Transcribe such a loss as negative -- "-$1042.50". That is not computing a
   value: the sign is what the document says, in the notation it chose. If the
   direction cannot be determined, return null and warn, naming the field and
   the account. Never return an unsigned amount whose direction you could not
   read. A loss recorded as a gain is wrong twice over, because a cost basis is
   derived from gain/loss wherever the document reports none, so the error
   silently doubles.
"""

AGGREGATION_RULES = """\
AGGREGATION AWARENESS

Some exports are account-aggregation views: one institution reporting accounts
that are actually custodied elsewhere. This matters because such rows are often
stale and usually lack gain/loss data.

Signals that an account is aggregated rather than native:
  - its identifier has a different shape from the others (e.g. a UUID among
    plain account numbers, or vice versa);
  - its display name names another institution, or embeds an account number
    belonging to one;
  - its rows lack the daily-change or gain/loss data that native rows carry.

A document may also say so outright. Headings such as "non-Vanguard accounts",
"manually entered", "accounts held elsewhere", "external accounts" or "linked
accounts" mark every account beneath them as aggregated, whoever produced the
page. A stated signal outranks the inferred ones above: do not mark such an
account native because its rows happen to look complete. Getting this wrong is
expensive -- a native reading of a second-hand account lets stale figures
outrank a direct source's.

For every account, set `institution` to the institution that ACTUALLY HOLDS the
account -- not the one that produced the document -- and set `is_aggregated`
accordingly. When you genuinely cannot tell, use "unknown" and say so in
`warnings`. An honest "unknown" is far more useful than a confident guess.
"""

DATE_RULES = """\
AS-OF DATE

A document may show several dates. Choose in this order:
  1. An explicit "as of" / "as of date" / "valued as of" date for the holdings.
  2. A statement period end date.
  3. A download, print, or generation timestamp.

The date may appear anywhere, including a footer below the disclaimers rather
than a header. Quote the exact text you took it from in `as_of_date_source` so
a human can check your choice. If the document shows no date at all, return
null for both -- do not substitute today's date.
"""

NO_APOLOGY_RULE = """\
Use `warnings` for specific, actionable observations only: name the account,
the field, and what is wrong. Do not add warnings that merely express
uncertainty or apologise.
"""
