from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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


class CommunicationImage(BaseModel):
    id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    #: Signed and short-lived. None when object storage is not configured.
    url: str | None = None


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
    images: list[CommunicationImage] = Field(default_factory=list)


class UnifiedCommunicationThreadDetail(BaseModel):
    thread: UnifiedCommunicationThread
    messages: list[UnifiedCommunicationMessage] = Field(default_factory=list)
    delivery_warning: str | None = None


class UnifiedCommunicationCompose(BaseModel):
    body: str = Field(default="", max_length=20_000)
    image_ids: list[UUID] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def content_required(self):
        if not self.body.strip() and not self.image_ids:
            raise ValueError("Enter a message or attach an image.")
        if len(set(self.image_ids)) != len(self.image_ids):
            raise ValueError("Attach each image only once.")
        return self


class UnifiedCommunicationSeen(BaseModel):
    thread_id: str
    seen_at: datetime


class CommunicationMessagesSeen(BaseModel):
    message_ids: list[UUID] = Field(default_factory=list, max_length=500)


class FileCommunicationSeen(CommunicationMessagesSeen):
    channel: Literal["sms", "email"]
    thread_id: UUID | None = None


class FileCommunicationUnread(BaseModel):
    intake_id: UUID
    profile_id: UUID | None = None
    sms: int = 0
    email: int = 0
    total: int = 0
    as_of: datetime


class FileCommunicationUnreadBatchRequest(BaseModel):
    intake_ids: list[UUID] = Field(max_length=100)


class FileCommunicationUnreadBatchResponse(BaseModel):
    items: list[FileCommunicationUnread] = Field(default_factory=list)


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


class UnifiedComposeRequest(UnifiedCommunicationCompose):
    recipient_kind: Literal["client", "intake", "dealer", "rep_contact"]
    recipient_id: str
    channels: list[Literal["sms", "email"]] = Field(min_length=1)
    subject: str | None = Field(None, max_length=300)


class ComposeChannelResult(BaseModel):
    channel: str
    ok: bool
    detail: str = ""


class UnifiedComposeResult(BaseModel):
    ok: bool
    results: list[ComposeChannelResult] = Field(default_factory=list)
    #: Thread the inbox should open after a successful send.
    thread_id: str | None = None
    delivery_warning: str | None = None
