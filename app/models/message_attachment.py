"""Per-message attachment row — backs the lender-thread inbox/outbox
attachment UX. Alembic 0054.

Two outbound flows merge into this table:

  * source='outbound_upload'   the operator drag-and-dropped or
                               browsed-to a fresh file in the
                               composer; we presigned-PUT it to S3
                               and the resulting attachment row
                               lives in `status='staged'` until the
                               operator hits send. On send the
                               reply handler flips status to
                               'committed' and links message_id.
  * source='system_doc_ref'    the operator picked an existing
                               Document from the loan vault; we
                               create an attachment row that
                               re-references the same s3_key so
                               the file isn't duplicated on S3.
                               document_id is populated for
                               traceability.

Inbound is simpler — source='inbound_lender', written directly with
message_id set in the same transaction as the Message row by the
Gmail poller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.loan import Loan
    from app.models.message import Message
    from app.models.user import User


class MessageAttachment(Base):
    __tablename__ = "message_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    loan_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL while a fresh upload sits in 'staged' before the operator
    # hits send. Always set on inbound rows.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Populated only for source='system_doc_ref' — preserves the
    # provenance back to the originating Document so the docs tab
    # shows that this file was sent out via the lender thread.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(
        String(120), nullable=False, default="application/octet-stream"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    """'outbound_upload' | 'system_doc_ref' | 'inbound_lender'"""
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    """'outbound' | 'inbound' — denormalized so the timeline can render
    without joining through the matching Message row."""
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staged")
    """'staged' (upload pending or fresh, message_id NULL) |
    'committed' (linked to a Message and shouldn't be GC'd)."""

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    loan: Mapped[Loan] = relationship()
    message: Mapped[Message | None] = relationship(back_populates="attachments")
    document: Mapped[Document | None] = relationship()
    uploader: Mapped[User | None] = relationship(foreign_keys=[uploaded_by])
