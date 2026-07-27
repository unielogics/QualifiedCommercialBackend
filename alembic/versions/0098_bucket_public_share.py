"""Bucket public share links.

Adds bucket_public_shares + bucket_public_share_files — a share link that
requires neither login nor an access code (unlike bucket_shares, whose
passcode_hash is NOT NULL). Purely additive; no existing bucket-sharing
tables are touched.

Revision ID: 0098_bucket_public_share
Revises: 0097_agent_reassignment_audit
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0098_bucket_public_share"
down_revision = "0097_agent_reassignment_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_public_shares",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(96), nullable=False),
        sa.Column("recipient_name", sa.String(180), nullable=True),
        sa.Column("can_preview", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("can_download", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("token", name="uq_bucket_public_shares_token"),
    )
    op.create_index("ix_bucket_public_shares_bucket_id", "bucket_public_shares", ["bucket_id"])
    op.create_index("ix_bucket_public_shares_token", "bucket_public_shares", ["token"])

    op.create_table(
        "bucket_public_share_files",
        sa.Column("public_share_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_public_shares.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("file_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bucket_public_share_files_file_id", "bucket_public_share_files", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_bucket_public_share_files_file_id", table_name="bucket_public_share_files")
    op.drop_table("bucket_public_share_files")
    op.drop_index("ix_bucket_public_shares_token", table_name="bucket_public_shares")
    op.drop_index("ix_bucket_public_shares_bucket_id", table_name="bucket_public_shares")
    op.drop_table("bucket_public_shares")
