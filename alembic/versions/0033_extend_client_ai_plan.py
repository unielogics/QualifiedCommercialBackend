"""Extend client_ai_plan for live deal intelligence (Phase 6).

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-09

Adds the post-handoff fields the spec calls the Deal Intelligence
Profile to client_ai_plan, rather than introducing a parallel table.
The packet (alembic 0031) remains the immutable bootstrap snapshot;
client_ai_plan becomes the live, recomputed view.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "client_ai_plan",
        sa.Column(
            "source_realtor_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column(
            "lending_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column(
            "handoff_packet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lending_handoff_packets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("verified_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("unverified_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("document_conflicts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("agent_visibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("borrower_visibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "client_ai_plan",
        sa.Column("funding_visibility", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("client_ai_plan", "funding_visibility")
    op.drop_column("client_ai_plan", "borrower_visibility")
    op.drop_column("client_ai_plan", "agent_visibility")
    op.drop_column("client_ai_plan", "document_conflicts")
    op.drop_column("client_ai_plan", "unverified_facts")
    op.drop_column("client_ai_plan", "verified_facts")
    op.drop_column("client_ai_plan", "handoff_packet_id")
    op.drop_column("client_ai_plan", "lending_thread_id")
    op.drop_column("client_ai_plan", "source_realtor_thread_id")
