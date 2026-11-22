# GenAI extraction

Named for what it is: extraction by a language model, of which Claude is
currently the only working provider. See
[Adding a provider](ADDING_A_PROVIDER.md) to run a different one.

## Why a model rather than a parser

Brokerage exports change without notice. The sample PDF in this project is a
browser print of a web page — its layout will change whenever the site is
redesigned. A per-institution parser is a maintenance commitment that comes due
at the worst moment, silently.

The trade is that a model's output cannot be trusted directly. Everything it
produces passes through deterministic validation, normalisation and arithmetic
before it becomes state, and the model has **no influence** over any of that.

## The two stages

**Stage 1 — document understanding.** What the document is, when it is from, and
which accounts it contains. Positions are not read.

**Stage 2 — position transcription.** Holdings for the named accounts, chunked
per account above `EXTRACTION_ACCOUNTS_PER_CALL` (default 5) so a single
response cannot truncate.

Stage 1's account set is then compared against Stage 2's. **A mismatch forces
review regardless of arithmetic** — dropping an account leaves a perfectly
self-consistent remainder, so nothing else detects it.

## The canonical schema

`extraction/canonical.py`. Every monetary and quantity field is a **string**,
unnormalised — `"$118936.42"`, `"+$44725.87"`, `"1,348.207"` arrive as printed.

Two reasons, both load-bearing: a Pydantic `Decimal` emits an `anyOf` that
strict structured-output generation rejects, and a string preserves the model's
output byte-for-byte until the normalizer converts it, so transport cannot
introduce a rounding error.

`SCHEMA_VERSION` is recorded on every run. It is currently `1.1` — `1.0` did not
have `cash_balance`, and adding it recovered six figures of cash the pipeline was dropping.

## The prompts

`extraction/prompts/`, versioned so a change in quality is attributable. Every
rule exists because a real document breaks without it:

- **Copy values exactly as printed.** No reformatting or rounding.
- **Never compute a missing value.** A third of real holdings report no cost
  basis; inventing one destroys the difference between unknown and zero.
- **`--`, `N/A` and blank mean not reported.** Return null, not `"0"`.
- **A holding may have no ticker.** Return null; do not copy the description or
  guess.
- **Include zero-quantity rows.** A closed position is information.
- **Capture cash reported outside the holdings table.**
- **Ignore page furniture** — navigation, ticker bars, promotional banners,
  disclaimers. Web-page printouts are full of numbers that are not holdings.
- **Institution is per account.** An account named `Vanguard Brokerage - …`
  inside a Fidelity export is custodied at Vanguard. Say `unknown` when unsure.

The recovery prompt carries the single most important sentence in the set:
**"DO NOT ALTER A CORRECTLY TRANSCRIBED VALUE IN ORDER TO MAKE THE ARITHMETIC
AGREE."** A model asked to make numbers reconcile will edit a good figure,
turning a detectable data problem into an undetectable one.

## Reconciliation

`ingestion/reconciler.py` — pure functions over `Decimal`, no model involvement:

- `quantity × price ≈ market_value`, skipped for closed positions and absent fields
- `cost_basis + unrealized ≈ market_value`, skipped when a term is absent
- `Σ positions ≈ reported_account_value`, **passing only when a shortfall matches
  a stated cash balance** — a missing holding is never excused as "probably cash"
- `Σ accounts ≈ reported total`

Tolerance is `min(max(abs, pct × value), cap)` with a **$25 cap**. Calibrated,
not guessed: across the 68 real rows carrying quantity, price and value, only 10
matched exactly and the worst drift was **$0.23 on a six-figure position**. An
exact-equality check would have rejected good data on 58 of 68.

## Anthropic provider specifics

Verified against the installed SDK, not recalled:

- `client.beta.messages.parse(..., output_format=<PydanticModel>)` →
  `.parsed_output`. The **beta** path, because only it accepts `betas` and
  `fallbacks`.
- `thinking={"type": "adaptive"}` — `budget_tokens` is a 400 on Opus 5.
- Server-side refusal fallback is enabled, so a classifier declining a document
  full of names and account numbers reroutes instead of failing an import.
- The system prompt is marked cacheable; it is identical on every import.
  Measured hit rate: **89.2%**.
- A PDF document block must precede the text block.
- Check `stop_reason` before reading content: on a refusal the blocks are not
  the extraction you asked for, and `max_tokens` means truncation.

## The benchmark

`portfolio extraction benchmark` is the objective gate for replacing a provider.
See [Adding a provider](ADDING_A_PROVIDER.md#deciding-whether-it-is-good-enough).
Claude Opus 5 currently scores **7/7 with 100% field accuracy** at roughly $0.25.
