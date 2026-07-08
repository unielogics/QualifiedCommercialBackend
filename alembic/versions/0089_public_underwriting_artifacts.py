"""Add public underwriting artifacts and vendor email audit.

Revision ID: 0089_public_underwriting_artifacts
Revises: 0088_dealer_intake_login_zip_files
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0089_public_underwriting_artifacts"
down_revision = "0088_dealer_intake_login_zip_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_underwriting_intake_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=48), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("s3_key", sa.String(length=700), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["intake_id"], ["public_underwriting_intakes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_underwriting_artifacts_created_by", "public_underwriting_intake_artifacts", ["created_by_user_id"])
    op.create_index("ix_public_underwriting_artifacts_intake_type", "public_underwriting_intake_artifacts", ["intake_id", "artifact_type"])
    op.create_index("ix_public_underwriting_artifacts_type", "public_underwriting_intake_artifacts", ["artifact_type"])

    op.create_table(
        "public_underwriting_intake_email_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("executive_summary_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lender_packet_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_emails", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cc_emails", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("vendor_access_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ses_status", sa.String(length=40), server_default="pending", nullable=False),
        sa.Column("ses_message_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ses_error", sa.Text(), nullable=True),
        sa.Column("sent_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["executive_summary_artifact_id"], ["public_underwriting_intake_artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["intake_id"], ["public_underwriting_intakes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lender_packet_artifact_id"], ["public_underwriting_intake_artifacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sent_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_public_underwriting_email_sends_intake", "public_underwriting_intake_email_sends", ["intake_id"])
    op.create_index("ix_public_underwriting_email_sends_sent_by", "public_underwriting_intake_email_sends", ["sent_by_user_id"])
    op.create_index("ix_public_underwriting_email_sends_status", "public_underwriting_intake_email_sends", ["ses_status"])


def downgrade() -> None:
    op.drop_index("ix_public_underwriting_email_sends_status", table_name="public_underwriting_intake_email_sends")
    op.drop_index("ix_public_underwriting_email_sends_sent_by", table_name="public_underwriting_intake_email_sends")
    op.drop_index("ix_public_underwriting_email_sends_intake", table_name="public_underwriting_intake_email_sends")
    op.drop_table("public_underwriting_intake_email_sends")
    op.drop_index("ix_public_underwriting_artifacts_type", table_name="public_underwriting_intake_artifacts")
    op.drop_index("ix_public_underwriting_artifacts_intake_type", table_name="public_underwriting_intake_artifacts")
    op.drop_index("ix_public_underwriting_artifacts_created_by", table_name="public_underwriting_intake_artifacts")
    op.drop_table("public_underwriting_intake_artifacts")
