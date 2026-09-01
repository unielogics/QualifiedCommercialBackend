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
    #: E.164 where the thread is phone-addressed (SMS); lets the contact
    #: grouping merge a person's SMS with their portal and email threads.
    participant_phone: str | None = None
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


class UnifiedContactGroup(BaseModel):
    """One person, every channel. The inbox row the operator actually wants:
    who spoke last, through what, with the full history one click away."""

    key: str
    name: str
    email: str | None = None
    phone: str | None = None
    channels: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    unread_total: int = 0
    message_total: int = 0
    latest_thread_id: str
    latest_snippet: str | None = None
    latest_channel: str
    latest_at: datetime
    threads: list[UnifiedCommunicationThread] = Field(default_factory=list)


class UnifiedContactPage(BaseModel):
    items: list[UnifiedContactGroup] = Field(default_factory=list)
    total: int = 0
    unread_total: int = 0


class ComposeRecipient(BaseModel):
    """One row in the new-message recipient picker."""

    kind: Literal["client", "intake", "dealer", "rep_contact"]
    id: str
    name: str
    label: str | None = None
    email: str | None = None
    phone: str | None = None


class UnifiedComposeRequest(BaseModel):
    recipient_kind: Literal["client", "intake", "dealer", "rep_contact"]
    recipient_id: str
    channels: list[Literal["sms", "email"]] = Field(min_length=1)
    subject: str | None = Field(None, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


class ComposeChannelResult(BaseModel):
    channel: str
    ok: bool
    detail: str = ""


class UnifiedComposeResult(BaseModel):
    ok: bool
    results: list[ComposeChannelResult] = Field(default_factory=list)
    #: Thread the inbox should open after a successful send.
    thread_id: str | None = None
