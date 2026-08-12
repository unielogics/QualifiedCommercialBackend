"""Dealer-lead communication channel read cursor.

dealer_lead_channel_seen: when a user (the lead's dealer partner or an internal
teammate) last opened the Messages channel on a dealer AI lead. The channel is
the shared BucketNote(visibility="admin") thread on the lead's bucket; this table
is the per-viewer read state it lacked, powering unread counts on the Messages
tab and the global dealer Messages inbox. One row per (intake_id, user_id).

Revision ID: 0107_dealer_lead_channel_seen
Revises: 0106_admin_activity_tracking
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0107_dealer_lead_channel_seen"
down_revision = "0106_admin_activity_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dealer_lead_channel_seen",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "intake_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intake_id", "user_id", name="uq_dealer_lead_channel_seen_intake_user"),
    )


def downgrade() -> None:
    op.drop_table("dealer_lead_channel_seen")
