"""Images pasted into a note or an internal message.

Four places take a note or an internal message and not one of them could hold a
picture: deal notes are a JSONB array on the deal, lead notes are bucket_notes
rows, the file conversation is dealer_messages, appointment notes are activity
rows. Rather than four attachment columns and four upload paths, one table keyed
by (subject_kind, subject_id).

subject_id is a string, not a UUID, because a deal note entry is an element of a
JSONB array with a client-generated id and no database key of its own.

Revision ID: 0188_inline_images
Revises: 0187_booking_delivery_error_at
"""

import sqlalchemy as sa

from alembic import op

revision = "0188_inline_images"
down_revision = "0187_booking_delivery_error_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inline_images",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject_kind", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("s3_key", sa.String(500), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "uploaded_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="staged"),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_inline_images_subject", "inline_images", ["subject_kind", "subject_id"])
    op.create_index("ix_inline_images_uploader", "inline_images", ["uploaded_by_user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_inline_images_uploader", table_name="inline_images")
    op.drop_index("ix_inline_images_subject", table_name="inline_images")
    op.drop_table("inline_images")
