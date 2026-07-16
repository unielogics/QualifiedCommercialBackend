"""Per-user Google OAuth grants + email_drafts.sender_user_id.

Creates `google_accounts` (one row per user: encrypted refresh token, granted
scopes, per-service capability flags, reserved automation/calendar-sync columns)
and adds `email_drafts.sender_user_id` so send-as-user knows whose connected
Gmail to send a draft from (null => firm SES fallback).

Revision ID: 0093_google_accounts
Revises: 0092_bucket_ai_review_progress
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0093_google_accounts"
down_revision = "0092_bucket_ai_review_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("google_email", sa.String(length=320), nullable=True),
        sa.Column("google_sub", sa.String(length=64), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encryption_provider", sa.String(length=24), nullable=False, server_default="fernet"),
        sa.Column("kms_key_id", sa.String(length=512), nullable=True),
        sa.Column("access_token_cache", sa.Text(), nullable=True),
        sa.Column("access_token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("gmail_connected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("calendar_connected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("drive_connected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("automation_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("calendar_sync_token", sa.Text(), nullable=True),
        sa.Column("calendar_channel_id", sa.String(length=255), nullable=True),
        sa.Column("calendar_resource_id", sa.String(length=255), nullable=True),
        sa.Column("calendar_watch_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_google_accounts_user_id"),
    )
    op.create_index("ix_google_accounts_user_id", "google_accounts", ["user_id"])

    op.add_column(
        "email_drafts",
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_email_drafts_sender_user_id",
        "email_drafts",
        "users",
        ["sender_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_email_drafts_sender_user_id", "email_drafts", type_="foreignkey")
    op.drop_column("email_drafts", "sender_user_id")
    op.drop_index("ix_google_accounts_user_id", table_name="google_accounts")
    op.drop_table("google_accounts")
