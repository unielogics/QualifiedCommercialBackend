"""buckets document rooms.

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buckets",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("bucket_type", sa.String(80), nullable=True),
        sa.Column("client_name", sa.String(180), nullable=True),
        sa.Column("purpose", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="collecting_documents"),
        sa.Column("created_by_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_buckets_created_by_id", "buckets", ["created_by_id"])

    op.create_table(
        "bucket_document_templates",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("name", name="uq_bucket_document_templates_name"),
    )

    op.create_table(
        "bucket_requested_documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("template_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_document_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("is_custom", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_bucket_requested_documents_bucket_id", "bucket_requested_documents", ["bucket_id"])

    op.create_table(
        "bucket_upload_links",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(96), nullable=False),
        sa.Column("recipient_name", sa.String(180), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allow_notes", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("allow_multiple_sessions", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("passcode_hash", sa.String(255), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token", name="uq_bucket_upload_links_token"),
    )
    op.create_index("ix_bucket_upload_links_bucket_id", "bucket_upload_links", ["bucket_id"])
    op.create_index("ix_bucket_upload_links_token", "bucket_upload_links", ["token"])

    op.create_table(
        "bucket_files",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_document_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_requested_documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("upload_link_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_upload_links.id", ondelete="SET NULL"), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("s3_key", sa.String(700), nullable=False),
        sa.Column("content_type", sa.String(160), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by_name", sa.String(180), nullable=True),
        sa.Column("uploaded_by_email", sa.String(320), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploaded"),
    )
    op.create_index("ix_bucket_files_bucket_id", "bucket_files", ["bucket_id"])

    op.create_table(
        "bucket_shares",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(96), nullable=False),
        sa.Column("recipient_name", sa.String(180), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=True),
        sa.Column("passcode_hash", sa.String(255), nullable=False),
        sa.Column("can_preview", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("can_download", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("can_add_notes", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("can_upload", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("can_see_internal_notes", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("token", name="uq_bucket_shares_token"),
    )
    op.create_index("ix_bucket_shares_bucket_id", "bucket_shares", ["bucket_id"])
    op.create_index("ix_bucket_shares_token", "bucket_shares", ["token"])

    op.create_table(
        "bucket_notes",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("share_id", pg.UUID(as_uuid=True), sa.ForeignKey("bucket_shares.id", ondelete="SET NULL"), nullable=True),
        sa.Column("author_name", sa.String(180), nullable=False),
        sa.Column("author_role", sa.String(40), nullable=False),
        sa.Column("visibility", sa.String(24), nullable=False, server_default="admin"),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index("ix_bucket_notes_bucket_id", "bucket_notes", ["bucket_id"])

    op.create_table(
        "bucket_activity_logs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("bucket_id", pg.UUID(as_uuid=True), sa.ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_name", sa.String(180), nullable=True),
        sa.Column("actor_role", sa.String(40), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(60), nullable=True),
        sa.Column("target_id", sa.String(80), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bucket_activity_logs_bucket_id", "bucket_activity_logs", ["bucket_id"])

    templates = [
        ("Last 6 months business bank statements", "Business Financials"),
        ("Last 2 years business tax returns", "Tax Returns"),
        ("Last 2 years personal tax returns", "Tax Returns"),
        ("Year-to-date profit and loss", "Business Financials"),
        ("Balance sheet", "Business Financials"),
        ("Personal financial statement", "Personal Financials"),
        ("Driver license", "Identity"),
        ("Entity documents", "Entity"),
        ("Purchase contract", "Property"),
        ("Lease agreement", "Property"),
        ("Insurance documents", "Insurance"),
        ("Property tax bill", "Property"),
        ("Rent roll", "Property"),
        ("Mortgage statement", "Debt"),
        ("Credit authorization", "Authorization"),
    ]
    for name, category in templates:
        op.execute(
            sa.text(
                "INSERT INTO bucket_document_templates (id, name, category) "
                "VALUES (gen_random_uuid(), :name, :category) "
                "ON CONFLICT (name) DO NOTHING"
            ).bindparams(name=name, category=category)
        )


def downgrade() -> None:
    op.drop_index("ix_bucket_activity_logs_bucket_id", table_name="bucket_activity_logs")
    op.drop_table("bucket_activity_logs")
    op.drop_index("ix_bucket_notes_bucket_id", table_name="bucket_notes")
    op.drop_table("bucket_notes")
    op.drop_index("ix_bucket_shares_token", table_name="bucket_shares")
    op.drop_index("ix_bucket_shares_bucket_id", table_name="bucket_shares")
    op.drop_table("bucket_shares")
    op.drop_index("ix_bucket_files_bucket_id", table_name="bucket_files")
    op.drop_table("bucket_files")
    op.drop_index("ix_bucket_upload_links_token", table_name="bucket_upload_links")
    op.drop_index("ix_bucket_upload_links_bucket_id", table_name="bucket_upload_links")
    op.drop_table("bucket_upload_links")
    op.drop_index("ix_bucket_requested_documents_bucket_id", table_name="bucket_requested_documents")
    op.drop_table("bucket_requested_documents")
    op.drop_table("bucket_document_templates")
    op.drop_index("ix_buckets_created_by_id", table_name="buckets")
    op.drop_table("buckets")
