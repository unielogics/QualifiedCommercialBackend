"""Client-thread takeover: pause the intake AI while a human is replying.

An underwriter answering the borrower in the client conversation used to run
through the same path as the borrower's own message, so the AI answered the
underwriter. Two columns fix that for good:

* ``bucket_upload_links.ai_paused_until`` — the takeover window. The client
  thread is defined by the upload link (see ``_client_thread_messages`` and
  ``_chat_context``), so the link is the one key every uploader-audience entry
  point already has in hand.
* ``bucket_ai_messages.sender_kind`` — who actually typed the row. Neither
  ``author_name`` nor ``user_id`` can carry this: two different attribution
  strings are in use ("Underwriter — x" and "Underwriter - x"), and a signed-in
  borrower's own message also arrives with ``user_id`` set.

Revision ID: 0183_client_thread_takeover
Revises: 0182_production_package_final
"""

from alembic import op
import sqlalchemy as sa

revision = "0183_client_thread_takeover"
down_revision = "0182_production_package_final"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bucket_upload_links",
        sa.Column("ai_paused_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bucket_ai_messages",
        sa.Column("sender_kind", sa.String(16), nullable=True),
    )
    # Backfill the operator turns already in the client threads so past
    # conversations render as a human reply rather than the borrower's own words.
    # Both attribution spellings, and only on the client-visible audience.
    op.execute(
        """
        UPDATE bucket_ai_messages
           SET sender_kind = 'operator'
         WHERE audience = 'uploader'
           AND role = 'user'
           AND author_name IS NOT NULL
           AND (author_name ILIKE 'underwriter %' OR author_name ILIKE 'underwriter—%'
                OR author_name ILIKE 'underwriter-%' OR author_name = 'Underwriter')
        """
    )


def downgrade() -> None:
    op.drop_column("bucket_ai_messages", "sender_kind")
    op.drop_column("bucket_upload_links", "ai_paused_until")
