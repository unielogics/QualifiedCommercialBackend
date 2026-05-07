"""Persisted AI Underwriter chat threads + messages.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-07

The standalone AI Intelligent Underwriter chat (mobile FAB / desktop
topbar icon) shipped in Phase 1 with conversation state held only in
component state — closing the panel wiped everything. Phase 8 adds
real persistence so users can resume previous conversations across
sessions, devices, and refreshes.

Two tables:

  ai_chat_threads
    one row per conversation. Owned by a single user. Holds title +
    last_message_preview/last_message_at for cheap list rendering
    without joining ai_chat_messages.

  ai_chat_messages
    role + body + thread_id + timestamps. role is a free-string
    discriminator ('user' | 'assistant') matching the shape we
    replay into Anthropic.

Cascade: deleting a user removes their threads (FK ON DELETE
CASCADE); deleting a thread removes its messages.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_chat_threads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "title",
            sa.String(length=120),
            nullable=False,
            server_default="New conversation",
        ),
        sa.Column("last_message_preview", sa.String(length=240), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_chat_threads_user_id", "ai_chat_threads", ["user_id"]
    )

    op.create_table(
        "ai_chat_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"], ["ai_chat_threads.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_chat_messages_thread_id", "ai_chat_messages", ["thread_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_chat_messages_thread_id", table_name="ai_chat_messages")
    op.drop_table("ai_chat_messages")
    op.drop_index("ix_ai_chat_threads_user_id", table_name="ai_chat_threads")
    op.drop_table("ai_chat_threads")
