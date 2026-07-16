"""Merged super-admin update — one email, one thread, client + realtor together.

When a super-admin works a loan file and wants to update both the borrower and
their realtor at once, this drafts a single EmailDraft addressed To the client
with the realtor CC'd (and super-admins BCC'd for audit). The subject carries the
[QC-{deal_id}] tag so replies thread back into the loan via the inbound
orchestrator. It's approve-first (status=PENDING) like lender_send, and sends via
the super-admin's connected Gmail (send_as_user) on approval.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import EmailDraftStatus, ParticipantRole
from app.models.client import Client
from app.models.email_draft import EmailDraft
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.services.ai.agent_settings import load_agent_user_id_for_loan
from app.services.email.parser import inject_deal_id

log = logging.getLogger(__name__)


async def _realtor_email(db: AsyncSession, loan: Loan) -> str | None:
    """Resolve the realtor (broker) email for a loan: broker_id → Broker.user_id → User.email."""
    user_id = await load_agent_user_id_for_loan(db, loan)
    if user_id is None:
        return None
    from app.models.user import User

    user = await db.get(User, user_id)
    return user.email if user and user.email else None


async def _client_email(db: AsyncSession, loan: Loan) -> str | None:
    client = await db.get(Client, loan.client_id)
    if client and client.email:
        return client.email
    if client and client.user_id:
        from app.models.user import User

        user = await db.get(User, client.user_id)
        return user.email if user and user.email else None
    return None


async def _super_admin_emails(db: AsyncSession) -> list[str]:
    from app.enums import Role
    from app.models.user import User

    rows = (
        await db.execute(
            select(User.email).where(User.role == Role.SUPER_ADMIN, User.deleted_at.is_(None))
        )
    ).scalars().all()
    return sorted({e.strip().lower() for e in rows if e and "@" in e})


def _norm(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e if e and "@" in e else None


async def enroll_realtor_participant(db: AsyncSession, loan_id: uuid.UUID, realtor_email: str | None) -> None:
    """Idempotently make the realtor a cc_outbound BROKER participant on the loan
    so they stay on the thread for replies. Called at SEND time (not draft time)
    so a never-approved/dismissed merged draft leaves no persistent recipient
    side-effect. Never flips a pre-existing NON-broker row's role to visible CC."""
    email = _norm(realtor_email)
    if not email:
        return
    existing = (
        await db.execute(
            select(LoanParticipant).where(
                LoanParticipant.loan_id == loan_id,
                LoanParticipant.email == email,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            LoanParticipant(
                loan_id=loan_id,
                email=email,
                role=ParticipantRole.BROKER,
                cc_outbound=True,
            )
        )
    elif existing.role == ParticipantRole.BROKER and not existing.cc_outbound:
        # Only promote an actual broker row — never re-role a client/lender row.
        existing.cc_outbound = True
    await db.flush()


async def draft_merged_update(
    db: AsyncSession,
    *,
    loan_id: uuid.UUID,
    subject: str,
    body: str,
    actor_user_id: uuid.UUID | None,
) -> EmailDraft:
    """Create an approve-first merged update draft. To=client, Cc=realtor,
    Bcc=super-admins, subject tagged [QC-{deal_id}]. Raises ValueError when there
    is no client email to address."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise ValueError("loan not found")

    client_email = _norm(await _client_email(db, loan))
    if not client_email:
        raise ValueError("loan has no client email to address the update to")
    realtor_email = _norm(await _realtor_email(db, loan))
    bcc = await _super_admin_emails(db)

    # Don't put the same address on both To and Cc when broker == borrower.
    cc = [realtor_email] if realtor_email and realtor_email != client_email else []
    # NOTE: the realtor is enrolled as a cc_outbound BROKER participant only at
    # SEND time (see enroll_realtor_participant, called from send_approved_draft),
    # so a drafted-but-dismissed merged update never silently adds the realtor to
    # all future loan correspondence.

    tagged_subject = inject_deal_id(subject.strip(), loan.deal_id)[:512]
    draft = EmailDraft(
        loan_id=loan.id,
        to_email=client_email,
        cc_emails=cc or None,
        bcc_emails=bcc or None,
        subject=tagged_subject,
        body=body,
        status=EmailDraftStatus.PENDING,
        sender_user_id=actor_user_id,
        triggered_by_kind="merged_update",
        triggered_by_payload={"realtor_email": realtor_email, "client_email": client_email},
    )
    db.add(draft)
    await db.flush()
    return draft


# Per-stage broker-facing update copy. Only these stages notify the realtor.
_STAGE_BROKER_MESSAGE = {
    "lender_connected": ("Your file is now with the lender",
                         "Good news — this file has been connected to a lender and is moving into review."),
    "processing": ("Your file is in processing",
                   "This file is now in processing. We'll flag anything else we need from you."),
    "closing": ("Your file is heading to closing",
                "This file has reached the closing stage. Please watch for any final items."),
    "funded": ("Your file has funded",
               "Congratulations — this file has funded. Thank you for the partnership."),
}


async def maybe_send_stage_change_email(
    db: AsyncSession, loan: Loan, new_stage: str, actor_user_id: uuid.UUID | None
) -> bool:
    """Send a realtor status-change email when the loan's owner has status-change
    automation enabled. Gated by automation_allowed (firm + per-user + connected).
    Best-effort: returns True if sent, never raises to the caller."""
    stage = new_stage.value if hasattr(new_stage, "value") else str(new_stage)
    copy = _STAGE_BROKER_MESSAGE.get(stage)
    if copy is None:
        return False
    # The sender is the loan's operational OWNER (whose Gmail we send from) — a
    # property of the loan, NOT whoever happened to click the stage change. An
    # owner-less loan deterministically does not auto-notify (rather than firing
    # from — and gating on — the acting admin, which made the outcome depend on
    # who moved the stage).
    from app.services.google.google_oauth_client import automation_allowed

    sender_id = loan.assigned_owner_id
    if sender_id is None or not await automation_allowed(db, sender_id, "status_change_email"):
        return False
    realtor_email = await _realtor_email(db, loan)
    if not realtor_email:
        return False

    from app.services.email.user_mailer import send_as_user

    subject_core, body_intro = copy
    subject = inject_deal_id(f"{subject_core} — {loan.address}"[:200], loan.deal_id)
    body = f"{body_intro}\n\nDeal: {loan.deal_id} · {loan.address}\n\n— Qualified Commercial"
    try:
        result = await send_as_user(db, sender_id, to_emails=[realtor_email], subject=subject, body_text=body)
        return bool(result.ok)
    except Exception:  # noqa: BLE001
        log.exception("stage-change broker email failed loan=%s", loan.id)
        return False
