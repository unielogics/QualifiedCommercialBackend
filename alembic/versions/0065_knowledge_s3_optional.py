"""ai_knowledge_documents.s3_key — make optional for pasted text notes.

The AI Agent builder's Knowledge step now accepts pasted text in
addition to file uploads. Pasted items have no S3 object — the body
lives in `parsed_text` directly. Relax the NOT NULL on `s3_key` so a
row can represent either kind of input.

Revision ID: 0065
Revises: 0064
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ai_knowledge_documents",
        "s3_key",
        existing_type=sa.String(length=512),
        nullable=True,
    )


def downgrade() -> None:
    # Pasted rows have NULL s3_key — they would violate NOT NULL on
    # re-tightening. Backfill them with an empty string so the column
    # constraint can be restored without losing rows.
    op.execute(
        "UPDATE ai_knowledge_documents SET s3_key = '' WHERE s3_key IS NULL"
    )
    op.alter_column(
        "ai_knowledge_documents",
        "s3_key",
        existing_type=sa.String(length=512),
        nullable=False,
    )
