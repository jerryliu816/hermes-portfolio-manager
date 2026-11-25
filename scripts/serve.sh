#!/bin/bash
# Start the API the way a background service should, rather than the way a
# terminal does.
#
# `make dev` assumes a person is watching: it reloads on edits, logs to a
# terminal, and dies with the shell. A login agent has none of that, so the two
# things it must do for itself are here -- wait for the database, and apply any
# migrations the checked-out code expects.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# launchd starts agents with a minimal PATH that has neither Docker nor
# Homebrew on it.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PG_PORT="${POSTGRES_PORT:-5433}"

echo "[$(date -u +%FT%TZ)] starting portfolio-manager from $ROOT"

# Docker Desktop may still be coming up when the agent fires at login. The
# container itself is `restart: unless-stopped`, so the job here is to wait for
# it rather than to own its lifecycle.
if command -v docker >/dev/null 2>&1; then
  for _ in $(seq 1 60); do
    if docker compose up -d postgres >/dev/null 2>&1; then
      break
    fi
    echo "waiting for docker to accept commands"
    sleep 5
  done
fi

# An unreachable database is not a reason to give up: launchd will restart this
# job, but a clean wait beats a crash loop in the log.
for _ in $(seq 1 60); do
  if nc -z localhost "$PG_PORT" 2>/dev/null; then
    break
  fi
  echo "waiting for postgres on $PG_PORT"
  sleep 2
done

# The alternative is serving against a schema the code does not expect, which
# fails later and less clearly.
.venv/bin/alembic upgrade head

# Host and port come from the settings (API_HOST / API_PORT in .env), not from
# here. The entry point refuses a non-loopback host unless the sign-in is
# configured, so editing .env cannot quietly put the app on the network open.
echo "[$(date -u +%FT%TZ)] starting the API (API_HOST/API_PORT from .env)"
exec .venv/bin/python -m portfolio_manager.serve
