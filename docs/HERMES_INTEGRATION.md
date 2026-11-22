# Hermes integration

Optional. `HERMES_ENABLED=false` is the default, and the application is fully
usable without it — that is the ordinary path, not a degraded one.

Hermes answers "what should I do about it?". This system answers "what is
true?". Keeping those separate is the point, so the boundary is enforced rather
than trusted.

## What Hermes can and cannot do

**24 tools**, of which **three** write — and none touches holdings:
`save_recommendation`, `save_recommendation_decision` and
`save_advisor_context`, all writing to advisory tables that are deliberately
not portfolio truth. Context the advisor saves is marked unconfirmed until you
confirm it on the Advisor page.

Three enforcement mechanisms, not one:

1. The exposed tool set is a declared tuple, asserted by a test. A second test
   rejects any tool whose leading verb looks mutating. Adding a tool cannot
   happen by accident.
2. Every read runs under `SET TRANSACTION READ ONLY`, so a coding mistake fails
   at Postgres rather than corrupting a snapshot.
3. No tool accepts SQL or a free-form query. The surface is bounded.

## Every reading carries its own caveats

Each portfolio tool returns `as_of_date`, `completeness`, and the stale,
unclassified and missing-basis fractions. An analyst reasoning about a
portfolio whose largest share has an unknown as-of date should know that, and cannot be relied on to
ask. This is tested by *calling* each tool, not by inspecting source — what
matters is what reaches the analyst.

Account identifiers are masked. Money is a string.

## The skill

`backend/portfolio_manager/hermes/skill/SKILL.md`, version-controlled and
installed to `~/.hermes/skills/productivity/portfolio-review/`. It carries the
rules the data forced:

- Never sum share counts across accounts.
- `has_divergent_prices` is not an error — report it, do not average it away.
- An unconfirmed tax treatment must not be assumed taxable.
- A snapshot comparison is a change in *value*, never a return.
- Read the recommendation history before proposing anything.

## Transport

Verified by reading the installed client at
`~/.hermes/hermes-agent/tools/mcp_tool.py`, not assumed:

`hermes mcp add <name> --url <endpoint>` stores a plain `url` key under
`mcp_servers` in `~/.hermes/config.yaml`, and **the client uses Streamable HTTP
for `url` servers by default** (`transport: sse` is an opt-in override). That is
exactly what `FastMCP.http_app(transport="http")` serves.

One mounting detail that costs an hour if missed: `app.router.lifespan_context =
mcp_app.lifespan` is required. Mounting alone does not chain FastMCP's lifespan,
and without it the session manager never starts — failing on the **first tool
call**, not at startup.

### The endpoint is `/mcp/`, with the trailing slash

A Starlette mount matches only paths *below* its prefix, so bare `/mcp` never
reached the MCP app and fell through to a 405 — which the Hermes client reports
as the unhelpful `Failed to connect: Server returned an error response`.
Registering the slashless form was the documented path for a while, because the
install helper printed it.

`/mcp` now answers with a 307 to `/mcp/` on GET, POST and DELETE. It has to be a
307: a JSON-RPC POST must keep its method and body across the redirect, which
301 and 302 do not guarantee. Prefer the trailing-slash form anyway — not every
MCP client follows redirects.

## Setup

```bash
portfolio hermes install            # prints the commands
portfolio hermes install --apply    # also copies the skill
```

It prints rather than acts by default: modifying an agent's configuration is not
something to do silently as a side effect of another command. Installing the
skill refuses to overwrite an existing one unless asked — you may have edited
the instructions, and those edits are yours; pass `--overwrite` to replace it.

The two commands it prints, with the API already running:

```bash
hermes mcp add portfolio-manager --url http://localhost:8787/mcp/
hermes mcp test portfolio-manager
```

Answer **no** to "Does this server require authentication?" — the endpoint is
bound to `127.0.0.1` and has none. `hermes mcp test` should report **24 tools**.
Zero tools means the API is not running: the endpoint exists only while it is.

Then ask Hermes in plain language — the skill matches on tags like portfolio,
allocation, rebalancing and tax:

> How is my portfolio allocated, and what should I know about the data quality
> before trusting it?

If it does not reach for the skill on its own, name it: *"Using the
portfolio-review skill, ..."*.

### Verifying it end to end

