#!/usr/bin/env python3
"""Seed a disposable database for the end-to-end tests.

The E2E tests drive a real browser against a real database, and some of them
*change* state — classifying an account is exactly the kind of thing worth
testing end to end, and it is also the kind of thing that makes a second run
behave differently from the first. So the database is rebuilt from scratch
every time rather than the tests being written to tiptoe around their own
effects.

Usage:
    python scripts/seed_e2e.py            # rebuild portfolio_e2e
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from portfolio_manager.config import Settings, get_settings  # noqa: E402
from portfolio_manager.extraction.providers.mock_provider import (  # noqa: E402
    MockExtractionProvider,
)
from portfolio_manager.models import Base  # noqa: E402
from portfolio_manager.services.importer import import_document  # noqa: E402
from portfolio_manager.services.policy import (  # noqa: E402
    STARTER_POLICY_YAML,
    save_policy_yaml,
)
from portfolio_manager.services.recommendation import create_recommendation  # noqa: E402

DATABASE = "portfolio_e2e"
DATA_DIR = Path("/tmp/portfolio-e2e-data")
FIXTURE = REPO_ROOT / "tests/fixtures/extraction/fidelity/fullview_anonymized.csv"


def main() -> int:
    base = get_settings().database_url.rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{DATABASE}"'))
    admin.dispose()

    engine = create_engine(f"{base}/{DATABASE}", future=True)
    Base.metadata.create_all(engine)

    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    settings = Settings(  # type: ignore[call-arg]
        data_dir=DATA_DIR, postgres_db=DATABASE, _env_file=None
    )

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        import_document(session, FIXTURE, MockExtractionProvider(), settings=settings)
        save_policy_yaml(session, STARTER_POLICY_YAML)
        create_recommendation(
            session,
            title="Rebalance toward fixed income",
            rationale="Fixed income sits below its target while cash sits above it.",
            source="hermes",
        )
        session.commit()

    print(f"seeded {DATABASE} (data in {DATA_DIR})")
    print(f"  run the API with: POSTGRES_DB={DATABASE} DATA_DIR={DATA_DIR} make dev")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
