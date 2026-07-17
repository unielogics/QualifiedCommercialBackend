"""Per-mailbox inbound/outbound email store (the isolated Workspace inbox).

One row per Gmail message synced from a connected Workspace mailbox. Distinct from
`Message` (loan-scoped chat rows with a fixed from_role enum): EmailMessage is
keyed to a mailbox OWNER, threads by gmail_thread_id, carries client-level linkage
+ an unmatched state, and stores full bodies ENCRYPTED at rest.

Isolation rules (see the Phase-4 plan):
1. Owner-only — every read is scoped `owner_user_id == current_user.id`.
2. The shared loan/client feed shows a body-less `email.tracked` Activity breadcrumb;
   the body lives ONLY here and is served only to the owner.
3. body_text_enc / body_html_enc are ciphertext (Fernet/KMS via provider_secrets);
   subject + snippet stay plaintext so the list/search render without decrypting.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class EmailMessage(TimestampMixin, Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        # Gmail message ids are unique per mailbox — dedup on (mailbox, id).
        UniqueConstraint("mailbox", "gmail_message_id", name="uq_email_messages_mailbox_gmail_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Whose mailbox this was synced from — the isolation key. FK to users so a
    # deleted user's mail is cleaned up with them.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The impersonated Workspace address the message came from (settings.gmail_delegated_user).
    mailbox: Mapped[str] = mapped_column(String(320), nullable=False)

    gmail_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    direction: Mapped[str] = mapped_column(String(12), nullable=False, default="inbound")  # inbound | outbound

    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    to_emails: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cc_emails: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)  # plaintext for list/search
    snippet: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # plaintext preview

    # Bodies ENCRYPTED at rest (ciphertext). encryption_provider is self-describing
    # (fernet | aws_kms), mirroring GoogleAccount.
    body_text_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    encryption_provider: Mapped[str] = mapped_column(String(24), nullable=False, default="fernet")

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Matched linkage — either/both may be null (unmatched → inbox only).
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    matched_party_role: Mapped[str | None] = mapped_column(String(32), nullable=True)  # lender | broker | client | ...

    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    labels: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
