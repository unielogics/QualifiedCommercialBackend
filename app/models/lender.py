from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class Lender(TimestampMixin, Base):
    __tablename__ = "lenders"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    submission_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    matrix: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
