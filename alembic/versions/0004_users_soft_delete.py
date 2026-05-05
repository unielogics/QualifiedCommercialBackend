"""Soft-delete + clerk_id nullable on users (team invitations)

Adds users.deleted_at so revoked team members are filtered out without
losing FK integrity to loans/clients/brokers, and relaxes clerk_id to be
nullable so an invite can create a row before the user finishes Clerk sign-up.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Relax clerk_id so we can create an invited row before Clerk sign-up
    # completes. The unique index is preserved (NULLs allowed under PG).
    op.alter_column("users", "clerk_id", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "clerk_id", existing_type=sa.String(64), nullable=False)
    op.drop_column("users", "deleted_at")
