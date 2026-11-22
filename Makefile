PY := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: venv install dev-db dev-db-stop migrate test lint fmt dev check-secrets \
	service-install service-uninstall service-status service-logs

venv:
	/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv

install: venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

dev-db:
	docker compose up -d postgres
	@until docker compose exec -T postgres pg_isready -q; do sleep 1; done
	@echo "postgres ready on localhost:$${POSTGRES_PORT:-5433}"

dev-db-stop:
	docker compose stop postgres

migrate:
	.venv/bin/alembic upgrade head

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check backend tests
	.venv/bin/ruff format --check backend tests
	.venv/bin/mypy

fmt:
	.venv/bin/ruff format backend tests
	.venv/bin/ruff check --fix backend tests

dev:
	.venv/bin/uvicorn portfolio_manager.main:app --reload --port $${API_PORT:-8787} --app-dir backend

dev-ui:
	cd frontend && npm run dev

ui-install:
	cd frontend && npm install

ui-build:
	cd frontend && npm run build

ui-check:
	cd frontend && npm run typecheck && npm run lint

e2e-seed:
	.venv/bin/python scripts/seed_e2e.py

e2e: e2e-seed
	@echo "start the API in another shell:"
	@echo "  POSTGRES_DB=portfolio_e2e DATA_DIR=/tmp/portfolio-e2e-data make dev"
	cd frontend && npm run e2e

benchmark:
	.venv/bin/portfolio extraction benchmark

benchmark-check:
	.venv/bin/python scripts/generate_expected.py --check

backup:
	.venv/bin/portfolio backup create backups

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-logs:
	docker compose logs -f app

docker-down:
	docker compose down

check-secrets:
	@git check-ignore -q .env  || (echo ".env is NOT ignored" && exit 1)
	@git check-ignore -q test/ || (echo "test/ is NOT ignored" && exit 1)
	@git check-ignore -q data/ || (echo "data/ is NOT ignored" && exit 1)
	@git check-ignore -q --no-index PROGRESS.md \
		|| (echo "PROGRESS.md is NOT ignored" && exit 1)
	@git check-ignore -q --no-index docs/FOR_FUTURE_SESSIONS.md \
		|| (echo "docs/FOR_FUTURE_SESSIONS.md is NOT ignored" && exit 1)
	@test -z "$$(git ls-files PROGRESS.md docs/FOR_FUTURE_SESSIONS.md)" \
		|| (echo "a local-only doc is tracked" && exit 1)
# A representative archive name, not the directory: backups/.gitkeep is
# tracked so the folder ships, and check-ignore honours the index.
	@git check-ignore -q backups/portfolio-backup-any.tar.gz \
		|| (echo "backup archives are NOT ignored" && exit 1)
	@! git log -p 2>/dev/null | grep -qE 'sk-ant-[A-Za-z0-9_-]{8}' || (echo "API key found in history" && exit 1)
	@echo "secret hygiene: OK"

# --- run as a login agent -------------------------------------------------
# `make dev` is for a person at a terminal. These install the same app as a
# launchd agent that starts at login and restarts if it dies.

LAUNCH_AGENT := $(HOME)/Library/LaunchAgents/dev.portfolio-manager.api.plist
SERVICE_LABEL := dev.portfolio-manager.api

# `bootout` returns before launchd has finished tearing the job down, and
# bootstrapping a label that is still on its way out fails with the famously
# unhelpful "Bootstrap failed: 5: Input/output error". So wait for the label to
# actually disappear before installing over it.
define wait_for_bootout
	@launchctl bootout gui/$$(id -u)/$(SERVICE_LABEL) 2>/dev/null || true
	@for _ in $$(seq 1 50); do \
		launchctl print gui/$$(id -u)/$(SERVICE_LABEL) >/dev/null 2>&1 || break; \
		sleep 0.2; \
	done
endef

service-install:
	@mkdir -p logs
	@sed "s|__ROOT__|$(CURDIR)|g" scripts/launchd/dev.portfolio-manager.api.plist.template > $(LAUNCH_AGENT)
	$(call wait_for_bootout)
	@launchctl bootstrap gui/$$(id -u) $(LAUNCH_AGENT)
	@echo "installed $(LAUNCH_AGENT)"
	@echo "it starts at login; logs are in ./logs"

service-uninstall:
	$(call wait_for_bootout)
	@rm -f $(LAUNCH_AGENT)
	@echo "removed the login agent; the app no longer starts on its own"

service-status:
	@launchctl print gui/$$(id -u)/$(SERVICE_LABEL) 2>/dev/null \
		| grep -E "state|pid|last exit" || echo "not loaded"

service-logs:
	@tail -n 40 -F logs/api.log logs/api.error.log
