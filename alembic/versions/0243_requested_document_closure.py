"""Let a requested document be closed, and close the ones archived files left open.

A requested document only ever knew two states, requested and uploaded, so
archiving a bucket left every open ask standing. In production 142 requests
sit "open" on archived buckets, which inflates the desk's checklist counts and
keeps dead asks in any outstanding-documents view. Closing is a status with a
reason and a time, so a restore can reopen exactly what the archive closed and
nothing else.

Revision ID: 0243_requested_document_closure
Revises: 0242_chat_action_open_worksheet
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0243_requested_document_closure"
down_revision = "0242_chat_action_open_worksheet"
branch_labels = None
depends_on = None

TABLE = "bucket_requested_documents"
ARCHIVE_REASON = "bucket_archived"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("closed_reason", sa.String(length=40), nullable=True))
    op.create_index(
        "ix_bucket_requested_documents_closed_reason",
        TABLE,
        ["bucket_id", "closed_reason"],
    )
    # Close what the archive should have closed, stamped with the archive's
    # own moment so the record reads as history rather than as a migration.
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE} AS d
               SET status = 'closed',
                   closed_reason = '{ARCHIVE_REASON}',
                   closed_at = COALESCE(b.archived_at, CURRENT_TIMESTAMP)
              FROM buckets AS b
             WHERE b.id = d.bucket_id
               AND b.archived_at IS NOT NULL
               AND d.status = 'requested'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET status = 'requested'
             WHERE status = 'closed' AND closed_reason = '{ARCHIVE_REASON}'
            """
        )
    )
    op.drop_index("ix_bucket_requested_documents_closed_reason", table_name=TABLE)
    op.drop_column(TABLE, "closed_reason")
    op.drop_column(TABLE, "closed_at")
