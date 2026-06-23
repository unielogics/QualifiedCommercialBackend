"""bucket file annotations.

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_file_annotations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False),
        sa.Column("share_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True),
        sa.Column("page_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("x", sa.Float(), nullable=False),
        sa.Column("y", sa.Float(), nullable=False),
        sa.Column("width", sa.Float(), nullable=False),
        sa.Column("height", sa.Float(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("author_name", sa.String(length=180), nullable=False),
        sa.Column("author_role", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bucket_file_annotations_bucket_id", "bucket_file_annotations", ["bucket_id"])
    op.create_index("ix_bucket_file_annotations_file_id", "bucket_file_annotations", ["file_id"])
    op.create_index("ix_bucket_file_annotations_share_id", "bucket_file_annotations", ["share_id"])


def downgrade() -> None:
    op.drop_index("ix_bucket_file_annotations_share_id", table_name="bucket_file_annotations")
    op.drop_index("ix_bucket_file_annotations_file_id", table_name="bucket_file_annotations")
    op.drop_index("ix_bucket_file_annotations_bucket_id", table_name="bucket_file_annotations")
    op.drop_table("bucket_file_annotations")
