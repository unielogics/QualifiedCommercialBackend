"""Lending Handoff Packet — bridges Realtor AI ↔ Lending AI.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-09

When the agent fires "Ready for Lending" on a client, we don't just
flip a status flag — we generate a structured handoff that the
Lending AI inherits as memory. This migration adds the bridge:

  lending_handoff_packets         NEW table — one row per handoff
                                  event. Carries the realtor summary,
                                  extracted facts, missing lending
                                  items, document refs, recommended
                                  lending path, the first lending
                                  AI question, and visibility rules.

  ai_chat_threads.phase           realtor | lending — drives the AI
                                  persona selection. Existing rows
                                  default to NULL (legacy/account-wide
                                  threads) so the selector falls
                                  through to its existing logic.

  ai_chat_threads.parent_thread_id   FK ai_chat_threads(id) — the
                                  lending thread points at its
                                  realtor predecessor so the AI can
                                  load full history when needed.

  ai_chat_threads.handoff_packet_id  FK lending_handoff_packets(id) —
                                  attached to the lending thread so
                                  every send-message reads the
                                  packet as the system context block.

  ai_chat_threads.prequal_request_id FK prequal_requests(id) — convenience
                                  for surfacing the quote without an
                                  extra join through the packet.

Idempotent — re-firing "Ready for Lending" on a client whose
lead_promotion_status is already agent_requested_review reads the
existing packet instead of creating a new one.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lending_handoff_packets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "realtor_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "lending_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "prequal_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prequal_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("handoff_reason", sa.String(64), nullable=False, server_default="agent_marked_ready_for_lending"),
        sa.Column("handoff_summary", sa.Text(), nullable=True),
        sa.Column("realtor_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extracted_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("missing_lending_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("uploaded_document_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("recommended_lending_path", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("first_lending_question", sa.Text(), nullable=True),
        sa.Column("visibility_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.add_column("ai_chat_threads", sa.Column("phase", sa.String(16), nullable=True))
    op.add_column(
        "ai_chat_threads",
        sa.Column(
            "parent_thread_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "ai_chat_threads",
        sa.Column(
            "handoff_packet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lending_handoff_packets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "ai_chat_threads",
        sa.Column(
            "prequal_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prequal_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_chat_threads", "prequal_request_id")
    op.drop_column("ai_chat_threads", "handoff_packet_id")
    op.drop_column("ai_chat_threads", "parent_thread_id")
    op.drop_column("ai_chat_threads", "phase")
    op.drop_table("lending_handoff_packets")
