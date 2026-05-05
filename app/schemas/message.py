from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.enums import MessageFrom
from app.schemas.common import ORMModel


class MessageCreate(BaseModel):
    loan_id: UUID
    body: str
    from_role: MessageFrom = MessageFrom.BROKER
    is_draft: bool = False


class MessageRead(ORMModel):
    id: UUID
    loan_id: UUID
    from_role: MessageFrom
    body: str
    is_system: bool
    is_draft: bool
    sent_at: datetime
