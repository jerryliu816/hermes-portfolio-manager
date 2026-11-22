# Adding another financial institution

The short version: **usually nothing.** Extraction is a model reading a
document, not a parser keyed to a format, so a new institution's export often
works with no code at all. Try it first.

```bash
EXTRACTION_PROVIDER=anthropic portfolio import ~/Downloads/new-institution.csv
```

Then read the output carefully. What follows is what to check, and what to
change when the answer is not "nothing".

## What to check on a first import

**Did it find every account?** Compare the account count against the document.
If Stage 1 and Stage 2 disagree the import is marked incomplete automatically,
but a document where *both* stages miss the same account will look clean. This
is the one thing worth verifying by hand on a new institution.

**Did the arithmetic reconcile?** Failures print the document's own numbers
against what they add up to. A failure is often a finding about the document
rather than the extraction — brokerages round, and aggregation feeds go stale.

**Is the as-of date right?** Some documents put it in a footer below the
disclaimers; some show two dates and mean different things by them.

**Are the accounts classifiable?** Names like `IIA 6QQ-80000` carry no tax
signal, and the system will ask rather than guess.

## When something needs changing

### The account type is not recognised

`services/classification.py` maps names to types and tax treatments. Rules are
ordered most-specific-first, and the default is `unknown` rather than a guess.

Add a rule only when the name genuinely determines the answer. `Corporation` and
`Employer Equity Plan` deliberately yield a *type* but `unknown` tax treatment —
a corporate account is not "taxable" in the personal sense, and an equity plan
may hold RSUs, ESPP shares or options taxed differently. Being asked once beats
a wrong answer that looks confident.

### A holding lands in the "unknown" asset class

`services/security_classification.py` infers instrument type and asset class
from symbol and name.

Real exports abbreviate names past recognition — `INTERNATL`, `EMR MKT`,
`DIV APP`, `VANGUARD TREASURY...`. Two ways to fix a gap:

- Add the abbreviation to the relevant pattern, if it generalises.
- Add the ticker to `_SYMBOL_HINTS`, if the name is truncated past any signal.

Prefer declining over guessing. The allocation chart reports unclassified value
honestly; a wrong classification moves money between buckets in a chart you make
decisions from.

### The document uses a number format the normalizer does not know

`ingestion/normalizer.py` is the only place strings become `Decimal`. It already
handles both formats seen in practice — `$118936.42` with no separators and
`$1,234.56` with them — plus signed values and accounting negatives.

Add a case there, with a test using the actual string from the document. Never
add parsing anywhere else; a second parser is how two parts of the system come
to disagree about what a number means.

### The document has a structural quirk

BOM, CRLF, a ragged trailing comma, disclaimers after the data, a date only in a
footer — all handled in `ingestion/detector.py`, deterministically and before
the model sees the file. That keeps the model's attention on the part that needs
judgement.

## The hard case: two sources describing the same account

This is where most of the design effort went, and it is worth understanding
before adding an aggregating institution.

An aggregator will report accounts held elsewhere. If you also import a
statement from the institution that actually holds them, you have two readings
of the same account — and they can disagree about **what the account contains**,
not merely what it is worth.

Measured on the corpus this was built against: the two sources agreed on their
*totals* to within a few hundred dollars, and disagreed on **35 of 44 securities**. An entire
six-figure holding was present in one and absent from the other. The aggregation had
missed a rebalance.

The first implementation refreshed prices onto the aggregated holdings. It moved
the portfolio by **$420,854** — far more than price staleness can justify — and
produced a portfolio of mixed vintage that *looked* current. That is worse than
an openly stale one.

The portfolio's owner later confirmed which source was right: the direct one.
The aggregation was carrying holdings that had been sold and missing ones that
had been bought — **stale composition, not just stale prices**. So an aggregated
position count is not evidence of anything, and precedence must replace an
account wholesale rather than reconciling row by row.

So the rule is **precedence, not patching**:

- For each account, one source wins outright and supplies it **wholesale**.
- A native source beats an aggregation automatically — an institution reporting
  its own account is the better witness.
- Two comparable sources are put to the user once, and the answer is remembered
  on `Account.preferred_source`.
- Matching is on the account identifier embedded in the name, and **only when it
  resolves uniquely on both sides.** In the corpus two 529 accounts shared a base
  number and differed only by an `-01` / `-02` suffix; matching on the base would
  have swapped two children's college funds.

### If your institution's identifiers do not match

`ingestion/matcher.py` extracts account-number-shaped tokens and tiers them by
specificity. If a source masks its identifiers entirely (one account in the
corpus is literally `**********rust`), no automatic match is possible and the
system asks for a one-time mapping stored in `AccountMapping`. That is the right
outcome — guessing here overwrites one account's holdings with another's.

### If the source reports cash outside the holdings table

`ExtractedAccount.cash_balance` exists for exactly this. On the corpus, three
brokerage accounts each had a settlement balance the holdings table omitted —
six figures in total, which the aggregating institution dropped entirely. The
reconciler treats a shortfall as cash **only** when a stated balance explains it,
so a genuinely missing holding is never excused as "probably cash".

## Before you trust it

Add a representative document to `tests/fixtures/benchmark/`, generate its
expected reading, review it, and commit both. See
[Adding a provider](ADDING_A_PROVIDER.md#extending-the-corpus-first).

If the document contains real account data, run it through
`scripts/anonymize_fixtures.py` first — the corpus is committed and the real
files are not.
