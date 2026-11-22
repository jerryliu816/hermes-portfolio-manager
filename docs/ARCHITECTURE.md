# Architecture

## The shape of the problem

Brokerage exports are not a clean interface. They change format without notice,
abbreviate fund names past the point of recognition, use `--` for six different
kinds of absence, and — most consequentially — one institution will happily
report accounts held at another, second-hand and out of date, in the same file
as its own.

So the system is built around two separations:

1. **Reading a document is separated from being a system of record.** Extraction
   is a judgement call made by a model. The portfolio is not. Everything the
   model produces passes through deterministic validation, normalisation and
   arithmetic before it becomes state.
2. **Being a system of record is separated from giving advice.** The portfolio
   answers "what is true?". Hermes answers "what should I do?". The second is
   optional, read-only, and cannot alter the first.

## Layers

```
  documents  ─────────────────────────────────────────────────────────┐
                                                                      │
  ┌────────────────────────────────────────────────────────────────┐  │
  │  extraction/          "what does this document say?"           │  │
  │    contracts.py       vendor-neutral boundary (a Protocol)     │  │
  │    canonical.py       the wire schema — money as STRINGS       │  │
  │    prompts/           versioned; every rule earned from data   │  │
  │    providers/         anthropic · mock · local (stub)          │  │
  │    benchmark.py       the gate for replacing a provider        │  │
  └────────────────────────────────────────────────────────────────┘  │
                                │ ExtractedPortfolioDocument          │
                                ▼                                     │
  ┌────────────────────────────────────────────────────────────────┐  │
  │  ingestion/           deterministic; no model influence         │ │
  │    detector.py        kind detection, CSV pre-clean             │ │
  │    normalizer.py      strings → Decimal / date; the ONLY place  │ │
  │    validator.py       completeness invariant, zero-value skip   │ │
  │    reconciler.py      pure arithmetic, capped tolerances        │ │
  │    matcher.py         account identity across sources           │ │
  └────────────────────────────────────────────────────────────────┘  │
                                ▼                                     │
  ┌────────────────────────────────────────────────────────────────┐  │
  │  services/            the system of record                      │ │
  │    source_file · snapshot · precedence · multi_source           │ │
  │    classification · security_classification · policy            │ │
  │    recommendation · backup · importer                           │ │
  │    philosophy · advisor_context · proposals · hermes_client     │ │
  └────────────────────────────────────────────────────────────────┘  │
                                ▼                                     │
  ┌───────────────────────────┐   ┌────────────────────────────────┐  │
  │  analytics/  pure funcs   │   │  models/   SQLAlchemy + Alembic│◄─┘
  │  over a PortfolioView     │   │  Postgres 17, NUMERIC(20,6)    │
  └───────────────────────────┘   └────────────────────────────────┘
                 ▼                              ▼
  ┌───────────────────────────┐   ┌────────────────────────────────┐
  │  api/  53 REST endpoints  │   │  hermes/  24 MCP tools         │
  │  money as strings         │   │  read-only + 3 advisory writes │
  └───────────────────────────┘   └────────────────────────────────┘
                 ▼
  ┌────────────────────────────────────────────────────────────────┐
  │  frontend/   React 19 · Tailwind · TanStack Query · Recharts    │
  │  served by FastAPI from the same origin in production          │
  └────────────────────────────────────────────────────────────────┘
```

`cli.py` sits beside the API and reaches the same services. Everything the app
does is available without a browser, because reconciliation output is far easier
to read as text and a system of record you can only query through a UI cannot be
scripted or checked from cron.

## The pipeline, end to end

```
file → sha256 → duplicate check → detect kind
  → deterministic pre-clean      BOM, CRLF, ragged trailing comma, footers
  → Stage 1  document understanding: institutions, as-of date, ACCOUNT SET
  → Stage 2  position extraction, chunked per account above a threshold
  → completeness invariant       Stage 1 account set == Stage 2 account set
  → normalize                    "--" → None; "$1,234.56" → Decimal
  → skip accounts reporting no value at all, recording each skip
  → validate + reconcile         capped tolerances; closed positions exempt
  → write snapshot               approved, or needs_review — never discarded
```

