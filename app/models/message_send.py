"""One row per message this system sends, including the ones it did not send.

Modelled on `sms_messages`, which has had this shape since 0169 and proves it:
a blocked or failed send is a row, not an absence. An audit page whose only
evidence is the successes is not an audit page.

The body is ciphertext. `encryption_provider` is self-describing so a key
rotation does not orphan old rows — the same convention `email_messages`,
`google_accounts` and `buckets` already use.

`secrets_masked` records that the stored copy is not byte-identical to what
went on the wire: a room PIN or a signing token was replaced before storage, so
reading this table cannot open anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app import request_context
from app.db import Base

#: Extends the SMS vocabulary rather than inventing a second one, so the two
#: tables read as one list. `blocked` means we refused to send it.
STATUSES = ("queued", "sent", "delivered", "bounced", "complained", "failed", "blocked", "received")


class MessageSend(Base):
    __tablename__ = "message_sends"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="outbound")
    context: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    template_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    to_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    to_phone: Mapped[str | None] = mapped_column(String(48), nullable=True)
    cc_emails: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)

    body_text_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    encryption_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="fernet")
    secrets_masked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attachment_names: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    provider_message_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    # Same id the audit trails carry, so a message and its cause join exactly.
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, default=lambda: request_context.request_id() or None
    )
    job: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Who may see it. The actor when a person sent it; otherwise the operator
    #: who owns the subject file. NULL means nobody owns it — a cron send with
    #: no subject — and those are super-admin only.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    # Deliberately not foreign keys: these point into four different subsystems
    # and a message must outlive the file it was about.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    dealer_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    loan_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    intake_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
