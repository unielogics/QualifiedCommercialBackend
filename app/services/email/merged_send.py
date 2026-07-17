"""Merged super-admin update + real-estate-agent (broker) automations.

Three related send paths, all keyed to a loan's connected Google users:

1. draft_merged_update — one EmailDraft To the client, realtor Cc'd, super-admins
   Bcc'd for audit; [QC-{deal_id}] subject tag so replies thread back via the
   inbound orchestrator. Approve-first (status=PENDING), sent via the super-admin's
   connected Gmail (send_as_user) on approval.
2. maybe_send_stage_change_email — a realtor status-change email on real loan
   stage transitions (gated by automation_allowed(...,"status_change_email")).
3. maybe_send_broker_collection_email — a once-per-day digest to the realtor of
   the outstanding items still being collected on the file, so they can chase the
   borrower (gated by automation_allowed(...,"re_agent_email")).

Both (2) and (3) send FROM the loan's operational owner (loan.assigned_owner_id),
never the acting admin or the broker, and no-op when there's no owner.
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


# ── Broker collection digest (RE-agent automation, re_agent_email) ────────────
#
# A once-per-day email to the loan's real-estate agent (broker) listing the
# outstanding items still being collected on their file, so they can chase the
# borrower. This is the "tasks that need completing/collecting" automation. It
# mirrors maybe_send_stage_change_email: sender = loan owner (whose Gmail we send
# from), gated by automation_allowed(...,"re_agent_email"). Dedup is a per-loan
# Activity row so the daily scheduler tick can't double-send.

_BROKER_DIGEST_ACTIVITY_KIND = "broker.collection_digest"
_BROKER_DIGEST_DEDUP_HOURS = 20  # < 24h so a daily 9am tick fires at most once/day


async def _owner_email(db: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    from app.models.user import User

    user = await db.get(User, user_id)
    return _norm(user.email) if user and user.email else None


async def _outstanding_docs_for_loan(db: AsyncSession, loan_id: uuid.UUID) -> list[str]:
    """Names of documents still being collected on the loan (status=REQUESTED)."""
    from app.enums import DocStatus
    from app.models.document import Document

    rows = (
        await db.execute(
            select(Document.name).where(
                Document.loan_id == loan_id,
                Document.status == DocStatus.REQUESTED,
            )
        )
    ).scalars().all()
    # De-dup + stable order; drop blanks.
    seen: list[str] = []
    for name in rows:
        n = (name or "").strip()
        if n and n not in seen:
            seen.append(n)
    return seen


async def _broker_digest_already_sent_today(db: AsyncSession, loan_id: uuid.UUID) -> bool:
    from datetime import datetime, timedelta, timezone

    from app.models.activity import Activity

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_BROKER_DIGEST_DEDUP_HOURS)
    row = (
        await db.execute(
            select(Activity.id)
            .where(
                Activity.loan_id == loan_id,
                Activity.kind == _BROKER_DIGEST_ACTIVITY_KIND,
                Activity.occurred_at >= cutoff,
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def resolve_broker_send_target(
    db: AsyncSession, loan: Loan
) -> tuple[uuid.UUID, str] | None:
    """Return (sender_owner_id, normalized_realtor_email) when this loan may
    receive an automated RE-agent email, else None.

    Shared gate for both the daily collection digest and the cadence broker
    lane: requires an assigned owner, automation_allowed(...,"re_agent_email")
    on that owner, a resolvable realtor, and owner != realtor (no self-email —
    both sides normalized so case/whitespace can't defeat the guard)."""
    from app.services.google.google_oauth_client import automation_allowed

    sender_id = loan.assigned_owner_id
    if sender_id is None or not await automation_allowed(db, sender_id, "re_agent_email"):
        return None
    realtor_email = _norm(await _realtor_email(db, loan))
    if not realtor_email:
        return None
    owner_email = await _owner_email(db, sender_id)  # already normalized
    if owner_email and owner_email == realtor_email:
        return None
    return sender_id, realtor_email


async def claim_broker_email_slot(
    db: AsyncSession, loan_id: uuid.UUID, *, summary: str, payload: dict | None = None
) -> bool:
    """Atomically claim today's ONE automated RE-agent email slot for a loan.

    Both the daily collection digest and the cadence broker lane call this, so a
    loan gets at most one automated realtor email per day across BOTH mechanisms
    (whichever fires first wins). Inserts the dedup Activity row and COMMITS it
    immediately, so the marker survives a later pass-wide commit failure/crash
    and closes the check-then-send race with the manual run-doc-reminders
    endpoint. Returns False if a recent marker already exists (someone else
    claimed it) or the commit fails. The email is sent only after this returns
    True, so at worst a crash between claim and send SKIPS one day's email
    (safe) rather than sending twice (spammy).

    Concurrency: a transaction-scoped Postgres advisory lock keyed on the loan
    serializes concurrent claimers (e.g. the 30-min cadence pass overlapping the
    manual run-doc-reminders endpoint). The lock is held until the commit that
    persists the marker releases it, so the loser blocks, then its own recency
    check sees the committed marker and backs off — no duplicate send even under
    truly-simultaneous claims."""
    from sqlalchemy import bindparam, text

    from app.models.activity import Activity

    # Two 32-bit keys: a constant namespace + a stable hash of the loan id.
    _LOCK_NS = 0x51434252  # "QCBR"
    lock_key = (int(loan_id.int) & 0x7FFFFFFF)
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :k)").bindparams(
                bindparam("ns", _LOCK_NS), bindparam("k", lock_key)
            )
        )
    except Exception:  # noqa: BLE001 — non-PG backend / lock error: fall back to the
        # best-effort check-then-insert below (still safe on the single-instance
        # scheduler; the advisory lock is defense-in-depth for multi-worker).
        log.debug("broker email slot advisory lock unavailable loan=%s", loan_id)

    if await _broker_digest_already_sent_today(db, loan_id):
        return False
    db.add(
        Activity(
            loan_id=loan_id,
            actor_id=None,
            actor_label="ai",
            kind=_BROKER_DIGEST_ACTIVITY_KIND,
            summary=summary,
            payload=payload,
        )
    )
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 — lost the race (or DB error); don't send.
        await db.rollback()
        log.warning("broker email slot claim failed loan=%s", loan_id)
        return False
    return True


