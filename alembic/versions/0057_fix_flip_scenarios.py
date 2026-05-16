"""Fix & Flip Deal Analyzer scenarios.

Standalone analyzer runs. client/deal/loan all optional so the tool
works before any pipeline record exists; created_by is the durable
owner for scoping. status tracks the save → funding handoff lifecycle.

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fix_flip_scenarios",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "client_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "deal_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deals.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "loan_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deal_score", sa.Integer(), nullable=True),
        sa.Column("deal_grade", sa.String(16), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_fix_flip_scenarios_created_by", "fix_flip_scenarios", ["created_by"])
    op.create_index("ix_fix_flip_scenarios_client_id", "fix_flip_scenarios", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_fix_flip_scenarios_client_id", table_name="fix_flip_scenarios")
    op.drop_index("ix_fix_flip_scenarios_created_by", table_name="fix_flip_scenarios")
    op.drop_table("fix_flip_scenarios")
