#!/bin/sh
#
# Container entrypoint.
#
# Migrations run before the server starts, not in a separate step someone can
# forget. `alembic upgrade head` is idempotent, so a restart is safe.
set -eu

wait_for_database() {
    attempts=0
    until python -c "
import sys
from sqlalchemy import create_engine, text
from portfolio_manager.config import get_settings
try:
    create_engine(get_settings().database_url).connect().execute(text('select 1'))
except Exception as exc:
    print(f'  database not ready: {exc}', file=sys.stderr)
    raise SystemExit(1)
" 2>/dev/null; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 60 ]; then
            echo "database did not become reachable after 60 attempts" >&2
            exit 1
        fi
        sleep 1
    done
}

case "${1:-serve}" in
    serve)
        echo "waiting for the database…"
        wait_for_database
        echo "applying migrations…"
        alembic upgrade head
        echo "starting on port ${API_PORT:-8787}"
        exec uvicorn portfolio_manager.main:app \
            --host 0.0.0.0 --port "${API_PORT:-8787}" --proxy-headers
        ;;
    migrate)
        wait_for_database
        exec alembic upgrade head
        ;;
    shell)
        exec /bin/sh
        ;;
    *)
        # Anything else is a portfolio CLI command: `docker compose run app backup ...`
        wait_for_database
        exec portfolio "$@"
        ;;
esac
