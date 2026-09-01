"""Every text message, in one dated ledger.

Until now each call site kept its own partial record: dealer consent links in
DealerAuditLog, intake links as an Activity summary line, re-engagement in an
Activity payload, inbound replies in a JSONL file on the relay. No provider
message id survived into the database anywhere, so tracing a CRM send to a
carrier record meant grepping container logs — and no UI could show "the SMS
history with this client" because no table held it.

This is that table. One row per message, outbound or inbound, whatever the
transport. The per-call-site records stay (they serve their own screens); this
is the spine they all share.

Refused sends are rows too — status "blocked" with the reason. When a rep asks
"why didn't the text go out", the answer should be a record, not an absence.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

#: Lifecycle of an outbound row: queued → sent → delivered / failed.
#: "blocked" means the gate refused it (opt-out, no consent, bad number).
#: Inbound rows are simply "received".
SMS_STATUSES: tuple[str, ...] = (
    "queued", "sent", "delivered", "failed", "blocked", "received",
)

SMS_DIRECTIONS: tuple[str, ...] = ("outbound", "inbound")


class SmsMessage(TimestampMixin, Base):
    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    direction: Mapped[str] = mapped_column(String(10))
    phone_e164: Mapped[str] = mapped_column(String(20), index=True)

    #: Stored for both directions. The one exception: consent-link bodies carry
    #: tokened URLs, so callers there pass a redacted body — the redaction is
    #: the caller's decision because only it knows what is sensitive.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: aws | twilio | android — which transport carried (or refused) it.
    provider: Mapped[str] = mapped_column(String(16), default="")

    #: AWS MessageId / Twilio SID / gateway id. The join key to carrier-side
    #: records, and what delivery-state updates match on.
    provider_message_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    status: Mapped[str] = mapped_column(String(12), index=True)
    #: Operator-facing: the failure reason, the block reason, the gate that said no.
    detail: Mapped[str] = mapped_column(String(300), default="")

    #: Why this message existed: intake_link | reengagement | consent_link |
    #: reply | manual — lets the UI group and label without parsing bodies.
    context: Mapped[str] = mapped_column(String(32), default="", index=True)

    #: Who this conversation is with, when we know. Nullable: an inbound text
    #: from an unknown number is still a record worth keeping.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    #: Set when a carrier delivery receipt confirms arrival — the timestamp a
    #: dispute actually needs, distinct from when we sent it.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SmsMessage {self.direction} {self.phone_e164} {self.status}>"
