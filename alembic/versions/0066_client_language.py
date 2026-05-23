"""clients.language — preferred language for AI outbound messages.

The AI Agent composer reads this column and instructs the model to
write in that language. Nullable + additive — existing rows are
unaffected.

Revision ID: 0066
Revises: 0065
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("language", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clients", "language")
