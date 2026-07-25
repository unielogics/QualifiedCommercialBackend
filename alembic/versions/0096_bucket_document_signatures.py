"""Generic e-sign primitive for requested documents (credit-authorization forms
and any future "have the client sign X" need).

Extends bucket_requested_documents with requires_signature / signature_kind /
template_file_id / signature_document_text. Adds bucket_document_signatures,
mirroring payment_authorizations' typed-name + canvas-signature + rendered
certificate pattern (minus Stripe fields) — the rendered certificate is stored
as a normal BucketFile that also satisfies the requested-document checklist
via the existing upload-driven status recalculation, so signing IS uploading.

Revision ID: 0096_bucket_document_signatures
Revises: 0095_email_messages
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0096_bucket_document_signatures"
down_revision = "0095_email_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bucket_requested_documents",
        sa.Column("requires_signature", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "bucket_requested_documents",
        sa.Column("signature_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "bucket_requested_documents",
        sa.Column("template_file_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "bucket_requested_documents",
        sa.Column("signature_document_text", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_bucket_requested_documents_template_file_id",
        "bucket_requested_documents",
        "bucket_files",
        ["template_file_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "bucket_document_signatures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("requested_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_version", sa.String(length=32), nullable=False),
        sa.Column("document_hash", sa.String(length=128), nullable=False),
        sa.Column("typed_name", sa.String(length=160), nullable=False),
        sa.Column("esign_consent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("signature_s3_key", sa.String(length=512), nullable=True),
        sa.Column("signature_hash", sa.String(length=128), nullable=True),
        sa.Column("certificate_s3_key", sa.String(length=512), nullable=True),
        sa.Column("certificate_hash", sa.String(length=128), nullable=True),
        sa.Column("applicant_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["requested_document_id"], ["bucket_requested_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["result_file_id"], ["bucket_files.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_bucket_document_signatures_requested_document_id",
        "bucket_document_signatures",
        ["requested_document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_bucket_document_signatures_requested_document_id", table_name="bucket_document_signatures")
    op.drop_table("bucket_document_signatures")
    op.drop_constraint(
        "fk_bucket_requested_documents_template_file_id", "bucket_requested_documents", type_="foreignkey"
    )
    op.drop_column("bucket_requested_documents", "signature_document_text")
    op.drop_column("bucket_requested_documents", "template_file_id")
    op.drop_column("bucket_requested_documents", "signature_kind")
    op.drop_column("bucket_requested_documents", "requires_signature")
