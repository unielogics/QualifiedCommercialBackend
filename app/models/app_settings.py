"""Operator-level configuration.

Single-row table — settings live as a typed JSONB blob keyed by section.
Keeps the schema flexible while we iterate on the Settings UI; we can
normalize into per-section tables later if any section grows complex enough
to warrant indexing.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class AppSettings(Base, TimestampMixin):
    __tablename__ = "app_settings"

    # `singleton: bool` flag with a unique constraint guarantees we only ever
    # have one row. Constraint is enforced at the DB layer (see migration).
    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(default=True, unique=True, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
