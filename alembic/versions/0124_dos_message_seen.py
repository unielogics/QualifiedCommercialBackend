"""dealer-os: per-viewer message seen marker (unread counters).

One row per (dealer, user): when that viewer last opened the thread.
Unread = non-internal (for dealers) messages created after seen_at by
someone else.

Revision ID: 0124_dos_message_seen
Revises: 0123_dos_plan_responses
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0124_dos_message_seen"
down_revision = "0123_dos_plan_responses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_message_seen",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dealer_id", "user_id", name="uq_dos_message_seen"),
    )


def downgrade() -> None:
    op.drop_table("dos_message_seen")
