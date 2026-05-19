from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class LegalAcceptance(TimestampMixin, Base):
    """Audit record for a user accepting Terms + Privacy at signup.

    One row per acceptance event — when the documents are versioned (effective
    date bumped) we record a fresh row so we can prove which version the user
    agreed to and when. IP + user agent are captured server-side from the
    request, NOT from the client (a client-supplied IP would be useless for
    legal/TCPA purposes).
    """

    __tablename__ = "legal_acceptances"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Plain-text effective date strings (e.g. "2026-05-19") so we don't have
    # to maintain a separate document_versions table for v1.
    terms_version: Mapped[str] = mapped_column(String(32), nullable=False)
    privacy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Funding/AI/Communications Disclosure version. Nullable because rows
    # written before the v1.0 deploy (when this doc didn't exist yet) only
    # captured terms + privacy; older clients can also still POST without it.
    disclosure_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
