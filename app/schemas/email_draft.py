"""Email draft schemas — broker-approval inbox."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from app.enums import EmailDraftStatus
from app.schemas.common import ORMModel


class EmailDraftRead(ORMModel):
    id: UUID
    loan_id: UUID
    to_email: str
    cc_emails: list[str] | None
    bcc_emails: list[str] | None
    subject: str
    body: str
    status: EmailDraftStatus
    triggered_by_kind: str | None
    actioned_by: str | None
    sent_message_id: str | None


class EmailDraftDecision(BaseModel):
    decision: EmailDraftStatus  # APPROVED|DISMISSED — anything else rejected
    body_override: str | None = None  # broker can tweak before sending
    subject_override: str | None = None


class EmailDraftCreate(BaseModel):
    """Used by tests + the Living Loan File / orchestrator flow."""
    loan_id: UUID
    to_email: str
    subject: str
    body: str
    cc_emails: list[str] | None = None
    bcc_emails: list[str] | None = None
    triggered_by_kind: str | None = None
    triggered_by_payload: dict | None = None
