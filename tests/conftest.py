"""Shared test fixtures.

Integration tests run against a dedicated `portfolio_test` database so a test
run can never touch real portfolio state. Tables are created from the models
and dropped per session.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

# A sign-in configured in the developer's own .env must not reach the suite:
# a test that builds the app without overriding settings would otherwise get a
# 401 for every request. The environment outranks .env, and empty means off.
os.environ["AUTH_USERNAME"] = ""
os.environ["AUTH_PASSWORD_HASH"] = ""

from portfolio_manager.config import get_settings  # noqa: E402
from portfolio_manager.models import Base  # noqa: E402

TEST_DB = "portfolio_test"


def _admin_url() -> str:
    return get_settings().database_url.rsplit("/", 1)[0] + "/postgres"


def _test_url() -> str:
    return get_settings().database_url.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if os.getenv("SKIP_DB_TESTS"):
        pytest.skip("SKIP_DB_TESTS is set")
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as exc:  # pragma: no cover - environment issue, not a test failure
        pytest.skip(f"postgres unavailable: {exc}")
    finally:
        admin.dispose()

    eng = create_engine(_test_url(), future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session rolled back after each test, so tests cannot leak into others."""
    connection = engine.connect()
    transaction = connection.begin()
    sess = sessionmaker(bind=connection, expire_on_commit=False, future=True)()
    try:
        yield sess
    finally:
        sess.close()
        # A test that provoked an IntegrityError leaves the transaction already
        # deassociated; rolling back again would warn.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
