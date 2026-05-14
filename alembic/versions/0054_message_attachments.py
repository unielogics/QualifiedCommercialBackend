"""message_attachments — files attached to lender-thread messages.

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-14

Powers attachment support on the LenderThread composer (outbound) and
the Gmail inbound poller (inbound). Sources:
  - 'outbound_upload'  uploaded fresh from the composer (browser file
                       picker → presigned S3 PUT → upload-complete)
  - 'system_doc_ref'   chose an existing Document from the loan's
                       vault; we duplicate the row here but reuse
                       the Document.s3_key (no second copy on S3)
  - 'inbound_lender'   pulled out of an incoming Gmail message by
                       the inbound poller

`message_id` is NULLABLE so freshly-uploaded outbound attachments
can exist in a "staged" state before the operator hits send. On
send, the reply handler updates message_id to point at the new
Message row. Inbound attachments are written with message_id set
in the same transaction as the Message row.

`document_id` is only set for source='system_doc_ref' so we can
keep the link back to the source vault file for traceability.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_attachments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False, server_default="application/octet-stream"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="staged",
        ),  # 'staged' | 'committed'
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_message_attachments_loan_id",
        "message_attachments",
        ["loan_id"],
    )
    op.create_index(
        "ix_message_attachments_message_id",
        "message_attachments",
        ["message_id"],
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.drop_index("ix_message_attachments_loan_id", table_name="message_attachments")
    op.drop_table("message_attachments")
