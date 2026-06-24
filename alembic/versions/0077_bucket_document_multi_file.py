"""bucket document multi-file flag.

Revision ID: 0077
Revises: 0076
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bucket_document_templates",
        sa.Column("allow_multiple_files", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "bucket_requested_documents",
        sa.Column("allow_multiple_files", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        sa.text(
            """
            UPDATE bucket_document_templates
            SET allow_multiple_files = true
            WHERE name ILIKE '%bank statement%'
               OR name ILIKE '%tax return%'
               OR name ILIKE '%irs%'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE bucket_requested_documents
            SET allow_multiple_files = true
            WHERE name ILIKE '%bank statement%'
               OR name ILIKE '%tax return%'
               OR name ILIKE '%irs%'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("bucket_requested_documents", "allow_multiple_files")
    op.drop_column("bucket_document_templates", "allow_multiple_files")
