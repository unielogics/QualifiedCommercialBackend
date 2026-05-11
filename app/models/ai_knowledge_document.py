"""Per-agent knowledge documents (PDFs / pasted FAQ) the AI uses as
context when speaking to leads or borrowers.

Lifecycle:
  1. Agent picks a file in /agent-settings/ai → frontend calls
     POST /me/ai-knowledge/upload-init → backend creates a row in
     status='uploading' + presigned PUT URL.
  2. Frontend PUTs the file to S3.
  3. Frontend calls POST /me/ai-knowledge/upload-complete → backend
     flips status='parsing', then parses the PDF inline (pypdf) and
     writes `parsed_text` + status='ready' (or 'failed' + error).
  4. The agent's AI chat assembly injects parsed_text into the system
     prompt — see services/ai/agent_settings + services/ai/context.

Soft-delete: `deleted_at` is set on DELETE; the parse layer skips
deleted rows. We keep the row + S3 object until a hard-delete cron
runs (TBD), which is friendlier to "I deleted that by accident".

The owner is the AGENT user id (not brokers.id) for symmetry with
AIPlaybookTemplate's `owner_id` column — both are users.id."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    pass


class AIKnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "ai_knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Original filename shown in the UI. Not used for storage.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # S3 key under the configured bucket — namespaced by agent so a
    # rogue listing call can't see another agent's docs.
    s3_key: Mapped[str] = mapped_column(String(512), nullable=False)

    # Extracted plain text. Truncated to ~200k chars on the parse
    # path — agents won't paste novels and the prompt has its own
    # budget on top of this.
    parsed_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 'uploading' (init), 'parsing' (post-complete), 'ready', 'failed'.
    # Kept as a plain string (no enum) so future statuses don't need
    # a migration — same pattern as Document.status pre-Phase 4.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploading")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Soft-delete — load_agent_knowledge() filters out non-null.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
