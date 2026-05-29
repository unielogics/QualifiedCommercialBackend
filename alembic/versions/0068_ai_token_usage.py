"""ai_token_usage — append-only token-cost ledger for AI calls.

One row per Anthropic completion. Powers the super-admin token-usage
report (per file / agent / activity / broker / model, over time).

Revision ID: 0068
Revises: 0067
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_token_usage",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("activity", sa.String(length=48), server_default="other", nullable=False),
        sa.Column("loan_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("deal_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("client_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("ai_agent_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("broker_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cache_creation_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
    )
    op.create_index("ix_ai_token_usage_created", "ai_token_usage", ["created_at"])
    op.create_index("ix_ai_token_usage_activity", "ai_token_usage", ["activity"])
    op.create_index("ix_ai_token_usage_loan", "ai_token_usage", ["loan_id"])
    op.create_index("ix_ai_token_usage_deal", "ai_token_usage", ["deal_id"])
    op.create_index("ix_ai_token_usage_agent", "ai_token_usage", ["ai_agent_id"])
    op.create_index("ix_ai_token_usage_broker", "ai_token_usage", ["broker_id"])


def downgrade() -> None:
    for ix in (
        "ix_ai_token_usage_broker",
        "ix_ai_token_usage_agent",
        "ix_ai_token_usage_deal",
        "ix_ai_token_usage_loan",
        "ix_ai_token_usage_activity",
        "ix_ai_token_usage_created",
    ):
        op.drop_index(ix, table_name="ai_token_usage")
    op.drop_table("ai_token_usage")
