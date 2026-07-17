"""Per-mailbox email store for the isolated Workspace inbox (Phase 4).

Creates email_messages: one row per synced Gmail message from a connected Workspace
mailbox. Bodies (body_text_enc / body_html_enc) are stored ENCRYPTED at rest via the
same Fernet/KMS path as google_accounts (self-describing encryption_provider). subject
+ snippet stay plaintext for list/search. Matched loan_id/client_id are nullable
(unmatched → inbox only). Unique on (mailbox, gmail_message_id) for dedup.

Revision ID: 0095_email_messages
Revises: 0094_calendar_google_sync
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0095_email_messages"
down_revision = "0094_calendar_google_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=128), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=128), nullable=True),
        sa.Column("direction", sa.String(length=12), nullable=False, server_default="inbound"),
        sa.Column("from_email", sa.String(length=320), nullable=True),
        sa.Column("to_emails", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cc_emails", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("subject", sa.String(length=998), nullable=True),
        sa.Column("snippet", sa.String(length=1024), nullable=True),
        sa.Column("body_text_enc", sa.Text(), nullable=True),
        sa.Column("body_html_enc", sa.Text(), nullable=True),
        sa.Column("encryption_provider", sa.String(length=24), nullable=False, server_default="fernet"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matched_party_role", sa.String(length=32), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_starred", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loan_id"], ["loans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("mailbox", "gmail_message_id", name="uq_email_messages_mailbox_gmail_id"),
    )
    op.create_index("ix_email_messages_owner_user_id", "email_messages", ["owner_user_id"])
    op.create_index("ix_email_messages_gmail_thread_id", "email_messages", ["gmail_thread_id"])
    op.create_index("ix_email_messages_loan_id", "email_messages", ["loan_id"])
    op.create_index("ix_email_messages_client_id", "email_messages", ["client_id"])
    # The inbox list is ordered newest-first per owner.
    op.create_index(
        "ix_email_messages_owner_received", "email_messages", ["owner_user_id", "received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_owner_received", table_name="email_messages")
    op.drop_index("ix_email_messages_client_id", table_name="email_messages")
    op.drop_index("ix_email_messages_loan_id", table_name="email_messages")
    op.drop_index("ix_email_messages_gmail_thread_id", table_name="email_messages")
    op.drop_index("ix_email_messages_owner_user_id", table_name="email_messages")
    op.drop_table("email_messages")