A second document describing some of the same accounts goes through
**precedence** rather than merging: for each account one source wins outright
and supplies it wholesale. See [Adding an institution](ADDING_AN_INSTITUTION.md)
for why patching was tried first and abandoned.

## The advisor

```
  browser ──► api/v1/advisor_chat ──SSE relay──► Hermes api_server
                │  records simulate_trades args       │ web search, skills
                ▼                                     ▼
  services/proposals ◄────── MCP tools ───────── hermes/mcp_server
                │  the one translation of the engine to JSON
                ▼
  analytics/rebalance   target_gaps · simulate · suggest_rebalance
```

Hermes holds the conversation and chooses securities. `analytics/rebalance.py`
produces every figure. `services/proposals.py` is the only translation between
the two, and the MCP tools, the proposal panel and the saved journal entry all
use it. See `docs/HERMES_INTEGRATION.md`.

## Decisions that shape everything else

### Money is never a float, anywhere

`NUMERIC(20,6)` in Postgres, `Decimal` in Python, **`str` on both wires** — the
extraction schema and the REST API. A JSON number is a float in most clients; in
JavaScript `0.1 + 0.2 !== 0.3`. The frontend formats from the string and does no
arithmetic on it.

There is a second reason on the extraction side: a Pydantic `Decimal` field
emits `anyOf: [number, string]` in its JSON Schema, which strict
structured-output generation does not accept.

Two tests enforce this: one walks every API response for a numeric money field,
another walks the OpenAPI schema. The schema check matters independently —
without an explicit `WithJsonSchema` override, Pydantic derives the schema from
the *input* type and publishes `number`, so a generated client would parse a
float even though the wire carries a string.

### Price belongs to the position, not the security

The same symbol legitimately carries two different prices in one document when
one source is stale or pre-split. `Security` therefore has **no price column**,
and **share counts are never summed across accounts** — `SecurityExposure` has
no `total_quantity` field at all. Values add; quantities are reported per
account.

### Symbol is not the security key

12 of 86 holdings in the source corpus had no ticker. `Security.symbol` is
nullable; identity is the symbol when present, else a normalized description.

### Completeness is checked independently of arithmetic

Stage 1's account set is compared against Stage 2's. A mismatch forces review no
matter how cleanly the surviving numbers reconcile — which they will, because
dropping an account leaves a perfectly self-consistent remainder. No other check
can substitute for this one.

### Absence is not zero

`--`, empty and `N/A` become `None`, never `Decimal("0")`. An unparseable value
raises rather than defaulting. Roughly a third of real holdings report no cost
basis, and imputing one destroys the ability to say so.

### Enum columns are `VARCHAR` + `CHECK`

Native Postgres enums do not autogenerate reliably under Alembic and have
downgrades that cannot be expressed. The database carries 10 CHECK constraints
and zero enum types.

### As-of date is per account

An aggregation feed reports accounts it synced at some earlier, unstated time.
The file's own date says nothing about them, so `AccountSnapshot` carries its
own `as_of_date`, `is_stale` and `staleness_reason`.

### Cash reported outside the holdings table is still money

`AccountSnapshot.cash_balance` holds settlement balances a source reports
separately. On the source corpus this was six figures across three accounts — real
money the aggregation feed omitted entirely. It is recorded rather than
fabricated as a position, because the document did not report it as one.

## Testing shape

| Layer | How it is tested |
|---|---|
| Pure functions (normalizer, reconciler, analytics, policy) | Unit tests with hand-built inputs |
| Services, API, Hermes | Integration tests against a real Postgres (`portfolio_test`, created and dropped per session) |
| Extraction providers | Scored against a corpus with known-good readings; a deliberately damaged provider proves the scorer works |
| The UI | 13 Playwright tests against a real browser, API and database |

Fixtures are synthetic derivatives of the private corpus, preserving every
structural quirk while carrying none of the content. See
[`scripts/anonymize_fixtures.py`](../scripts/anonymize_fixtures.py).
