"""sms_messages — every text, one dated ledger

Each SMS call site kept its own partial record and none kept the provider
message id, so no screen could show a client's SMS history and no row could be
traced to a carrier record. This table is the shared spine: one row per
message, both directions, all transports, refused sends included (status
"blocked" with the reason — an absence is not an audit answer).

client_id is SET NULL on delete: the ledger outlives the client row because
the compliance question "what did we send to this number" outlives it too.

Revision ID: 0169_sms_messages
Revises: 0168_sms_opt_out
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0169_sms_messages"
down_revision = "0168_sms_opt_out"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_messages",
        sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("phone_e164", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("provider_message_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("detail", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("context", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "client_id", PG_UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # The two reads the UI makes: "this client's thread, newest first" and
    # "everything to/from this number". Status + context feed list filters.
    op.create_index("ix_sms_messages_client_created", "sms_messages", ["client_id", "created_at"])
    op.create_index("ix_sms_messages_phone_e164", "sms_messages", ["phone_e164"])
    op.create_index("ix_sms_messages_provider_message_id", "sms_messages", ["provider_message_id"])
    op.create_index("ix_sms_messages_status", "sms_messages", ["status"])
    op.create_index("ix_sms_messages_context", "sms_messages", ["context"])


def downgrade() -> None:
    for ix in (
        "ix_sms_messages_context", "ix_sms_messages_status",
        "ix_sms_messages_provider_message_id", "ix_sms_messages_phone_e164",
        "ix_sms_messages_client_created",
    ):
        op.drop_index(ix, table_name="sms_messages")
    op.drop_table("sms_messages")
