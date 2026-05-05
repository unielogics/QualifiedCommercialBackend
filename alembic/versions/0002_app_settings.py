"""app_settings singleton table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Unique=True on `singleton` enforces at most one row.
        sa.Column("singleton", sa.Boolean, nullable=False, unique=True, server_default=sa.true()),
        sa.Column("data", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
