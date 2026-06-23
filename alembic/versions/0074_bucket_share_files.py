"""bucket share selected files.

Revision ID: 0074
Revises: 0073
Create Date: 2026-06-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_share_files",
        sa.Column("share_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_shares.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("file_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_files.id", ondelete="CASCADE"), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bucket_share_files_file_id", "bucket_share_files", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_bucket_share_files_file_id", table_name="bucket_share_files")
    op.drop_table("bucket_share_files")
