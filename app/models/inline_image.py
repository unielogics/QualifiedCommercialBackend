"""An image pasted into a note or an internal message.

Notes and internal messages live in four different tables — a JSON entry on a
deal, a `bucket_notes` row, a `dealer_messages` row on the file conversation,
an appointment activity row. Not one of them can hold a picture, and giving
each its own attachment column would be four migrations and four upload paths
for a single behaviour.

So the image is keyed by (subject_kind, subject_id) instead. `subject_id` is a
string rather than a UUID because deal note entries are elements of a JSONB
array and carry a client-generated id, not a database key.

Bytes never pass through the API. The browser gets a presigned PUT and ships
them straight to S3, the way lender attachments already work; a row is `staged`
between the presign and the upload finishing. Staged rows are invisible: a read
returns only rows that are `ready` AND bound to a subject, so an upload that is
abandoned halfway leaves nothing behind in anyone's UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class InlineImage(TimestampMixin, Base):
    __tablename__ = "inline_images"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Which kind of thing this hangs off. See SUBJECT_KINDS in the service —
    #: the set is closed, so a typo cannot quietly create a new namespace.
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Null until the note or message it belongs to actually exists.
    subject_id: Mapped[str | None] = mapped_column(String(64))
    s3_key: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    #: staged — presigned, bytes may not have landed yet. ready — uploaded.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="staged", server_default="staged")
    #: When it was bound to its subject. A ready row with no subject is an
    #: upload the author abandoned, and is safe to sweep.
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_inline_images_subject", "subject_kind", "subject_id"),
        Index("ix_inline_images_uploader", "uploaded_by_user_id", "status"),
    )
