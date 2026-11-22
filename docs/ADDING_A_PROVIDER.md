# Running a different extraction model

The whole architecture exists so this is a contained change. `anthropic` is
imported in exactly one module, and a test asserts that selecting the mock
provider never loads the vendor SDK.

## What you have to implement

The contract is `DocumentExtractionProvider` in
[`extraction/contracts.py`](../backend/portfolio_manager/extraction/contracts.py) —
a `Protocol`, so there is no base class to inherit:

```python
name           -> str
capabilities() -> ProviderCapabilities
health()       -> ProviderHealth          # never raises; reports a verdict
understand_document(document, context) -> OverviewResult    # Stage 1
extract_positions(document, overview, context) -> ExtractionResult  # Stage 2
```

Three rules the boundary depends on:

1. **No vendor type may cross it.** Not in a return value, not in an exception.
   A provider that leaks one makes the next swap a rewrite instead of a
   config change.
2. **Raise only `ExtractionError`**, with an `ExtractionErrorKind` and an honest
   `retryable` flag. The pipeline uses that to decide whether retrying helps.
3. **Return money as strings, exactly as printed.** `"$118936.42"`,
   `"+$44725.87"`, `"1,348.207"`. Do not normalise, and do not compute a value
   the document does not state. Absence is information.

## Writing a local-model provider

`providers/local_llm_provider.py` is a complete interface stub that raises on
every method — deliberately, because a half-working extraction provider is worse
than an absent one.

Filling it in:

```python
class LocalLLMExtractionProvider:
    @property
    def name(self) -> str:
        return "local"

    def understand_document(self, document, context) -> OverviewResult:
        response = self._client.chat(
            system=OVERVIEW_SYSTEM,  # reuse the prompts
            user=overview_user_prompt(document.filename, document.kind.value),
            schema=DocumentOverview.model_json_schema(),  # if the server supports it
        )
        return OverviewResult(
            overview=DocumentOverview.model_validate(response),
            provider=self.name,
            model=self._model,
            prompt_version=OVERVIEW_PROMPT_VERSION,
            latency_ms=elapsed,
        )
```

Reuse the prompts in `extraction/prompts/`. Every rule in them was earned from a
real document, and a local model needs them at least as much.

If the server cannot constrain output to a schema, ask for JSON and validate
with `model_validate`, mapping a `ValidationError` to
`ExtractionErrorKind.INVALID_OUTPUT` with `retryable=True`. Do not repair
malformed output by hand — a provider that guesses at what the model meant is
a second extraction layer with no tests.

Then register it in `extraction/registry.py` and set `EXTRACTION_PROVIDER=local`.

## Deciding whether it is good enough

**Do not decide this by looking at output.** A provider that gets 95% of
positions right looks excellent and is unusable, because the missing 5% is a
portfolio that silently does not add up.

```bash
portfolio extraction benchmark --provider local --verbose
```

The corpus is `tests/fixtures/benchmark/`, and each document has a known-good
reading beside it produced by a deterministic parse — not by a model, so the
baseline is not itself an opinion.

Scoring is unforgiving on purpose:

- A document passes only if **every** account and position is found **and**
  every field matches. There is no partial credit.
- Field accuracy is always reported beside position recall. Three perfect
  positions out of eighty would otherwise read as 100%.
- Money is compared by value, not spelling, so `"$1,000.00"` and `"1000"` are
  equal — normalising is the normalizer's job.
- Missing and invented accounts are named individually, because a wrong number
  shows up in reconciliation and a missing account does not.

For reference, Claude Opus 5 scores 7/7 with 100% field accuracy on this corpus,
at roughly $0.25 and an 89% prompt-cache hit rate.

**A provider is acceptable only at 7/7.** A provider failing one document in
seven fails one import in seven, and an import that is wrong without saying so
is worse than one that refuses.

## Extending the corpus first

Before trusting a new provider, add documents that exercise what *your*
institutions do. Put the document in `tests/fixtures/benchmark/`, then:

```bash
python scripts/generate_expected.py        # writes the baseline
python scripts/generate_expected.py --check  # fails if any baseline is stale
```

Review the generated baseline before committing it. It is a claim about what
the document says, and everything downstream is measured against it.

The baseline generator uses the mock provider's deterministic CSV parse. For a
PDF, write the expected reading by hand — slow, but a benchmark whose baseline
came from the thing being benchmarked measures nothing.

## Practical notes from running this against Claude

- **PDFs need a vision-capable model.** The sample statement in this project is
  a browser print of a web page, complete with navigation, a market-index ticker
  bar full of numbers, and promotional copy. The prompt tells the model to
  ignore page furniture; a weaker model will need that instruction to work.
- **Prompt-cache the stable prefix.** The system prompt and schema are identical
  on every import; caching them took the hit rate to 89%.
- **Budget generously for `max_tokens` and stream.** A 14-account document
  produces a large structured response, and truncation silently drops accounts.
  The provider treats `stop_reason == "max_tokens"` as a retryable error rather
  than parsing a truncated answer.
- **Check the stop reason before reading content.** On a refusal the content
  blocks are not the extraction you asked for.
