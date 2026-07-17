"""Response/request shapes for the owner-scoped Workspace inbox (Phase 5).

These are hand-constructed (not ORM-validated) because message bodies are stored
ENCRYPTED at rest and decrypted only on the owner-only read path — the plaintext
`body_text` / `preview` fields never exist on the ORM row.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class InboxMessageRead(BaseModel):
    """One message in a thread, body DECRYPTED (owner-only)."""

    id: UUID
    gmail_thread_id: str | None = None
    gmail_message_id: str | None = None
    direction: str
    from_email: str | None = None
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    subject: str | None = None
    body_text: str | None = None  # decrypted plaintext — owner-only
    received_at: datetime | None = None
    is_read: bool = False
    is_starred: bool = False
    has_attachments: bool = False
    loan_id: UUID | None = None
    client_id: UUID | None = None
    matched_party_role: str | None = None


class InboxThreadSummary(BaseModel):
    """A thread row for the mailbox list. `preview` is derived from the latest
    message's DECRYPTED body (owner-only) — there is no plaintext body at rest."""

    thread_id: str  # gmail_thread_id, or the message id for un-threaded singletons
    subject: str | None = None
    last_from: str | None = None
    preview: str | None = None
    last_received_at: datetime | None = None
    message_count: int = 1
    unread_count: int = 0
    is_starred: bool = False
    has_attachments: bool = False
    participants: list[str] = []
    loan_id: UUID | None = None
    client_id: UUID | None = None
    matched_party_role: str | None = None


class InboxThreadListResponse(BaseModel):
    threads: list[InboxThreadSummary]
    total: int  # total threads matched before pagination
    truncated: bool = False  # true if the underlying message fetch hit its cap


class InboxThreadDetail(BaseModel):
    thread_id: str
    subject: str | None = None
    loan_id: UUID | None = None
    client_id: UUID | None = None
    matched_party_role: str | None = None
    messages: list[InboxMessageRead]


class InboxReplyRequest(BaseModel):
    body: str
    to_emails: list[str] | None = None  # defaults to the latest inbound sender
    cc_emails: list[str] | None = None
    subject: str | None = None  # defaults to "Re: <thread subject>"


class InboxReplyResponse(BaseModel):
    ok: bool
    detail: str | None = None
    message_id: str | None = None


class MarkReadRequest(BaseModel):
    is_read: bool = True


class StarRequest(BaseModel):
    is_starred: bool = True
