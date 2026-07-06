"""Add dealer intake login sessions and ZIP child file metadata.

Revision ID: 0088_dealer_intake_login_zip_files
Revises: 0087_public_underwriting_intakes
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0088_dealer_intake_login_zip_files"
down_revision = "0087_public_underwriting_intakes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_files", sa.Column("parent_zip_file_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_files", sa.Column("zip_entry_path", sa.String(length=700), nullable=True))
    op.add_column("bucket_files", sa.Column("extraction_status", sa.String(length=32), nullable=True))
    op.add_column("bucket_files", sa.Column("extraction_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_bucket_files_parent_zip_file_id",
        "bucket_files",
        "bucket_files",
        ["parent_zip_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_files_parent_zip_file_id", "bucket_files", ["parent_zip_file_id"])

    op.create_table(
        "dealer_intake_login_challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_hash", sa.String(length=96), nullable=False),
        sa.Column("code_hash", sa.String(length=96), nullable=False),
        sa.Column("session_hash", sa.String(length=96), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip_address", sa.String(length=80), nullable=True),
        sa.Column("user_agent", sa.String(length=600), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["intake_id"], ["public_underwriting_intakes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dealer_intake_login_challenges_email_hash", "dealer_intake_login_challenges", ["email_hash"])
    op.create_index("ix_dealer_intake_login_challenges_intake_id", "dealer_intake_login_challenges", ["intake_id"])
    op.create_index(
        "ix_dealer_intake_login_challenges_session_hash",
        "dealer_intake_login_challenges",
        ["session_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_dealer_intake_login_challenges_session_hash", table_name="dealer_intake_login_challenges")
    op.drop_index("ix_dealer_intake_login_challenges_intake_id", table_name="dealer_intake_login_challenges")
    op.drop_index("ix_dealer_intake_login_challenges_email_hash", table_name="dealer_intake_login_challenges")
    op.drop_table("dealer_intake_login_challenges")
    op.drop_index("ix_bucket_files_parent_zip_file_id", table_name="bucket_files")
    op.drop_constraint("fk_bucket_files_parent_zip_file_id", "bucket_files", type_="foreignkey")
    op.drop_column("bucket_files", "extraction_reason")
    op.drop_column("bucket_files", "extraction_status")
    op.drop_column("bucket_files", "zip_entry_path")
    op.drop_column("bucket_files", "parent_zip_file_id")
