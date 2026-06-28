"""Add bucket AI review, chat, and routed action items.

Revision ID: 0084_bucket_ai_workspace
Revises: 0083_bucket_file_soft_delete
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0084_bucket_ai_workspace"
down_revision = "0083_bucket_file_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("buckets", sa.Column("ai_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("bucket_upload_links", sa.Column("can_use_ai_chat", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_upload_links", sa.Column("can_view_ai_tasks", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_shares", sa.Column("can_use_ai_chat", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_shares", sa.Column("can_view_ai_summary", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_shares", sa.Column("can_view_ai_tasks", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("bucket_shares", sa.Column("can_propose_tasks", sa.Boolean(), nullable=False, server_default="true"))

    op.create_table(
        "bucket_ai_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("file_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bucket_ai_reviews_bucket_id", "bucket_ai_reviews", ["bucket_id"])
    op.create_index("ix_bucket_ai_reviews_status_created", "bucket_ai_reviews", ["status", "created_at"])

    op.create_table(
        "bucket_ai_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("upload_link_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("share_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("audience", sa.String(length=24), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("author_name", sa.String(length=180), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("proposed_context_patch", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["share_id"], ["bucket_shares.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["upload_link_id"], ["bucket_upload_links.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bucket_ai_messages_bucket_id", "bucket_ai_messages", ["bucket_id"])
    op.create_index("ix_bucket_ai_messages_upload_link_id", "bucket_ai_messages", ["upload_link_id"])
    op.create_index("ix_bucket_ai_messages_share_id", "bucket_ai_messages", ["share_id"])

    op.create_table(
        "bucket_ai_action_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("upload_link_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("share_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="proposed", nullable=False),
        sa.Column("route", sa.String(length=24), server_default="admin", nullable=False),
        sa.Column("title", sa.String(length=220), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=24), server_default="ai", nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["bucket_id"], ["buckets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["file_id"], ["bucket_files.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_document_id"], ["bucket_requested_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["share_id"], ["bucket_shares.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_message_id"], ["bucket_ai_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["upload_link_id"], ["bucket_upload_links.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bucket_ai_action_items_bucket_id", "bucket_ai_action_items", ["bucket_id"])
    op.create_index("ix_bucket_ai_action_items_status", "bucket_ai_action_items", ["status"])
    op.create_index("ix_bucket_ai_action_items_route", "bucket_ai_action_items", ["route"])
    op.create_index("ix_bucket_ai_action_items_upload_link_id", "bucket_ai_action_items", ["upload_link_id"])
    op.create_index("ix_bucket_ai_action_items_share_id", "bucket_ai_action_items", ["share_id"])
    op.create_index("ix_bucket_ai_action_items_file_id", "bucket_ai_action_items", ["file_id"])
    op.create_index("ix_bucket_ai_action_items_requested_document_id", "bucket_ai_action_items", ["requested_document_id"])


def downgrade() -> None:
    op.drop_index("ix_bucket_ai_action_items_requested_document_id", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_file_id", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_share_id", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_upload_link_id", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_route", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_status", table_name="bucket_ai_action_items")
    op.drop_index("ix_bucket_ai_action_items_bucket_id", table_name="bucket_ai_action_items")
    op.drop_table("bucket_ai_action_items")

    op.drop_index("ix_bucket_ai_messages_share_id", table_name="bucket_ai_messages")
    op.drop_index("ix_bucket_ai_messages_upload_link_id", table_name="bucket_ai_messages")
    op.drop_index("ix_bucket_ai_messages_bucket_id", table_name="bucket_ai_messages")
    op.drop_table("bucket_ai_messages")

    op.drop_index("ix_bucket_ai_reviews_status_created", table_name="bucket_ai_reviews")
    op.drop_index("ix_bucket_ai_reviews_bucket_id", table_name="bucket_ai_reviews")
    op.drop_table("bucket_ai_reviews")

    op.drop_column("bucket_shares", "can_propose_tasks")
    op.drop_column("bucket_shares", "can_view_ai_tasks")
    op.drop_column("bucket_shares", "can_view_ai_summary")
    op.drop_column("bucket_shares", "can_use_ai_chat")
    op.drop_column("bucket_upload_links", "can_view_ai_tasks")
    op.drop_column("bucket_upload_links", "can_use_ai_chat")
    op.drop_column("buckets", "ai_context")
