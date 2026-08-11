"""Backfill user_id on internal-thread assistant rows.

Internal ("admin"-audience) AI threads are becoming private per user: the
super-admin cockpit and each dealer-partner broker keep their own
conversation with the AI on a lead, and threads are never shared between
users. Reads now filter on (user_id == viewer OR user_id IS NULL), and
create_chat_reply stamps user_id on assistant rows going forward.

Historical assistant rows have user_id NULL, which would surface them to
every internal viewer. Each assistant row was flushed in the same
transaction as its prompting user row, so the pair shares an identical
(bucket_id, audience, created_at); copy the user row's user_id across the
pair. Rows with no such pair (system welcome messages) stay NULL on
purpose — they are shared by every internal viewer.

Data-only; no schema change.

Revision ID: 0105_admin_thread_user_backfill
Revises: 0104_intake_delete_request
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op


revision = "0105_admin_thread_user_backfill"
down_revision = "0104_intake_delete_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE bucket_ai_messages AS assistant
        SET user_id = prompt.user_id
        FROM bucket_ai_messages AS prompt
        WHERE assistant.audience = 'admin'
          AND assistant.role = 'assistant'
          AND assistant.user_id IS NULL
          AND prompt.bucket_id = assistant.bucket_id
          AND prompt.audience = 'admin'
          AND prompt.role = 'user'
          AND prompt.user_id IS NOT NULL
          AND prompt.created_at = assistant.created_at
        """
    )


def downgrade() -> None:
    # Irreversible in principle (the pre-backfill NULLs are not recoverable),
    # and harmless to leave in place: user_id on assistant rows only narrows
    # which internal viewer sees the row.
    pass
