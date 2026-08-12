"""Admin activity tracking: per-admin seen cursors + email digest state.

- admin_activity_seen: when a super admin last looked at a lead
  (intake_id set) or at the global what's-new feed (intake_id NULL).
  Drives the NEW badges and unseen counts in the admin leads UI.
- admin_digest_state: singleton cursor for the client/broker activity
  email digest job (services/admin_activity.py) — last event emailed and
  when the last digest went out. The cursor only advances after a
  successful SES send, so no activity is lost while SES is unprovisioned.
- bucket_activity_logs.created_at index: both consumers scan recent
  activity by time.

Revision ID: 0106_admin_activity_tracking
Revises: 0105_admin_thread_user_backfill
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0106_admin_activity_tracking"
down_revision = "0105_admin_thread_user_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_activity_seen",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "intake_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_admin_activity_seen_user_intake",
        "admin_activity_seen",
        ["user_id", "intake_id"],
        unique=True,
        postgresql_where=sa.text("intake_id IS NOT NULL"),
    )
    op.create_index(
        "ix_admin_activity_seen_user_feed",
        "admin_activity_seen",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("intake_id IS NULL"),
    )

    op.create_table(
        "admin_digest_state",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("singleton", sa.Boolean(), nullable=False, unique=True, server_default=sa.true()),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_bucket_activity_logs_created_at", "bucket_activity_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_bucket_activity_logs_created_at", table_name="bucket_activity_logs")
    op.drop_table("admin_digest_state")
    op.drop_index("ix_admin_activity_seen_user_feed", table_name="admin_activity_seen")
    op.drop_index("ix_admin_activity_seen_user_intake", table_name="admin_activity_seen")
    op.drop_table("admin_activity_seen")
