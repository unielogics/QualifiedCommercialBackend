"""Deal — agent-side transaction unit (Phase 3).

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-12

A Client can carry multiple Deals at once (buyer search + seller
listing + investor purchase). Each Deal is the agent-side pre-funding
unit; the Loan it eventually promotes to is the funding file
(source_deal_id back-ref lands in alembic 0048).

Indexes:
- ix_deals_client_id
- ix_deals_assigned_agent_id
- ix_deals_client_status (composite, hot path for the workspace endpoint)
- uq_deals_promoted_loan_id partial unique (one deal per promoted loan)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("deal_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False, server_default="buyer"),
        sa.Column(
            "property_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("client_properties.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column(
            "assigned_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ai_status", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("handoff_status", sa.String(24), nullable=False, server_default="none"),
        sa.Column(
            "promoted_loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("living_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
    op.create_index("ix_deals_client_id", "deals", ["client_id"])
    op.create_index("ix_deals_assigned_agent_id", "deals", ["assigned_agent_id"])
    op.create_index("ix_deals_status", "deals", ["status"])
    op.create_index("ix_deals_client_status", "deals", ["client_id", "status"])
    op.create_index(
        "uq_deals_promoted_loan_id",
        "deals",
        ["promoted_loan_id"],
        unique=True,
        postgresql_where=sa.text("promoted_loan_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_deals_promoted_loan_id", table_name="deals")
    op.drop_index("ix_deals_client_status", table_name="deals")
    op.drop_index("ix_deals_status", table_name="deals")
    op.drop_index("ix_deals_assigned_agent_id", table_name="deals")
    op.drop_index("ix_deals_client_id", table_name="deals")
    op.drop_table("deals")
