"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("clerk_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("email", sa.String(320), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(160), nullable=False, server_default=""),
        sa.Column("role", sa.String(32), nullable=False, server_default="client"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "brokers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), unique=True),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("tier", sa.String(16), server_default="bronze"),
        sa.Column("joined", sa.Date),
        sa.Column("lifetime_points", sa.Integer, server_default="0"),
        sa.Column("redeemed_points", sa.Integer, server_default="0"),
        sa.Column("balance_points", sa.Integer, server_default="0"),
        sa.Column("funded_total", sa.Numeric(14, 2), server_default="0"),
        sa.Column("funded_count", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(32)),
        sa.Column("city", sa.String(160)),
        sa.Column("since", sa.Date),
        sa.Column("tier", sa.String(16), server_default="standard"),
        sa.Column("fico", sa.Integer),
        sa.Column("avatar_color", sa.String(16)),
        sa.Column("referral_source", sa.String(160)),
        sa.Column("funded_total", sa.Numeric(14, 2), server_default="0"),
        sa.Column("funded_count", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "loans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("deal_id", sa.String(16), unique=True, index=True, nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE")),
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("city", sa.String(160)),
        sa.Column("property_type", sa.String(32), server_default="single_family"),
        sa.Column("sqft", sa.Integer),
        sa.Column("beds", sa.Integer),
        sa.Column("baths", sa.Numeric(4, 1)),
        sa.Column("year_built", sa.Integer),
        sa.Column("annual_taxes", sa.Numeric(12, 2), server_default="0"),
        sa.Column("annual_insurance", sa.Numeric(12, 2), server_default="0"),
        sa.Column("monthly_hoa", sa.Numeric(10, 2), server_default="0"),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(32)),
        sa.Column("stage", sa.String(32), server_default="prequalified"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("ltv", sa.Numeric(6, 4)),
        sa.Column("ltc", sa.Numeric(6, 4)),
        sa.Column("arv", sa.Numeric(14, 2)),
        sa.Column("base_rate", sa.Numeric(7, 6)),
        sa.Column("discount_points", sa.Numeric(5, 3), server_default="0"),
        sa.Column("final_rate", sa.Numeric(7, 6)),
        sa.Column("origination_pct", sa.Numeric(6, 4), server_default="0.015"),
        sa.Column("term_months", sa.Integer),
        sa.Column("monthly_rent", sa.Numeric(12, 2)),
        sa.Column("dscr", sa.Numeric(8, 4)),
        sa.Column("risk_score", sa.Integer),
        sa.Column("close_date", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(64)),
        sa.Column("s3_key", sa.String(512)),
        sa.Column("status", sa.String(32), server_default="pending"),
        sa.Column("requested_on", sa.Date),
        sa.Column("received_on", sa.Date),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("verified_by", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "hud_line_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE")),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), server_default="0"),
        sa.Column("category", sa.String(16), server_default="fixed"),
        sa.Column("editable", sa.Boolean, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_label", sa.String(64)),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("summary", sa.String(512), server_default=""),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE")),
        sa.Column("from_role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("is_system", sa.Boolean, server_default=sa.false()),
        sa.Column("is_draft", sa.Boolean, server_default=sa.false()),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "ai_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(16), server_default="medium"),
        sa.Column("status", sa.String(16), server_default="pending"),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text, server_default=""),
        sa.Column("confidence", sa.Numeric(4, 3), server_default="0"),
        sa.Column("agent", sa.String(64), server_default="general"),
        sa.Column("draft_payload", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "calendar_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("who", sa.String(160)),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.Integer),
        sa.Column("priority", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "credit_pulls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="CASCADE")),
        sa.Column("status", sa.String(16), server_default="pending"),
        sa.Column("legal_first_name", sa.String(80)),
        sa.Column("legal_last_name", sa.String(80)),
        sa.Column("dob", sa.Date),
        sa.Column("street", sa.String(255)),
        sa.Column("city", sa.String(160)),
        sa.Column("state", sa.String(2)),
        sa.Column("zip", sa.String(16)),
        sa.Column("phone", sa.String(32)),
        sa.Column("email", sa.String(320)),
        sa.Column("last4_ssn", sa.String(4)),
        sa.Column("fcra_consent", sa.Boolean, server_default=sa.false()),
        sa.Column("fico", sa.Integer),
        sa.Column("bureau_response", postgresql.JSONB),
        sa.Column("notes", sa.Text),
        sa.Column("pulled_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "lenders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(160), unique=True, nullable=False),
        sa.Column("submission_email", sa.String(320)),
        sa.Column("matrix", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "rate_sheet",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku", sa.String(64), unique=True),
        sa.Column("loan_type", sa.String(32), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("base_rate", sa.Numeric(7, 6), nullable=False),
        sa.Column("delta_bps", sa.Integer, server_default="0"),
        sa.Column("max_ltv", sa.Numeric(6, 4)),
        sa.Column("term_months", sa.Integer),
        sa.Column("effective_date", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "vector_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("loans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("deal_id", sa.String(16), index=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB),
        sa.Column("embedding", Vector(1536)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    for tbl in [
        "vector_logs",
        "rate_sheet",
        "lenders",
        "credit_pulls",
        "calendar_events",
        "ai_tasks",
        "messages",
        "activities",
        "hud_line_items",
        "documents",
        "loans",
        "clients",
        "brokers",
        "users",
    ]:
        op.drop_table(tbl)
    op.execute("DROP EXTENSION IF EXISTS vector;")