async def maybe_send_broker_collection_email(db: AsyncSession, loan: Loan) -> bool:
    """Email the loan's real-estate agent a digest of outstanding collection items.

    Gated by resolve_broker_send_target (owner + automation_allowed + realtor +
    no self-email). Sends from the owner's Gmail. Best-effort: never raises.
    Idempotent per loan per day — the dedup Activity marker is claimed AND
    committed BEFORE the send, so a crash can only under-send, never double-send.
    Returns True only when an email was actually sent."""
    target = await resolve_broker_send_target(db, loan)
    if target is None:
        return False
    sender_id, realtor_email = target

    outstanding = await _outstanding_docs_for_loan(db, loan.id)
    if not outstanding:
        return False  # nothing to chase — stay quiet

    shown = outstanding[:20]
    more = len(outstanding) - len(shown)
    # Claim the slot (commit the marker) before the irreversible send.
    if not await claim_broker_email_slot(
        db,
        loan.id,
        summary=f"Broker collection digest sent — {len(outstanding)} item(s) to {realtor_email}",
        payload={"items": shown, "item_count": len(outstanding), "to": realtor_email, "source": "digest"},
    ):
        return False

    from app.services.email.user_mailer import send_as_user

    bullet_lines = "\n".join(f"  • {name}" for name in shown)
    if more > 0:
        bullet_lines += f"\n  • …and {more} more"
    subject = inject_deal_id(f"Items still needed — {loan.address}"[:200], loan.deal_id)
    body = (
        f"Hi,\n\nThese items are still outstanding on {loan.address} "
        f"(Deal {loan.deal_id}). Please help nudge your client so we can keep the "
        f"file moving:\n\n{bullet_lines}\n\n"
        f"Reply to this email and it will thread back to the file.\n\n— Qualified Commercial"
    )
    try:
        result = await send_as_user(db, sender_id, to_emails=[realtor_email], subject=subject, body_text=body)
    except Exception:  # noqa: BLE001
        log.exception("broker collection digest failed loan=%s", loan.id)
        return False
    # The marker is already committed; even if the send failed we keep it so we
    # don't hammer the realtor on the next tick. A failed send is logged only.
    if not result.ok:
        log.warning("broker collection digest send not ok loan=%s detail=%s", loan.id, result.detail)
        return False
    return True
