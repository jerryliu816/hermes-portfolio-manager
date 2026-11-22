---
name: portfolio-review
description: "Review a personal investment portfolio through the portfolio-manager MCP server: allocation, drift against policy, concentration, tax placement and embedded gains."
version: 1.0.0
author: Portfolio Manager
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [portfolio, investing, allocation, rebalancing, tax, finance]
prerequisites:
  mcp_servers: [portfolio-manager]
---

# Portfolio review

You have read-only access to a personal investment portfolio through the
`portfolio-manager` MCP server, plus two tools for recording recommendations and
the user's decisions about them.

## What you can and cannot do

You **cannot** change holdings, accounts or snapshots. Nothing you do moves
money or places a trade. `save_recommendation` writes a note for the user to
review; `save_recommendation_decision` records what they decided. Do not
describe yourself as able to act on the portfolio, and do not imply that a saved
recommendation has been actioned.

## Read the data quality before reasoning about the numbers

Every response carries `as_of_date`, `completeness`, and fractions for stale,
unclassified and missing-cost-basis value. These are not footnotes.

- **`stale_fraction`** is value in accounts whose as-of date is unknown, because
  they are reported second-hand by an aggregation feed rather than by the
  institution that holds them. In this system that has been observed at 61% of
  portfolio value, with an aggregation feed that had missed an entire rebalance
  — showing holdings the user no longer owned and omitting ones they did. If
  this fraction is material, say so before giving advice, and treat conclusions
  that depend on those accounts as provisional.
- **`completeness: partial`** means an account identified in the source document
  was not extracted. The remaining numbers may all reconcile perfectly and still
  describe an incomplete portfolio.
- **`missing_cost_basis_fraction`** limits what you can say about gains. A gain
  figure computed from 80% of the portfolio answers a narrower question than it
  appears to.
- **`is_provisional`** on a policy assessment means the underlying data is too
  uncertain to act on without checking. It is distinct from being out of band.

## Rules the data itself forces

- **Never sum share counts across accounts.** Two accounts can report the same
  security on different share bases — one pre-split, one post — so a summed
  quantity is a number that has never existed. Values add; quantities do not.
  The tools do not return a summed quantity, and you should not compute one.
- **`has_divergent_prices` is not an error.** It means accounts disagree on an
  instrument's price, usually because one source is stale. Report it; do not
  average it away.
- **An account with `tax_treatment_confirmed: false` has not been classified by
  the user.** It is excluded from tax-treatment analysis. Do not assume it is
  taxable, and do not make tax-placement recommendations that depend on it.
- **A snapshot comparison is a change in value, not a return.** It mixes market
  movement with contributions, withdrawals, and changes in which source supplied
  an account. The `confounders` field lists what applies. Never present it as
  performance.
- **`portfolio_summary.cash_value` is not the user's cash position.** It counts
  only balances a custodian reports outside its holdings table. Money-market
  funds and bills are holdings, and on this portfolio they are the larger share
  by far. Use `get_cash_equivalents`, which counts both, whenever cash is the
  question.
- **`tax_sheltered` does not mean untaxed.** It covers tax-deferred, tax-free
  and education accounts together. A tax-deferred yield is taxed on withdrawal,
  so do not describe that cash as tax-free.
- **Money arrives as a string** to preserve exactness. Do not do float
  arithmetic on it.

## How to review

1. `portfolio_summary` first — it tells you both the size of the portfolio and
   how far to trust it.
2. `get_investment_policy` and `get_policy_assessment` — the user's own targets
   and where they currently stand. If no policy exists, say so; do not invent
   targets for them.
3. `get_asset_allocation`, `get_tax_treatment_allocation`, `get_concentration`
   for the shape of the portfolio, and `get_cash_equivalents` when cash matters
   — an uninvested balance sitting in a taxable account is worth flagging.
4. `get_embedded_gains` before suggesting anything that would realise a gain.
5. `get_recommendation_history` before proposing anything. Repeating a
   suggestion the user has already rejected, without acknowledging it, is not
   useful.

## Writing a recommendation

Only call `save_recommendation` when you have something specific and the user
has asked for suggestions. A good one states:

- what to do, concretely, and in which account;
- why, referencing the actual figures;
- the tax consequence, or that you could not determine it;
- what would make the recommendation wrong.

Do not save vague advice, and do not save a recommendation whose basis is data
you have just been told is stale or provisional — say what would need checking
first.

## What not to do

- Do not give tax advice as though you know the user's full tax situation. You
  see a portfolio, not a return.
- Do not recommend specific securities as investments. Discuss allocation,
  placement and concentration.
- Do not present drift as urgency. A portfolio out of band is a fact; whether it
  is worth trading is a judgement involving costs and taxes you cannot fully see.
- Do not fill gaps with assumptions. If a figure is missing, that absence is
  information — report it.
