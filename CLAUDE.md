# Working on this project

A personal portfolio system of record. Read
`docs/FOR_FUTURE_SESSIONS.md` before making non-trivial changes — it carries
the measured facts about the real data that most of these decisions rest on.
`PROGRESS.md` is the phase-by-phase record.

Both files describe the owner's real portfolio, so they are **local-only**:
gitignored, never committed, absent from any clone. In a git worktree they are
absent too, because git does not copy ignored files. Read and update the copies
in the main checkout instead — its root is the parent of
`git rev-parse --path-format=absolute --git-common-dir`. Never create a new
`PROGRESS.md` inside a worktree; it is lost when the worktree is removed.

## The commitment that shapes everything

**Every figure travels with what qualifies it.** A total is never shown alone
when part of it is stale, unclassified, or missing a cost basis. Where a number
is unknown, the system says so rather than estimating one. If a change makes the
app more confident than its data, it is the wrong change.

## Invariants — do not break these

- **Money is `Decimal` in Python, `NUMERIC(20,6)` in Postgres, and `str` on
  both wires.** Never a float, never a JSON number. API money types need both a
  serializer *and* a `WithJsonSchema` override, or OpenAPI advertises `number`.
- **Never sum share counts across accounts.** The same symbol can be pre-split
  in one source and post-split in another. `SecurityExposure` has no
  `total_quantity` and must not gain one.
- **Price lives on the position, never on `Security`.**
- **`Security.symbol` is nullable.** Identity is symbol-or-normalized-name.
- **Absence is not zero.** `--`/``/`N/A` → `None`. An unparseable value raises.
- **Completeness is checked independently of arithmetic.** A dropped account
  leaves a self-consistent remainder; only the Stage 1 vs Stage 2 account-set
  comparison catches it.
- **`ingestion/normalizer.py` is the only place strings become `Decimal`.**
- **`anthropic` is imported in exactly one module.** A test asserts the mock
  path never loads it.
- **Hermes is read-only** apart from three advisory writes, enforced by the tool
  set, `SET TRANSACTION READ ONLY`, and a test.
- **Classification declines rather than guesses.** `unknown` is a valid,
  frequent, correct answer.

## Layout

```
backend/portfolio_manager/
  extraction/   provider boundary, canonical schema, prompts, benchmark
  ingestion/    deterministic: detector, normalizer, validator, reconciler, matcher
  services/     system of record: snapshot, precedence, policy, backup, importer
  analytics/    pure functions over a PortfolioView
  api/          53 REST endpoints        hermes/  24 MCP tools
  cli.py        everything, without a browser
frontend/       React 19 + Tailwind + TanStack Query + Recharts
tests/          572 tests; fixtures are anonymized derivatives of a private corpus
```

## Before claiming anything works

```bash
make test lint ui-check benchmark benchmark-check check-secrets
```

`test/` holds real financial data and is gitignored. Never commit it, never
paste its contents into a message, and regenerate fixtures with
`scripts/anonymize_fixtures.py` rather than by hand.

**Never put a real name, account number or balance into code, tests or docs** —
not even as an illustrative example. Beyond privacy, anything keyed to a real
balance goes stale the moment a new statement is imported; what a test depends
on should be fixed by the fixture, not by what the owner held that day.

### Every number in a test, example or comment is invented

This repository is public. The real portfolio is in `test/`, `data/`, the
running database, `PROGRESS.md` and `docs/FOR_FUTURE_SESSIONS.md` — all
local-only. Nothing from those places may be copied into a tracked file. That
covers more than balances:

- **No real figure of any kind:** account totals, position values, cost basis,
  gains and losses, share quantities, cash and settlement balances,
  money-market balances, portfolio totals, and percentages computed from them.
  A real number is still real with its commas removed, re-rounded, or used as an
  "e.g." in a prompt or docstring.
- **No rounded magnitudes of the real portfolio either:** not "the portfolio is
  about $NM" or "a $NK position" with the real N. Say "six figures", or use an
  invented round number. (The same goes for examples written into rules like
  this one: an example of what not to write is still written.)
- **No real account endings, even masked.** `****1234` style suffixes, the
  account key column and account names are identifiers. Use `X1`, `acct`,
  `Z10000077`-style obvious fakes.
- **Share prices are the one exception.** They are public market data, and the
  anonymized fixtures keep them on purpose. Quantities and values are not
  public; never pair a real price with a real quantity.

How to choose a number:

1. Make it up, with exact cents and non-round digits when the test is about
   parsing (`$48207.66`, not `$50000.00`).
2. Keep whatever arithmetic the test depends on — basis + gain = value,
   holdings + cash = total to the cent, quantity × price within the rounding
   drift being tested — and compute it, rather than eyeballing it.
3. Prefer the anonymized fixtures in `tests/fixtures/` when a test needs a
   realistic shape; regenerate them with `scripts/anonymize_fixtures.py`.
4. Never write "verified on the real data" next to invented numbers. Say the
   shape is real and the figures are invented, or drop the figure.

When you look at the real data to understand a bug, carry the *shape* into the
test (a symbol-less holding, a value with no cost basis, a shortfall exactly
equal to the cash), never the values.

`tests/integration/test_no_private_data_in_repo.py` enforces this. At run time
it reads every statement in `test/` and `data/sources/` — CSV exports and PDFs —
and fails if any tracked file, fixtures included, contains a real name, account
key, masked account ending, amount or share count from them. It reports
`path:line`, never the value. It skips only when there are no statements on the
machine at all, which includes every git worktree, because `data/` is ignored
and not copied into one. So a pass in a worktree means "unchecked": run it in
the main checkout before committing anything with numbers in it.

It cannot catch a rounded magnitude ("about $NM") or a figure from a statement
that was never imported. Those are on you.

## Conventions

- Every phase ends with a commit and a `PROGRESS.md` update.
- Prefer declining over guessing anywhere a wrong answer would look confident.
- When you patch by string replacement, verify the edit landed — `ruff format`
  reflows blocks and silently breaks matches.
- Render UI changes and look at them. Several real bugs here were invisible to
  the test suite and obvious on screen.
