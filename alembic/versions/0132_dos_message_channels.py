"""dos_messages.channel + dos_ai_messages: four conversations per file

`internal` was carrying two jobs: whether the client can see a row, and which
conversation it belongs to. Splitting them lets notes exist without inventing a
second boolean, and leaves `internal` in place as the safety filter the dealer
portal and QCDashboard already trust.

Backfill is the important part. Every existing internal=true row becomes 'desk'
and every internal=false row becomes 'client', which is exactly what they were.

Revision ID: 0132_dos_message_channels
Revises: 0131_dos_sms_consent
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0132_dos_message_channels"
down_revision = "0131_dos_sms_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dos_messages",
        sa.Column("channel", sa.String(12), nullable=False, server_default="client"),
    )
    op.add_column("dos_messages", sa.Column("edited_at", sa.DateTime(timezone=True)))
    # Existing rows keep exactly the meaning they already had.
    op.execute("UPDATE dos_messages SET channel = 'desk' WHERE internal IS TRUE")
    op.execute("UPDATE dos_messages SET channel = 'client' WHERE internal IS NOT TRUE")
    op.create_index(
        "ix_dos_messages_dealer_channel", "dos_messages", ["dealer_id", "channel", "created_at"]
    )

    op.create_table(
        "dos_ai_messages",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dealer_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("dos_dealers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "ix_dos_ai_messages_thread", "dos_ai_messages", ["dealer_id", "user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_dos_ai_messages_thread", table_name="dos_ai_messages")
    op.drop_table("dos_ai_messages")
    op.drop_index("ix_dos_messages_dealer_channel", table_name="dos_messages")
    op.drop_column("dos_messages", "edited_at")
    op.drop_column("dos_messages", "channel")
