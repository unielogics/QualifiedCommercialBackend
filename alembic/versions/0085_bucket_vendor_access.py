"""Add authenticated vendor bucket access.

Revision ID: 0085_bucket_vendor_access
Revises: 0084_bucket_ai_workspace
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0085_bucket_vendor_access"
down_revision = "0084_bucket_ai_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_vendor_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_scope", sa.String(length=24), server_default="all_active", nullable=False),
        sa.Column("can_preview", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_download", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("can_add_notes", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_see_internal_notes", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("can_use_ai_chat", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_view_ai_summary", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_view_ai_tasks", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("can_propose_tasks", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("download_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_id", "vendor_user_id", name="uq_bucket_vendor_access_bucket_user"),
    )
    op.create_index("ix_bucket_vendor_access_bucket_id", "bucket_vendor_access", ["bucket_id"])
    op.create_index("ix_bucket_vendor_access_vendor_user_id", "bucket_vendor_access", ["vendor_user_id"])
    op.create_index("ix_bucket_vendor_access_status_expires", "bucket_vendor_access", ["status", "expires_at"])

    op.create_table(
        "bucket_vendor_access_files",
        sa.Column("vendor_access_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["bucket_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_access_id"], ["bucket_vendor_access.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vendor_access_id", "file_id"),
    )

    op.add_column("bucket_file_annotations", sa.Column("vendor_access_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_bucket_file_annotations_vendor_access_id",
        "bucket_file_annotations",
        "bucket_vendor_access",
        ["vendor_access_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_file_annotations_vendor_access_id", "bucket_file_annotations", ["vendor_access_id"])

    op.add_column("bucket_notes", sa.Column("vendor_access_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_bucket_notes_vendor_access_id",
        "bucket_notes",
        "bucket_vendor_access",
        ["vendor_access_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("bucket_ai_messages", sa.Column("vendor_access_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_bucket_ai_messages_vendor_access_id",
        "bucket_ai_messages",
        "bucket_vendor_access",
        ["vendor_access_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_ai_messages_vendor_access_id", "bucket_ai_messages", ["vendor_access_id"])

    op.add_column("bucket_ai_action_items", sa.Column("vendor_access_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_bucket_ai_action_items_vendor_access_id",
        "bucket_ai_action_items",
        "bucket_vendor_access",
        ["vendor_access_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_bucket_ai_action_items_vendor_access_id", "bucket_ai_action_items", ["vendor_access_id"])


def downgrade() -> None:
    op.drop_index("ix_bucket_ai_action_items_vendor_access_id", table_name="bucket_ai_action_items")
    op.drop_constraint("fk_bucket_ai_action_items_vendor_access_id", "bucket_ai_action_items", type_="foreignkey")
    op.drop_column("bucket_ai_action_items", "vendor_access_id")

    op.drop_index("ix_bucket_ai_messages_vendor_access_id", table_name="bucket_ai_messages")
    op.drop_constraint("fk_bucket_ai_messages_vendor_access_id", "bucket_ai_messages", type_="foreignkey")
    op.drop_column("bucket_ai_messages", "vendor_access_id")

    op.drop_constraint("fk_bucket_notes_vendor_access_id", "bucket_notes", type_="foreignkey")
    op.drop_column("bucket_notes", "vendor_access_id")

    op.drop_index("ix_bucket_file_annotations_vendor_access_id", table_name="bucket_file_annotations")
    op.drop_constraint("fk_bucket_file_annotations_vendor_access_id", "bucket_file_annotations", type_="foreignkey")
    op.drop_column("bucket_file_annotations", "vendor_access_id")

    op.drop_table("bucket_vendor_access_files")
    op.drop_index("ix_bucket_vendor_access_status_expires", table_name="bucket_vendor_access")
    op.drop_index("ix_bucket_vendor_access_vendor_user_id", table_name="bucket_vendor_access")
    op.drop_index("ix_bucket_vendor_access_bucket_id", table_name="bucket_vendor_access")
    op.drop_table("bucket_vendor_access")
