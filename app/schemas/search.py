from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

ResultKind = Literal["client", "loan", "doc", "message", "event", "ai_task"]


class SearchResult(BaseModel):
    kind: ResultKind
    id: UUID
    title: str
    subtitle: str | None = None
    client_id: UUID | None = None
    loan_id: UUID | None = None


class GroupedResults(BaseModel):
    """Results grouped by client (matches the desktop GlobalSearch UX)."""
    client_id: UUID
    client_name: str
    items: list[SearchResult]
