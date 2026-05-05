"""Read-only audit log for the loan timeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.schemas.common import ORMModel


class ActivityRead(ORMModel):
    id: UUID
    loan_id: UUID | None
    actor_id: UUID | None
    actor_label: str | None
    kind: str
    summary: str
    payload: dict[str, Any] | None
    occurred_at: datetime
