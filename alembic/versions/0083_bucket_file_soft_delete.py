"""Add bucket file soft-delete metadata.

Revision ID: 0083_bucket_file_soft_delete
Revises: 0082_user_booking_settings
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0083_bucket_file_soft_delete"
down_revision = "0082_user_booking_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bucket_files", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bucket_files", sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("bucket_files", sa.Column("delete_storage_status", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        "fk_bucket_files_deleted_by_user_id_users",
        "bucket_files",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_files_deleted_at", "bucket_files", ["deleted_at"])
    op.create_index("ix_bucket_files_bucket_deleted_at", "bucket_files", ["bucket_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_bucket_files_bucket_deleted_at", table_name="bucket_files")
    op.drop_index("ix_bucket_files_deleted_at", table_name="bucket_files")
    op.drop_constraint("fk_bucket_files_deleted_by_user_id_users", "bucket_files", type_="foreignkey")
    op.drop_column("bucket_files", "delete_storage_status")
    op.drop_column("bucket_files", "deleted_by_user_id")
    op.drop_column("bucket_files", "deleted_at")
