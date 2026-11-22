# Deployment

```bash
docker compose up -d --build
```

App on `:8787`, serving the API and the built UI from the same origin. Postgres
on host `:5433` — deliberately not 5432, so this can run alongside another local
Postgres. Both ports are published on `127.0.0.1` only; to reach the app from
another machine use an SSH tunnel (`ssh -L 8787:localhost:8787 <host>`) rather
than widening the binding — the MCP endpoint is unauthenticated.

Use `--build` when the code has changed: `docker compose up` reuses its own
previously built image, and a `docker build` under a different tag does not
affect it.

## Starting automatically on a Mac

`docker compose up -d --build` already restarts both containers with Docker, so
if you run the app in containers there is nothing else to do beyond enabling
**Start Docker Desktop when you sign in** (Settings → General).

Running the app from source instead — the `make dev` setup, where only Postgres
is containerised and the original statements live in `./data` rather than a
Docker volume — needs a login agent, because `make dev` dies with the terminal
that started it.

```bash
make service-install      # generate the plist, load it, start now
make service-status       # state, pid, last exit code
make service-logs         # tail logs/api.log and logs/api.error.log
make service-uninstall    # stop it starting at login
```

`service-install` writes `~/Library/LaunchAgents/dev.portfolio-manager.api.plist`
from the template in `scripts/launchd/`, substituting the repository's absolute
path, and bootstraps it immediately — no reboot needed to test. Stop `make dev`
first, or the agent cannot bind `:8787`.

**Docker Desktop must be set to start at login.** Postgres is
`restart: unless-stopped`, so the container returns once Docker is running, but
nothing starts Docker itself. Without that setting the agent restart-loops at
every boot waiting for a database that never arrives.

### If `service-install` reports "Bootstrap failed: 5"

`launchctl bootout` returns before launchd has finished tearing the job down,
so bootstrapping the same label immediately afterwards can land while it is
still on its way out, and launchd reports that as `Input/output error` — which
says nothing about the actual cause. `service-install` waits for the label to
disappear before installing over it, so this should not recur; if it does, it is
a genuine problem with the plist, and `plutil -lint` on
`~/Library/LaunchAgents/dev.portfolio-manager.api.plist` is the place to start.

### What the agent has to do that `make dev` does not

`make dev` assumes a person is watching. `scripts/serve.sh` covers the three
things that assumption hides:

- **launchd starts agents with a minimal PATH** that has neither Docker nor
  Homebrew on it. The script sets its own.
- **Docker is usually still starting at login.** It retries `docker compose up
  -d postgres` for up to five minutes, then waits for port 5433 to accept a
  connection, rather than failing into a crash loop.
- **Migrations run before serving.** Otherwise a pull that adds a migration
  leaves the service quietly answering against a schema the code does not
  expect — the same reason the container entrypoint does it.

`KeepAlive` restarts the job if it dies anyway, throttled to 15 seconds. The
server binds `127.0.0.1` explicitly rather than `0.0.0.0`: the MCP endpoint is
unauthenticated, and a service that starts by itself should not quietly become
reachable from the network. Logs go to `./logs`, which is gitignored.

### While the agent is running

There is no `--reload`. After changing backend code:

```bash
launchctl kickstart -k gui/$(id -u)/dev.portfolio-manager.api
```

Frontend changes still need `make ui-build` — the agent serves the built bundle
from `frontend/dist`, not the Vite dev server.

To go back to developing, `make service-uninstall` or work on another port. Two
processes cannot share `:8787`.

## The image

Multi-stage. Node builds the frontend and does **not** appear in the final
image, keeping an entire runtime and its CVE surface out of something that runs
unattended. The app runs as a non-root user, and `/app/data` is mode 0700
because it holds original statements.

`APP_ROOT=/app` is set explicitly. Without it the installed package resolves
paths relative to `site-packages`, and the bundled UI is silently not served.

## Startup

The entrypoint waits for the database, applies migrations, then starts the
server. Migrations run there rather than as a separate step someone can forget;
`alembic upgrade head` is idempotent, so restarts are safe.

The healthcheck reports the extraction provider as well as liveness, so a
container that is up but misconfigured is distinguishable from a healthy one.

## Volumes — both matter

| Volume | Holds |
|---|---|
| `portfolio-pgdata` | the database |
| `portfolio-sources` | the original documents, at `/app/data` |

The documents are the only way to re-derive a reading if the schema changes or
extraction improves. Losing them means re-importing from scratch, assuming you
still have the files.

`./backups` on the host is mounted at `/backups`.

## Backup and restore

The database and the documents are **one backup**. An archive always contains
both, and a restore refuses one missing either.

```bash
docker compose exec app portfolio backup create /backups
docker compose exec app portfolio backup inspect /backups/portfolio-backup-….tar.gz
docker compose exec app portfolio backup restore /backups/portfolio-backup-….tar.gz --yes
```

An archive is a plain `tar.gz` of a manifest, a `pg_dump` in **plain SQL**, and
the documents. Plain SQL because a backup you cannot inspect without the exact
tool that wrote it is one you are trusting on faith.

Restore refuses without `--yes`, refuses a future archive version, and refuses
any tar member whose path escapes the destination — a restore is exactly when
someone hands you a file from elsewhere. Archives are written mode 0600.

Verified end to end: dropping the entire schema and deleting every stored
document, then restoring, returns the portfolio and the documents intact.

### Restoring while the login agent is running

The agent holds an open connection pool, and a restore drops and recreates
tables underneath it. Stop it first, and start it again afterwards:

```bash
launchctl bootout gui/$(id -u)/dev.portfolio-manager.api
portfolio backup restore <archive> --yes
make service-install
```

Running from source rather than in containers, `pg_dump` and `psql` have to be
on the host's PATH — `brew install libpq && brew link --force libpq` on macOS.
The containers already carry them, which is why the commands above are written
`docker compose exec app`.

Backups are not automatic. Nothing in the login agent takes one; it starts the
API and does nothing else. See
[the user guide](USER_GUIDE.md#backups) for the procedure.

## Configuration

Everything is environment variables; see `.env.example`. `.env` is gitignored
and never built into an image — `.dockerignore` excludes it along with `test/`
and `data/`.

The pre-commit hook at `.githooks/pre-commit` blocks staged `sk-ant-` keys and
any attempt to commit `.env`, `test/`, `data/` or the local-only docs. The
pre-push hook refuses to send commits from outside the public history to the
public repository. Enable both with
`git config core.hooksPath "$(pwd)/.githooks"` — absolute, so git worktrees
run them too — and verify with `make check-secrets`.

## Running against a DGX or another host

Nothing is Mac-specific. Point `POSTGRES_HOST` at the database and set
`EXTRACTION_PROVIDER`. To run a local model instead of Claude, see
[Adding a provider](ADDING_A_PROVIDER.md) — the provider boundary is one file.
