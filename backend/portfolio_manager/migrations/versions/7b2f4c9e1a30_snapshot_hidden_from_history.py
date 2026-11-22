"""snapshot hidden from history

Revision ID: 7b2f4c9e1a30
Revises: e66df13cb924
Create Date: 2026-11-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b2f4c9e1a30"
down_revision: str | Sequence[str] | None = "e66df13cb924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "portfolio_snapshot",
        sa.Column("hidden_from_history", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("portfolio_snapshot", "hidden_from_history")
