"""Activity gets a nullable client_id so the agent CRM workspace can
log calls / SMS / meetings against a Client even before a Loan exists.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # Activity uses occurred_at (not created_at) — see app/models/activity.py.
    # Earlier draft of this migration referenced the wrong column and crashed
    # alembic on first apply against a real DB.
    op.create_index("ix_activities_client", "activities", ["client_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_activities_client", table_name="activities")
    op.drop_column("activities", "client_id")
