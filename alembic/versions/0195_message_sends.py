"""Every message this system sends, and what it actually said.

SMS has had this since 0169: `sms_messages` keeps the body, the provider, the
carrier's id, the status and the reason, and writes a row whether the send
succeeded, failed or was refused. Email has never had an equivalent. Of the
forty-one candidate tables in this database exactly one was a purpose-built
outbound email record, for a single feature, holding a single row — so the
honest answer to "what did we send them?" was usually "we don't know".

Eleven send paths recorded nothing at all. Nine more recorded that something
went but not what it said.

This is the spine. It deliberately does not swallow `sms_messages`: that table
works, three surfaces read it, and the read API unions the two rather than
disturbing a working system.

Bodies are encrypted at rest with the same Fernet/KMS path the Gmail sync uses,
and secrets are masked out of the copy before it is stored, so a leaked log
cannot open a room.

Revision ID: 0195_message_sends
Revises: 0194_audit_request_id
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0195_message_sends"
down_revision = "0194_audit_request_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # what and where
        sa.Column("channel", sa.String(16), nullable=False),          # email | sms
        sa.Column("direction", sa.String(16), nullable=False, server_default="outbound"),
        sa.Column("context", sa.String(48), nullable=False, server_default=""),
        sa.Column("template_key", sa.String(64), nullable=True),
        sa.Column("to_email", sa.String(320), nullable=True),
        sa.Column("to_phone", sa.String(48), nullable=True),
        sa.Column("cc_emails", postgresql.JSONB, nullable=True),
        sa.Column("subject", sa.String(512), nullable=True),
        # the body, encrypted, with secrets already removed from the copy
        sa.Column("body_text_enc", sa.Text, nullable=True),
        sa.Column("body_html_enc", sa.Text, nullable=True),
        sa.Column("encryption_provider", sa.String(24), nullable=False, server_default="fernet"),
        sa.Column("secrets_masked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attachment_names", postgresql.JSONB, nullable=True),
        # the wire
        sa.Column("provider", sa.String(24), nullable=False, server_default=""),
        sa.Column("provider_message_id", sa.String(160), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("detail", sa.String(500), nullable=False, server_default=""),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        # who, why, and about whom
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_label", sa.String(24), nullable=False, server_default="system"),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("job", sa.String(64), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dealer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("loan_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # The page reads newest-first, filtered. The provider id is how a delivery
    # event finds its row; the request id is how a message finds its cause.
    op.create_index("ix_message_sends_created_at", "message_sends", ["created_at"])
    op.create_index("ix_message_sends_provider_message_id", "message_sends", ["provider_message_id"])
    op.create_index("ix_message_sends_request_id", "message_sends", ["request_id"])
    op.create_index("ix_message_sends_owner", "message_sends", ["owner_user_id", "created_at"])
    op.create_index("ix_message_sends_client", "message_sends", ["client_id", "created_at"])
    op.create_index("ix_message_sends_channel_status", "message_sends", ["channel", "status"])


def downgrade() -> None:
    op.drop_table("message_sends")
