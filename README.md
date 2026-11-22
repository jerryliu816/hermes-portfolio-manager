# Portfolio Manager

A personal portfolio system of record. It reads brokerage exports through a
GenAI extraction layer, verifies them arithmetically, and owns the resulting
portfolio state over time.

Three independent layers:

| Layer | Answers | Replaceable? |
|---|---|---|
| **Extraction** | "What does this document say?" | Yes — that is the point |
| **Portfolio Manager** | "What is true?" | No — this is the system of record |
| **Hermes** (optional) | "What should I do about it?" | Yes — and absent by default |

The app is fully usable with Hermes absent, and Hermes can never mutate
holdings.

## The one idea worth knowing up front

**Every figure travels with what qualifies it.** A total is never shown alone if
part of it is stale, unclassified, or missing a cost basis. That is not
defensiveness — on the real portfolio this was built against, 61% of value sat
in accounts whose as-of date was unknown, and the aggregation feed reporting
them had missed an entire rebalance. A confident-looking number would have been
wrong in a way nothing on screen revealed.

## Quick start

```bash
brew install python@3.12          # macOS; any 3.12+ works
make install                      # venv + dependencies
make dev-db                       # Postgres 17 in Docker, host port 5433
make migrate

portfolio import path/to/export.csv --provider mock
portfolio summary
```

For the web UI:

```bash
make ui-install
make dev                          # API on :8787
make dev-ui                       # UI on :5173, proxying to the API
```

Or run the whole thing in containers:

```bash
docker compose up -d --build      # app on :8787, UI served from the same origin
```

To have it start by itself at login instead of typing `make dev` every time, see
[starting automatically on a Mac](docs/DEPLOYMENT.md#starting-automatically-on-a-mac).

To let an agent read the portfolio, see
[Hermes integration](docs/HERMES_INTEGRATION.md#setup).

## Documentation

| Document | What it covers |
|---|---|
| [User guide](docs/USER_GUIDE.md) | Importing, reviewing, classifying, policy, the journal |
| [Architecture](docs/ARCHITECTURE.md) | How the pieces fit and why they are shaped that way |
| [Data model](docs/DATA_MODEL.md) | The tables, and the constraints that make them trustworthy |
| [GenAI extraction](docs/GENAI_EXTRACTION.md) | The provider boundary, prompts, and the benchmark |
| [Adding a provider](docs/ADDING_A_PROVIDER.md) | Running a local model instead of Claude |
| [Adding an institution](docs/ADDING_AN_INSTITUTION.md) | Supporting a new brokerage |
| [Hermes integration](docs/HERMES_INTEGRATION.md) | The MCP surface, the skill, the trigger |
| [Deployment](docs/DEPLOYMENT.md) | Docker, starting at login, volumes, backup and restore |

## Status

415 Python tests and 13 end-to-end tests. `ruff`, `ruff format`,
`mypy --strict`, `tsc` and `oxlint` all clean.