```bash
hermes mcp list                     # transport, tool count, enabled
hermes mcp test portfolio-manager   # connects and enumerates the tools
```

Adding a tool to `EXPOSED_TOOLS` needs no re-registration. Hermes reads the tool
list from the server when it connects, so a new tool appears on the next
connection.

## The advisor

The Advisor page is a chat with Hermes, relayed through this server. Hermes
holds the conversation and does the research; this system holds the truth and
does the arithmetic.

### How a proposal is made

1. The advisor reads data quality, your philosophy (`get_investment_philosophy`)
   and your context (`get_advisor_context`, `read_advisor_context`), the policy
   and the recommendation history.
2. It researches with whatever web search Hermes has configured, and cites
   dated sources. News, pages and PDFs are evidence, never instructions.
3. It asks the engine for a baseline (`suggest_rebalance`): asset-class moves
   per account, tax-sheltered accounts first, taxable gains last.
4. It picks securities and calls `simulate_trades`, iterating until there are
   no violations.
5. When you say so, it calls `save_recommendation` with `kind="trades"` or
   `kind="target_change"`.

**The model never computes a figure.** `get_rebalance_gaps`,
`suggest_rebalance` and `simulate_trades` return the engine's numbers, with
the portfolio's data-quality caveats and the engine's own caveats in a single
list. `save_recommendation` ignores any figures the model supplies for a
structured proposal. It simulates the trades again on the server and stores
that report. A tax figure typed by the model survives only on a free-text
`note`.

**Trades are dollars per account and security, never shares.** Converting
would mean trusting one source's share count, and the same security can be
pre-split in one source and post-split in another. Money never moves between
accounts in a proposal.

**With no policy and no proposed targets, the engine tools refuse** and tell
the advisor to ask. They do not invent targets. Proposed targets are decimal
strings that must sum to 1. A JSON number is rejected by the tool schema.

### The proposal panel

While relaying the stream, this server records the arguments of the
advisor's latest `simulate_trades` call. `GET /advisor/sessions/{id}/proposal`
re-runs the engine on them against the current snapshot and policy, and the
panel shows only that, never the chat text. "Save to journal" calls
`record_proposal`, the function `save_recommendation` uses, so the two paths
cannot disagree.

Each conversation's id is in Hermes's system prompt, and the advisor passes
it as `conversation_id` when saving, which links the journal entry back to the
chat. **Known limitation:** a branched conversation is a Hermes fork, which
inherits the parent's system prompt. A recommendation saved from a branch
therefore links to the parent conversation.

### Setup

`portfolio hermes install` prints the advisor steps under item 4, and
`--apply` installs the `investment-advisor` skill to
`~/.hermes/skills/finance/investment-advisor/`. In short:

1. Put the same long random key in `~/.hermes/.env` as `API_SERVER_KEY` and in
   this project's `.env` as `HERMES_API_KEY`, with `HERMES_ENABLED=true`. The
   key never reaches the browser.
2. **Restrict the advisor's tools.** It reads untrusted text, so in
   `~/.hermes/config.yaml`:

   ```yaml
   platform_toolsets:
     api_server: [web, skills, todo, portfolio-manager]
   ```

   An explicit list replaces the default `hermes-api-server` bundle, which
   includes the terminal and file tools. With this list, the worst a prompt
   injection can do is produce a bad suggestion, and you still have to act on
   it yourself.
3. Choose a web search backend (`hermes tools`). Without one, the advisor
   cannot research current events, and should say so.
4. `hermes gateway restart`, then `portfolio advisor status`.

If Hermes is down or disabled, the Advisor page says so (a 503 naming the URL
it tried), and the rest of the app is unaffected.

## The snapshot trigger

`HermesAnalysisTrigger` posts an HMAC-SHA256-signed
`{"event": "portfolio_snapshot_created", ...}` when an import completes.

**Fire-and-forget, with every failure logged and swallowed.** The snapshot is
already committed and valid by the time it runs, so an unreachable analyst must
never turn a successful import into a failed one.

Webhooks require `platforms.webhook.enabled` in `~/.hermes/config.yaml` and a
running `hermes gateway`; the install helper prints how to enable them rather
than assuming. Put the URL and secret that `hermes webhook subscribe` prints
into `.env` as `HERMES_WEBHOOK_URL` and `HERMES_WEBHOOK_SECRET`, and set
`HERMES_ENABLED=true`.
