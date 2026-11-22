# Data model

12 tables on Postgres 17. Money is `NUMERIC(20,6)` throughout — six decimal
places because real quantities carry three (shaped like `318.457`) and money-market balances
carry two on large numbers.

Enum-valued columns are `VARCHAR` with a `CHECK` constraint rather than native
Postgres enums: `sa.Enum` does not autogenerate `CREATE TYPE` / `ALTER TYPE`
reliably under Alembic, and its downgrades cannot be expressed. The database
carries **10 CHECK constraints and zero enum types**.

## Portfolio truth

### `source_file`
An imported document. `sha256` is **unique** — re-importing an identical file is
a no-op. `role` is `authoritative` or `enrichment`. `stored_path` points at
`data/sources/<sha[:2]>/<sha>`, where the original bytes are kept forever: they
are the only way to re-derive a reading if extraction improves or a number is
disputed.

### `extraction_run`
One attempt at reading a document. Records provider, model, `prompt_version`,
`schema_version`, `attempt_no`, warnings, the reconciliation result, dropped
page indices, latency and token usage. **Re-extraction inserts a new row and
flips the previous to `is_active=false`** — never overwrites, so "why did this
number change?" stays answerable.

### `account`
Unique on `(institution, external_account_key)`. Institution is per account, not
per file. `is_aggregated` marks an account reported second-hand.
`external_account_key_masked` is what leaves the server — the raw key never
does. `preferred_source` records which institution's documents win for this
account once decided.

### `security`
Unique on `normalized_key`. Carries **no price** and a **nullable symbol**.
`security_type` and `asset_class` come from a deterministic classifier that
declines rather than guessing.

### `portfolio_snapshot`
Portfolio state at one point in time. `completeness` is
`complete`/`partial`/`unknown`. `skipped_accounts_json` records every account
omitted and why. `unmatched_enrichment_json` records accounts a second document
reported that the portfolio does not cover. `superseded_by` points at a
correcting snapshot for the same period.

### `account_snapshot`
One account within a snapshot. Unique on `(portfolio_snapshot_id, account_id)`.

Carries its **own** `as_of_date`, `as_of_confidence`, `is_stale` and
`staleness_reason`, because an aggregation feed reports accounts it synced at
some earlier, unstated time and the file's date says nothing about them.

`cash_balance` holds settlement or money-market cash the source reports outside
its holdings table. `computed_value` is the sum of positions; `total_value` adds
the cash.

### `position_snapshot`
**Identity is `(portfolio_snapshot_id, account_id, security_id)`, uniquely
constrained.** One security held in three accounts is one `security` row and
three `position_snapshot` rows.

The measurement tuple — `quantity`, `price`, `market_value`, `cost_basis`,
`unrealized_gain_loss` — is replaced atomically or not at all when a second
source supplies it. `cost_basis_source` is `reported`, `derived` or `unknown`,
and `unknown` is displayed honestly rather than imputed. `is_closed` marks a
zero-quantity holding, excluded from allocation denominators and exempt from
`quantity × price` reconciliation. `value_source` and
`enriched_from_source_file_id` record provenance.

### `account_mapping`
A user-confirmed link between an account and how another source names it.
Needed because embedded identifiers are not always unique — two 529 accounts in
the source corpus shared a base number and differed only by suffix.

### `security_alias`
Links two names for the same instrument across sources, for holdings with no
ticker whose descriptions differ.

### `account_audit_log`
Every user override of classification: field, old value, new value, who, why.

## Advisory — deliberately not portfolio truth

These are the only tables Hermes can write to.

### `investment_policy`
Versioned. `document_json` is what the system reads; `raw_yaml` is what the user
wrote, comments and ordering intact. Saving inserts a new version and retires
the previous one.

### `recommendation` / `recommendation_decision`
A recommendation and the decisions made about it. Decisions **append** — a
recommendation deferred in March and accepted in June keeps both facts, and
status is derived from the latest rather than stored.

## Migrations

Three revisions. `alembic upgrade head` runs in the container entrypoint before
the server starts, so there is no separate step to forget, and it is idempotent.

A second `--autogenerate` must produce an empty diff; if it does not, the models
and the migration have drifted.
