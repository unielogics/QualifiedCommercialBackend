"""Dealer Capital OS Phase 2 — link dealers/documents to the Buckets system.

dos_dealers.bucket_id: the dealer's linked document Bucket (adopted from the
AI-underwriter intake matched by email, or lazily created as an audit bucket).
dos_documents.bucket_file_id: mirror BucketFile for a Dealer OS document
(push), or the source BucketFile an ingest pulled from (pull).

Both are nullable with ON DELETE SET NULL — bucket-side deletions must never
cascade into Dealer OS rows.

Revision ID: 0111_dos_bucket_link
Revises: 0110_dos_documents
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0111_dos_bucket_link"
down_revision = "0110_dos_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dos_dealers", sa.Column("bucket_id", pg.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_dos_dealers_bucket_id",
        "dos_dealers",
        "buckets",
        ["bucket_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("dos_documents", sa.Column("bucket_file_id", pg.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_dos_documents_bucket_file_id",
        "dos_documents",
        "bucket_files",
        ["bucket_file_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_dos_documents_bucket_file_id", "dos_documents", type_="foreignkey")
    op.drop_column("dos_documents", "bucket_file_id")
    op.drop_constraint("fk_dos_dealers_bucket_id", "dos_dealers", type_="foreignkey")
    op.drop_column("dos_dealers", "bucket_id")
