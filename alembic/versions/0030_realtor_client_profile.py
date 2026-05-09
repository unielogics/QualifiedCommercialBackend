"""Realtor Client Intelligence Profile + per-client AI threads.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-09

Spec: Realtor AI layer — separate front-end relationship + transaction
assistant ahead of the Bank/Lending AI. Adds two columns:

  clients.realtor_profile        JSONB (free-shape; written by the
                                 Realtor AI on every conversational
                                 turn). Carries client_type,
                                 relationship_stage, intent_summary,
                                 buyer_profile, seller_profile,
                                 known_facts, missing_facts, etc.

  ai_chat_threads.client_id      FK clients(id), nullable. Existing
                                 threads scope by (user_id, loan_id);
                                 realtor work happens before a loan
                                 exists, so we add a client-scoped
                                 dimension.

The new partial unique index `ix_ai_chat_threads_user_client` enforces
one client-scoped thread per (user, client) pair, mirroring the
existing per-loan unique. NULL client_id is excluded so loan-scoped
threads (which already enforce per-(user, loan) uniqueness) and the
account-wide thread aren't affected.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Realtor Client Intelligence Profile — JSONB blob the Realtor AI
    # reads + writes every turn.
    op.add_column(
        "clients",
        sa.Column("realtor_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Per-client AI chat threads.
    op.add_column(
        "ai_chat_threads",
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # Partial unique — one thread per (user, client). Ignores rows
    # where client_id is NULL so existing per-loan + account-wide
    # uniques continue to apply unchanged.
    op.create_index(
        "ix_ai_chat_threads_user_client",
        "ai_chat_threads",
        ["user_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_chat_threads_user_client",
        table_name="ai_chat_threads",
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )
    op.drop_column("ai_chat_threads", "client_id")
    op.drop_column("clients", "realtor_profile")
