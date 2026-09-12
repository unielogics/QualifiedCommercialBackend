"""Add file-scoped AI Intake SMS and email history.

Revision ID: 0214_ai_intake_communications
Revises: 0213_evidence_workspace_supporting_group
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0214_ai_intake_communications"
down_revision = "0213_evidence_workspace_supporting_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_rep_inbox_threads", sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_inbox_threads", sa.Column("participant_emails", postgresql.JSONB(), nullable=True))
    op.add_column("dos_rep_inbox_threads", sa.Column("provider_thread_id", sa.String(length=160), nullable=True))
    op.create_index("ix_dos_rep_inbox_threads_profile", "dos_rep_inbox_threads", ["profile_id", "last_message_at"])
    op.create_index("ix_dos_rep_inbox_threads_provider_thread", "dos_rep_inbox_threads", ["provider_thread_id"])

    op.add_column("dos_rep_inbox_messages", sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_inbox_messages", sa.Column("message_send_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("dos_rep_inbox_messages", sa.Column("cc_emails", postgresql.JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_dos_rep_inbox_messages_message_send",
        "dos_rep_inbox_messages",
        "message_sends",
        ["message_send_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_dos_rep_inbox_messages_profile", "dos_rep_inbox_messages", ["profile_id", "created_at"])

    for column in ("profile_id", "intake_id", "portal_message_id"):
        op.add_column("sms_messages", sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True))
        op.create_index(f"ix_sms_messages_{column}", "sms_messages", [column])

    op.create_index("ix_message_sends_profile_created", "message_sends", ["profile_id", "created_at"])
    op.create_index("ix_message_sends_intake_created", "message_sends", ["intake_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_message_sends_intake_created", table_name="message_sends")
    op.drop_index("ix_message_sends_profile_created", table_name="message_sends")
    for column in ("portal_message_id", "intake_id", "profile_id"):
        op.drop_index(f"ix_sms_messages_{column}", table_name="sms_messages")
        op.drop_column("sms_messages", column)
    op.drop_index("ix_dos_rep_inbox_messages_profile", table_name="dos_rep_inbox_messages")
    op.drop_constraint("fk_dos_rep_inbox_messages_message_send", "dos_rep_inbox_messages", type_="foreignkey")
    op.drop_column("dos_rep_inbox_messages", "cc_emails")
    op.drop_column("dos_rep_inbox_messages", "message_send_id")
    op.drop_column("dos_rep_inbox_messages", "profile_id")
    op.drop_index("ix_dos_rep_inbox_threads_provider_thread", table_name="dos_rep_inbox_threads")
    op.drop_index("ix_dos_rep_inbox_threads_profile", table_name="dos_rep_inbox_threads")
    op.drop_column("dos_rep_inbox_threads", "provider_thread_id")
    op.drop_column("dos_rep_inbox_threads", "participant_emails")
    op.drop_column("dos_rep_inbox_threads", "profile_id")
