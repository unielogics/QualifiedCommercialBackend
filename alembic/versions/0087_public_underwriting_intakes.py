"""Add public underwriting intake table.

Revision ID: 0087_public_underwriting_intakes
Revises: 0086_billing_preauthorization
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0087_public_underwriting_intakes"
down_revision = "0086_billing_preauthorization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_underwriting_intakes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_upload_link_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("latest_review_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_hash", sa.String(length=96), nullable=False),
        sa.Column("variant", sa.String(length=64), server_default="dealer_financing_v1", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="collecting", nullable=False),
        sa.Column("full_name", sa.String(length=180), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("phone", sa.String(length=48), nullable=True),
        sa.Column("business_name", sa.String(length=180), nullable=True),
        sa.Column("loan_purpose", sa.String(length=255), nullable=True),
        sa.Column("requested_loan_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("estimated_credit_score", sa.Integer(), nullable=True),
        sa.Column("referral_source", sa.String(length=180), nullable=True),
        sa.Column("asset_rows", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("intake_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bucket_upload_link_id"], ["bucket_upload_links.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["latest_review_id"], ["bucket_ai_reviews.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_underwriting_intakes_bucket_id", "public_underwriting_intakes", ["bucket_id"])
    op.create_index("ix_public_underwriting_intakes_bucket_upload_link_id", "public_underwriting_intakes", ["bucket_upload_link_id"])
    op.create_index("ix_public_underwriting_intakes_client_id", "public_underwriting_intakes", ["client_id"])
    op.create_index("ix_public_underwriting_intakes_email", "public_underwriting_intakes", ["email"])
    op.create_index("ix_public_underwriting_intakes_latest_review_id", "public_underwriting_intakes", ["latest_review_id"])
    op.create_index("ix_public_underwriting_intakes_status", "public_underwriting_intakes", ["status"])
    op.create_index("ix_public_underwriting_intakes_token_hash", "public_underwriting_intakes", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_public_underwriting_intakes_token_hash", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_status", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_latest_review_id", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_email", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_client_id", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_bucket_upload_link_id", table_name="public_underwriting_intakes")
    op.drop_index("ix_public_underwriting_intakes_bucket_id", table_name="public_underwriting_intakes")
    op.drop_table("public_underwriting_intakes")
