"""Fintech Orchestrator — Lender → Broker/Client relay logic.

Used by the inbound email handler (Pub/Sub webhook or polling worker). The
orchestrator does three things:

1. Identify the inbound participant (Lender / Broker / Client / unknown) by
   matching against the loan's `LoanParticipant` rows.
2. If Lender: scrub PII via app.services.email.pii_filter.RedactionContext,
   create an EmailDraft addressed to the broker (and optionally the client),
   and enqueue a high-priority AITask.
3. If Broker/Client: relay the message back into the Lender thread (subject
   prefixed with [QC-{deal_id}]) using the configured Gmail delegated user.

The relay layer wraps the Gmail client so the actual Gmail send call only
happens when domain-wide delegation is set up — otherwise the draft is left
in `pending` for manual broker action.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import AITaskPriority, AITaskSource, AITaskStatus, EmailDraftStatus, ParticipantRole
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.models.email_draft import EmailDraft
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.services.email.parser import inject_deal_id
from app.services.email.pii_filter import RedactionContext, reframe_request_for_broker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundEmail:
    """Raw inbound email shape, agnostic of source (Pub/Sub, polling, etc)."""

    sender: str
    subject: str
    body: str
    deal_id: str | None  # extracted from subject — see parser.extract_deal_id


@dataclass(frozen=True)
class InboundResult:
    loan_id: str | None
    sender_role: str  # "lender" | "broker" | "client" | "unknown"
    draft_id: str | None
    task_id: str | None
    note: str


async def _participants(db: AsyncSession, loan_id) -> list[LoanParticipant]:
    return list(
        (
            await db.execute(
                select(LoanParticipant).where(LoanParticipant.loan_id == loan_id)
            )
        ).scalars().all()
    )


def _lookup_role(participants: Iterable[LoanParticipant], email: str) -> ParticipantRole | None:
    em = email.lower().strip()
    for p in participants:
        if p.email.lower() == em:
            return p.role
    return None


def _broker_target(participants: Iterable[LoanParticipant]) -> LoanParticipant | None:
    """Pick the primary broker recipient (To: line) for orchestrator drafts."""
    # First, prefer a broker explicitly marked for visible CC — that operator
    # is engaged on the thread.
    for p in participants:
        if p.role == ParticipantRole.BROKER and p.cc_outbound:
            return p
    # Otherwise: any broker on the thread.
    for p in participants:
        if p.role == ParticipantRole.BROKER:
            return p
    return None


def _cc_list(participants: Iterable[LoanParticipant], primary_to_email: str | None) -> list[str]:
    """Visible CC list for outbound mail (broker + client rows flagged cc_outbound)."""
    out: list[str] = []
    for p in participants:
        if not p.cc_outbound:
            continue
        if p.role == ParticipantRole.LENDER:  # never CC a lender on a broker-facing draft
            continue
        if primary_to_email and p.email.lower() == primary_to_email.lower():
            continue  # already on To
        out.append(p.email)
    return out


def _bcc_list(participants: Iterable[LoanParticipant]) -> list[str]:
    return [p.email for p in participants if p.bcc_outbound]


async def process_inbound(db: AsyncSession, email: InboundEmail) -> InboundResult:
    """Run the inbound logic for one email.

    Caller (webhook handler) is responsible for committing the session.
    """
    if email.deal_id is None:
        return InboundResult(
            loan_id=None,
            sender_role="unknown",
            draft_id=None,
            task_id=None,
            note="No [QC-{deal_id}] tag in subject — message ignored by orchestrator.",
        )

    loan = (
        await db.execute(select(Loan).where(Loan.deal_id == email.deal_id))
    ).scalar_one_or_none()
    if loan is None:
        return InboundResult(
            loan_id=None,
            sender_role="unknown",
            draft_id=None,
            task_id=None,
            note=f"Subject references unknown deal {email.deal_id}.",
        )

    participants = await _participants(db, loan.id)
    sender_role = _lookup_role(participants, email.sender)

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label=(
                sender_role.value
                if sender_role is not None and hasattr(sender_role, "value")
                else (str(sender_role) if sender_role is not None else "unknown")
            ),
            kind="email.inbound",
            summary=f"Inbound email — {email.sender}: {email.subject[:120]}",
            payload={"subject": email.subject, "from": email.sender, "deal_id": email.deal_id},
        )
    )

    # Lender → run the One-Way Mirror flow.
    if sender_role == ParticipantRole.LENDER:
        return await _handle_lender_inbound(db, email, loan, participants)

    # Broker/Client/unknown: log only — outbound relay handled by their own UI actions.
    return InboundResult(
        loan_id=str(loan.id),
        sender_role=(
            sender_role.value
            if sender_role is not None and hasattr(sender_role, "value")
            else (str(sender_role) if sender_role is not None else "unknown")
        ),
        draft_id=None,
        task_id=None,
        note="Logged inbound activity (non-lender sender).",
    )


async def _handle_lender_inbound(
    db: AsyncSession,
    email: InboundEmail,
    loan: Loan,
    participants: list[LoanParticipant],
) -> InboundResult:
    ctx = RedactionContext.from_participants(participants)
    safe_body = reframe_request_for_broker(lender_text=email.body, ctx=ctx)

    broker = _broker_target(participants)
    bcc = _bcc_list(participants)

    if broker is None:
        # No broker on the thread — flag a high-priority AI task instead.
        task = AITask(
            loan_id=loan.id,
            source=AITaskSource.MESSAGES,
            priority=AITaskPriority.HIGH,
            status=AITaskStatus.PENDING,
            action="assign_broker",
            title="Assign a broker to this deal",
            summary="A lender update arrived but no broker is on the thread. Add a broker in Participants.",
            confidence=1.0,
            agent="orchestrator",
            draft_payload={"redacted_body": safe_body, "deal_id": loan.deal_id},
        )
        db.add(task)
        await db.flush()
        return InboundResult(
            loan_id=str(loan.id),
            sender_role="lender",
            draft_id=None,
            task_id=str(task.id),
            note="No broker — created HIGH AI task instead of draft.",
        )

    subject = inject_deal_id(f"Update on {loan.address}", loan.deal_id)
    cc = _cc_list(participants, broker.email)
    draft = EmailDraft(
        loan_id=loan.id,
        to_email=broker.email,
        cc_emails=cc or None,
        bcc_emails=bcc or None,
        subject=subject,
        body=safe_body,
        status=EmailDraftStatus.PENDING,
        triggered_by_kind="inbound_lender_request",
        triggered_by_payload={"sender": email.sender, "subject": email.subject},
    )
    db.add(draft)
    await db.flush()

    task = AITask(
        loan_id=loan.id,
        source=AITaskSource.MESSAGES,
        priority=AITaskPriority.HIGH,
        status=AITaskStatus.PENDING,
        action="approve_email_draft",
        title="Approve broker notification — lender update",
        summary="Lender pinged the deal. A redacted summary draft is ready to send to the broker.",
        confidence=0.95,
        agent="orchestrator",
        draft_payload={"draft_id": str(draft.id)},
    )
    db.add(task)
    await db.flush()

    return InboundResult(
        loan_id=str(loan.id),
        sender_role="lender",
        draft_id=str(draft.id),
        task_id=str(task.id),
        note="Drafted broker notification with PII scrubbed; awaiting approval.",
    )


# --- Outbound send (called when broker approves a draft) ---------------------

async def send_approved_draft(db: AsyncSession, draft: EmailDraft, *, actor_label: str | None = None) -> dict:
    """Send a previously-drafted email via Gmail (when DWD is set up).

    Falls back to "queued" status if the Gmail client isn't configured —
    keeps the dev path unblocked.
    """
    settings = get_settings()
    sent_id: str | None = None
    note: str

    # 1) Send-as-user: if the draft has a sender with a connected personal Gmail,
    #    send from THEIR mailbox (their Sent folder, replies to them).
    if draft.sender_user_id is not None:
        from app.services.email.user_mailer import send_as_user

        result = await send_as_user(
            db,
            draft.sender_user_id,
            to_emails=[draft.to_email],
            subject=draft.subject,
            body_text=draft.body,
            cc_emails=list(draft.cc_emails or []),
            bcc_emails=list(draft.bcc_emails or []),
        )
        if result.ok:
            sent_id = result.message_id
            # Attribute the transport accurately: only "sent_gmail" is truly from
            # the connected user's mailbox; "sent" means it went out via firm SES.
            if result.detail == "sent_gmail":
                note = f"Sent as connected user. message_id={sent_id}"
            else:
                note = f"Sent via firm mailbox (sender's Gmail not connected). message_id={sent_id}"
        elif result.detail.startswith("gmail_send_failed"):
            note = f"{result.detail}. Draft kept as approved for retry."
        else:
            note = ""  # not connected / SES dormant — fall through to firm paths below

    # 2) Firm Gmail service account (existing behavior) when no per-user send happened.
    if sent_id is None and not note:
        if settings.gmail_service_account_path and settings.gmail_delegated_user:
            # Local import to avoid hard-binding the Gmail client at module load.
            from app.services.email.gmail_client import gmail_config, send_message

            cfg = gmail_config()
            if cfg is None:
                note = "Gmail config missing; draft marked as approved but not sent."
            else:
                try:
                    # Real Gmail send. cc_emails / bcc_emails are carried as
                    # Cc / Bcc headers in the raw MIME — Gmail delivers to all
                    # three (the Bcc header is stripped from the delivered copy).
                    resp = send_message(
                        cfg,
                        to=draft.to_email,
                        subject=draft.subject,
                        body=draft.body,
                        cc=list(draft.cc_emails or []),
                        bcc=list(draft.bcc_emails or []),
                    )
                    sent_id = resp.get("id")
                    note = f"Sent via Gmail. message_id={sent_id}"
                except Exception as exc:  # noqa: BLE001
                    note = f"Gmail send failed: {exc}. Draft kept as approved for retry."
        else:
            note = "Gmail not configured (no SA path / delegated user). Draft approved but not sent."

    # A merged super-admin update enrolls the realtor as a cc_outbound BROKER
    # participant ONLY once the email actually goes out (any transport) — so a
    # drafted-then-dismissed update never silently adds the realtor to all future
    # loan correspondence. Enrollment happens at send time, not draft time.
    if sent_id and draft.triggered_by_kind == "merged_update":
        realtor_email = (draft.triggered_by_payload or {}).get("realtor_email")
        if realtor_email:
            from app.services.email.merged_send import enroll_realtor_participant

            try:
                await enroll_realtor_participant(db, draft.loan_id, realtor_email)
            except Exception:  # noqa: BLE001
                log.exception("merged_update: realtor enrollment failed loan=%s", draft.loan_id)

    draft.status = EmailDraftStatus.SENT if sent_id else EmailDraftStatus.APPROVED
    draft.actioned_by = actor_label
    draft.sent_message_id = sent_id

    db.add(
        Activity(
            loan_id=draft.loan_id,
            actor_id=None,
            actor_label=actor_label or "broker",
            kind="email.outbound" if sent_id else "email.draft_approved",
            summary=f"Outbound to {draft.to_email}: {draft.subject[:120]}",
            payload={"draft_id": str(draft.id), "sent_message_id": sent_id, "note": note},
        )
    )
    await db.flush()
    return {
        "sent_message_id": sent_id,
        "status": (
            draft.status.value
            if hasattr(draft.status, "value")
            else str(draft.status)
        ),
        "note": note,
    }
