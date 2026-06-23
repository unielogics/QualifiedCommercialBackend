"""notifications.

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("recipient_user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("category", sa.String(length=40), server_default="system", nullable=False),
        sa.Column("priority", sa.String(length=16), server_default="medium", nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("target_type", sa.String(length=60), nullable=True),
        sa.Column("target_id", sa.String(length=80), nullable=True),
        sa.Column("deep_link", sa.String(length=600), nullable=True),
        sa.Column("channels", pg.JSONB(), server_default="[]", nullable=False),
        sa.Column("meta", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column("batch_key", sa.String(length=180), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_notifications_recipient_user_id", "notifications", ["recipient_user_id"])
    op.create_index("ix_notifications_event_type", "notifications", ["event_type"])
    op.create_index("ix_notifications_batch_key", "notifications", ["batch_key"])
    op.create_index(
        "ix_notifications_recipient_read_created",
        "notifications",
        ["recipient_user_id", "read_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_recipient_read_created", table_name="notifications")
    op.drop_index("ix_notifications_batch_key", table_name="notifications")
    op.drop_index("ix_notifications_event_type", table_name="notifications")
    op.drop_index("ix_notifications_recipient_user_id", table_name="notifications")
    op.drop_table("notifications")
