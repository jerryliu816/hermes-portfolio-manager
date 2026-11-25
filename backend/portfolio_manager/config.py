"""Application configuration.

Resolution order for every value: process environment, then the project `.env`,
then the default here. The API key is deliberately allowed to be empty -- the
mock extraction provider drives the entire test suite and needs no credentials.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    """Where the application's non-package files live.

    In development the package sits at `<root>/backend/portfolio_manager`, so
    walking up two levels finds the root. Once installed non-editably -- which
    is how the container builds it -- the package lives in site-packages and
    that walk lands somewhere useless, taking the bundled frontend and the
    `.env` lookup with it. `APP_ROOT` names the root explicitly for that case.
    """
    explicit = os.environ.get("APP_ROOT")
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _repo_root()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- database -----------------------------------------------------------
    # Host port 5433, not 5432: another local project owns 5432. The
    # container-internal port is still 5432 (see docker-compose.yml).
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "portfolio"
    postgres_user: str = "portfolio"
    postgres_password: SecretStr = SecretStr("change-me-locally")

    # --- extraction ---------------------------------------------------------
    extraction_provider: str = "mock"
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5"
    anthropic_timeout_seconds: float = 600.0
    extraction_effort: str = "high"
    max_extraction_attempts: int = 2
    extraction_accounts_per_call: int = 5
    extraction_max_pages: int = 600
    extraction_max_bytes: int = 32 * 1024 * 1024
    #: Logs full document and model content, including holdings. Sensitive.
    extraction_debug_log: bool = False

    # --- reconciliation -----------------------------------------------------
    #: A check passes if EITHER tolerance is met, but the percentage tolerance
    #: is capped by ``recon_max_abs_tolerance`` so a large account cannot be
    #: granted unlimited slack (0.1% of $5M would be $5,000).
    recon_abs_tolerance: Decimal = Decimal("1.00")
    recon_pct_tolerance: Decimal = Decimal("0.001")
    recon_max_abs_tolerance: Decimal = Decimal("25.00")

    # --- snapshots ----------------------------------------------------------
    snapshot_date_tolerance_days: int = 5
    auto_approve_clean_imports: bool = False

    # --- storage ------------------------------------------------------------
    data_dir: Path = Field(default=Path("data"))

    # --- hermes (optional) --------------------------------------------------
    hermes_enabled: bool = False
    hermes_webhook_url: str = ""
    hermes_webhook_secret: SecretStr = SecretStr("")
    hermes_timeout_seconds: float = 5.0
    #: Hermes's api_server platform, which the Advisor page chats through. The
    #: key stays on this server; the browser never sees it.
    hermes_api_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr = SecretStr("")
    #: How long to wait for Hermes to start answering. A turn that researches
    #: the news can take minutes, so the stream itself has no read timeout.
    hermes_connect_timeout_seconds: float = 5.0

    # --- api ----------------------------------------------------------------
    #: Loopback by default. Anything else exposes the app to the network, and
    #: `serve.py` refuses it unless the sign-in below is configured.
    api_host: str = "127.0.0.1"
    api_port: int = 8787

    # --- sign-in ------------------------------------------------------------
    #: Both set turns the sign-in page on. Generate the hash with
    #: `portfolio auth set-password`. Off when either is empty.
    auth_username: str = ""
    auth_password_hash: SecretStr = SecretStr("")
    auth_session_days: int = 30
    #: The built frontend. Served from the same origin as the API when present,
    #: which is what keeps production to one container and one port.
    frontend_dir: Path = Field(default=REPO_ROOT / "frontend" / "dist")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password_hash.get_secret_value())

    @property
    def binds_loopback_only(self) -> bool:
        return self.api_host in {"127.0.0.1", "::1", "localhost"}

    @property
    def database_url(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sources_dir(self) -> Path:
        base = self.data_dir if self.data_dir.is_absolute() else REPO_ROOT / self.data_dir
        return base / "sources"

    @property
    def context_dir(self) -> Path:
        """Documents given to the advisor. Kept apart from `sources`: they are
        opinion and background, never a record of what is held."""
        base = self.data_dir if self.data_dir.is_absolute() else REPO_ROOT / self.data_dir
        return base / "context"


@lru_cache
def get_settings() -> Settings:
    return Settings()
