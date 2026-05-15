"""Deal chat messages — multi-party (A) thread per Deal.

Mirrors the loan_chat_messages table (the (L) workspace chat) but
keyed on deal_id. Broker, client, and AI all write here pre-promotion.
On promotion (services/handoff.promote_deal_to_loan) the history is
summarized into a single broker_internal message at the top of the
new loan's loan_chat_messages so the funding team inherits context.

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deal_chat_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "deal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_role", sa.String(32), nullable=False),
        sa.Column(
            "from_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "client_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_deal_chat_messages_deal_id_created_at",
        "deal_chat_messages",
        ["deal_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deal_chat_messages_deal_id_created_at", table_name="deal_chat_messages")
    op.drop_table("deal_chat_messages")
