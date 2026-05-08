"""Chat unread + push tokens.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-08

Closes the loop on the conversational doc collector — the borrower
needs to *know* when the AI sends them a message. Two pieces:

  1. `ai_chat_threads.last_seen_at` — bumped to now() whenever the
     borrower opens the thread. Read alongside `last_message_at` to
     compute `unread = last_message_at > last_seen_at`. NULL = never
     opened (every existing message counts as unread on first show).

  2. `device_tokens` — Expo push tokens per (user, token). Mobile
     registers via POST /devices/push-tokens after permission grant;
     `post_ai_message` looks them up and fires a push via Expo's
     HTTP API for every system-initiated AI message.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_chat_threads",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False, server_default="expo"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", "token", name="uq_device_tokens_user_token"),
    )


def downgrade() -> None:
    op.drop_table("device_tokens")
    op.drop_column("ai_chat_threads", "last_seen_at")
