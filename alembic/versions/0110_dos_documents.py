"""Dealer Capital OS document ingestion — dos_documents.

Revision ID: 0110_dos_documents
Revises: 0109_dos_messaging
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "0110_dos_documents"
down_revision = "0109_dos_messaging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dos_documents",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(260), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.String(400)),  # nullable — S3 archival is best-effort
        sa.Column("kind", sa.String(24), nullable=False, server_default="statement"),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("error", sa.Text()),
        sa.Column("extracted", pg.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_dos_documents_dealer_created", "dos_documents", ["dealer_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_dos_documents_dealer_created", table_name="dos_documents")
    op.drop_table("dos_documents")
