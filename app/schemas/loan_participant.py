"""Loan participant schemas.

The Loan Detail "Participants" UI is the single source of truth — never read
from .env. Lender rows default to hide_identity=True so the orchestrator
applies the One-Way Mirror filter automatically. Broker/Client default to
visible CC; Super Admin defaults to silent BCC for the audit trail.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.enums import ParticipantRole
from app.schemas.common import ORMModel


class LoanParticipantCreate(BaseModel):
    email: EmailStr
    role: ParticipantRole
    display_name: str | None = None
    company: str | None = None
    cc_outbound: bool | None = None
    bcc_outbound: bool | None = None
    hide_identity: bool | None = None
    # Optional: only used when role=super_admin and the UI passes a User row.
    user_id: UUID | None = None


class LoanParticipantUpdate(BaseModel):
    display_name: str | None = None
    company: str | None = None
    role: ParticipantRole | None = None
    cc_outbound: bool | None = None
    bcc_outbound: bool | None = None
    hide_identity: bool | None = None


class LoanParticipantRead(ORMModel):
    id: UUID
    loan_id: UUID
    email: str
    display_name: str | None
    role: ParticipantRole
    company: str | None
    cc_outbound: bool
    bcc_outbound: bool
    hide_identity: bool


def role_defaults(role: ParticipantRole) -> dict:
    """Sensible defaults for the toggles, based on role.

    - LENDER: hidden by default (One-Way Mirror); never CC'd or BCC'd to others.
    - BROKER: visibly CC'd on outbound mail (cc_outbound=True).
    - CLIENT: visibly CC'd on outbound mail (cc_outbound=True), simplified copy.
    - SUPER_ADMIN: silent BCC for audit trail (bcc_outbound=True).
    """
    if role == ParticipantRole.LENDER:
        return {"hide_identity": True, "cc_outbound": False, "bcc_outbound": False}
    if role == ParticipantRole.BROKER:
        return {"hide_identity": False, "cc_outbound": True, "bcc_outbound": False}
    if role == ParticipantRole.CLIENT:
        return {"hide_identity": False, "cc_outbound": True, "bcc_outbound": False}
    if role == ParticipantRole.SUPER_ADMIN:
        return {"hide_identity": False, "cc_outbound": False, "bcc_outbound": True}
    return {"hide_identity": False, "cc_outbound": False, "bcc_outbound": False}
