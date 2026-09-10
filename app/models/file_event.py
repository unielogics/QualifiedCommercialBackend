"""One line on a file's timeline.

Every meaningful write on a file — a document asked for or received, a
message, a status move, a note, a review, a seat change — appends a row here
through `services/file_events.emit`. `visibility` is the tier that may read
it: `client` (everyone on the file including the client), `team` (the agent,
the underwriters, the company and the desk) or `desk` (underwriters and the
desk). The row never carries a message body or a review body; it says what
happened, not what was said.

`notice_status` drives the email drain: `pending` rows are batched into one
email per person per file, `none` rows never email (desk tier).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app import request_context
from app.db import Base

VISIBILITY_CLIENT = "client"
VISIBILITY_TEAM = "team"
VISIBILITY_DESK = "desk"
VISIBILITIES = (VISIBILITY_CLIENT, VISIBILITY_TEAM, VISIBILITY_DESK)

NOTICE_PENDING = "pending"
NOTICE_SENT = "sent"
NOTICE_SKIPPED = "skipped"
NOTICE_NONE = "none"


class FileEvent(Base):
    __tablename__ = "file_events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    visibility: Mapped[str] = mapped_column(String(12), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    #: Filled by a column default so no writer can forget it; joins the row to
    #: the request that caused it, like every other ledger here.
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=lambda: request_context.request_id() or None
    )
    notice_status: Mapped[str] = mapped_column(String(16), nullable=False, default=NOTICE_PENDING, server_default=NOTICE_PENDING)
    notice_recipients: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    notice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_file_events_profile_created", "profile_id", "created_at"),
        Index("ix_file_events_notice", "notice_status", "created_at"),
    )

