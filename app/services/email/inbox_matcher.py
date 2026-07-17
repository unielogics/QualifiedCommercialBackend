"""Match an inbound email to a loan / client / party for the Workspace inbox.

Resolution order (first hit wins; a loan link is only set when UNAMBIGUOUS so a
subject-bearing breadcrumb never lands on the wrong loan's shared feed):
  1. [QC-{deal_id}] subject tag → Loan (authoritative, unique-index hit).
  2. sender → LoanParticipant: sets the role; sets loan only if the sender is on
     exactly one loan.
  3. sender → Client.email (or Client.user_id→User.email): client_id only, and
     only when it resolves to exactly one client (a client may have many loans).
  4. sender → Lender.contact_email / submission_email: role, plus loan only when
     exactly one lender and one loan.
Anything unmatched (or ambiguous) stays inbox-only for that field.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.lender import Lender
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.models.user import User
from app.services.email.parser import extract_deal_id


@dataclass
class InboxMatch:
    loan_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    party_role: str | None = None  # lender | broker | client | super_admin | ...


def _norm(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e if e and "@" in e else None


async def match_inbound(db: AsyncSession, *, sender: str, subject: str) -> InboxMatch:
    """Resolve a loan_id / client_id / party role for an inbound email.
    Best-effort and side-effect-free — returns an empty InboxMatch when nothing
    resolves (the message still lands in the owner's inbox, just unlinked)."""
    m = InboxMatch()
    s = _norm(sender)

    # 1) Subject tag → Loan (authoritative). Loan.client_id is NOT NULL, so a loan
    #    match always yields the client too.
    deal_id = extract_deal_id(subject or "")
    loan: Loan | None = None
    if deal_id:
        loan = (await db.execute(select(Loan).where(Loan.deal_id == deal_id))).scalar_one_or_none()
    if loan is not None:
        m.loan_id = loan.id
        m.client_id = loan.client_id
        # Fill the party role from the loan's participants when the sender matches.
        if s is not None:
            m.party_role = await _participant_role(db, loan.id, s)
        return m

    if s is None:
        return m

    # 2) Sender → participant. Only bind a loan when it's UNAMBIGUOUS (the sender is
    #    a participant on exactly one loan) — a shared address on many loans would
    #    otherwise attach a subject-bearing breadcrumb to the wrong loan's shared
    #    feed. When ambiguous, keep only the role (inbox-only for the loan link).
    parts = (
        await db.execute(select(LoanParticipant).where(func.lower(LoanParticipant.email) == s))
    ).scalars().all()
    if parts:
        loan_ids = {p.loan_id for p in parts}
        m.party_role = _role_str(parts[0].role)
        if len(loan_ids) == 1:
            only_loan = await db.get(Loan, next(iter(loan_ids)))
            if only_loan is not None:
                m.loan_id = only_loan.id
                m.client_id = only_loan.client_id
        return m

    # 3) Sender → Client by email, else Client.user_id → User.email. client_id only
    #    (a client can have many loans, so we don't guess a loan). Unambiguous only.
    clients = (
        await db.execute(select(Client).where(func.lower(Client.email) == s))
    ).scalars().all()
    if not clients:
        # Fallback: the sender is the client's linked login (User) email.
        clients = (
            await db.execute(
                select(Client).join(User, Client.user_id == User.id).where(func.lower(User.email) == s)
            )
        ).scalars().all()
    if clients:
        m.party_role = "client"
        client_ids = {c.id for c in clients}
        if len(client_ids) == 1:
            m.client_id = next(iter(client_ids))
        return m

    # 4) Sender → Lender (direct email columns). Only bind a loan when the lender
    #    resolves unambiguously to exactly one lender AND one loan.
    lenders = (
        await db.execute(
            select(Lender).where(
                (func.lower(Lender.contact_email) == s) | (func.lower(Lender.submission_email) == s)
            )
        )
    ).scalars().all()
    if lenders:
        m.party_role = "lender"
        if len(lenders) == 1:
            lender_loans = (
                await db.execute(select(Loan).where(Loan.lender_id == lenders[0].id))
            ).scalars().all()
            if len(lender_loans) == 1:
                m.loan_id = lender_loans[0].id
                m.client_id = lender_loans[0].client_id

    return m


async def _participant_role(db: AsyncSession, loan_id: uuid.UUID, sender: str) -> str | None:
    rows = (
        await db.execute(select(LoanParticipant).where(LoanParticipant.loan_id == loan_id))
    ).scalars().all()
    for p in rows:
        if (p.email or "").strip().lower() == sender:
            return _role_str(p.role)
    return None


def _role_str(role) -> str | None:
    if role is None:
        return None
    return role.value.lower() if hasattr(role, "value") else str(role).lower()
