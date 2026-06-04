"""ai_usage_events — Anthropic spend ledger and budget audit trail.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), server_default="anthropic", nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("broker_id", pg.UUID(as_uuid=True), sa.ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_id", pg.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("loan_id", pg.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("thread_id", pg.UUID(as_uuid=True), sa.ForeignKey("ai_chat_threads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("metadata_json", pg.JSONB(), nullable=True),
    )
    op.create_index("ix_ai_usage_events_created", "ai_usage_events", ["created_at"])
    op.create_index("ix_ai_usage_events_feature_created", "ai_usage_events", ["feature", "created_at"])
    op.create_index("ix_ai_usage_events_category_created", "ai_usage_events", ["category", "created_at"])
    op.create_index("ix_ai_usage_events_broker_created", "ai_usage_events", ["broker_id", "created_at"])
    op.create_index("ix_ai_usage_events_client_created", "ai_usage_events", ["client_id", "created_at"])
    op.create_index("ix_ai_usage_events_loan_created", "ai_usage_events", ["loan_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_usage_events_loan_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_client_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_broker_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_category_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_feature_created", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_created", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
