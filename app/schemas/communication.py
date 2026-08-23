from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CommunicationChannel = Literal[
    "client",
    "underwriter_ai",
    "partner",
    "internal",
    "desk",
    "email",
    "sms",
]


class UnifiedCommunicationThread(BaseModel):
    id: str
    title: str
    participant_name: str | None = None
    participant_email: str | None = None
    participant_type: str
    source_kind: str
    source_id: str
    source_ref: str | None = None
    source_label: str | None = None
    channel: str
    transport: str
    unread_count: int = 0
    message_count: int = 0
    latest_snippet: str | None = None
    latest_at: datetime
    assigned_desk: str | None = None
    href: str
    can_reply: bool = True


class UnifiedCommunicationThreadPage(BaseModel):
    items: list[UnifiedCommunicationThread] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int
    totals_by_participant: dict[str, int] = Field(default_factory=dict)
    totals_by_channel: dict[str, int] = Field(default_factory=dict)
    unread_total: int = 0


class UnifiedCommunicationMessage(BaseModel):
    id: str
    thread_id: str
    body: str
    sender_name: str | None = None
    sender_type: str
    direction: Literal["inbound", "outbound", "system"]
    channel: str
    transport: str
    created_at: datetime
    seen: bool = True
    delivery_status: str | None = None


class UnifiedCommunicationThreadDetail(BaseModel):
    thread: UnifiedCommunicationThread
    messages: list[UnifiedCommunicationMessage] = Field(default_factory=list)


class UnifiedCommunicationCompose(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class UnifiedCommunicationSeen(BaseModel):
    thread_id: str
    seen_at: datetime
