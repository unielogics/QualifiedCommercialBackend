"""Loan-chat attachment — lets a client send a note + file in one turn.

Adds nullable attachment_document_id FK to loan_chat_messages. Plain
text messages leave it NULL, so existing behavior is unchanged.

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loan_chat_messages",
        sa.Column(
            "attachment_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_loan_chat_messages_attachment_document_id",
        "loan_chat_messages",
        ["attachment_document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_loan_chat_messages_attachment_document_id",
        table_name="loan_chat_messages",
    )
    op.drop_column("loan_chat_messages", "attachment_document_id")
