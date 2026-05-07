"""Connect-Lender — single entry point that wires a Lender onto a Loan.

Connecting a lender does four things atomically:

  1. Sets `loan.lender_id`. Single source of truth for "which lender
     is on this deal" — used by the desktop loan page to render
     'Connected: <name>' without joining through participants.

  2. Ensures a `LoanParticipant(role=LENDER, hide_identity=True)` row
     exists. THIS is what `app/services/email/orchestrator.py:115`
     and friends actually key off when an inbound email tagged
     `[QC-{deal_id}]` arrives. Without this row the One-Way Mirror
     redaction never kicks in, no matter what `loan.lender_id` says.

  3. Applies the operator's notify-list toggles — flips
     `cc_outbound` / `bcc_outbound` on the broker / super-admin
     participant rows the operator picked at connect time. The
     orchestrator's outbound recipient builder reads these directly
     (see `_bcc_list` in orchestrator.py), so no extra wiring is
     needed here.

  4. Promotes stage to LENDER_CONNECTED if the loan hadn't reached
     it yet. We never auto-regress.

Idempotent: re-running with the same lender_id is a no-op for the
participant + stage; notify toggles are replayed (the operator can
edit the notify list later by re-running this).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import LoanStage, ParticipantRole
from app.models.activity import Activity
from app.models.lender import Lender
from app.models.loan import Loan
from app.models.loan_participant import LoanParticipant
from app.services.activity_log import mark_loan_dirty

log = logging.getLogger(__name__)


@dataclass
class NotifyToggle:
    """Per-participant CC/BCC override applied at connect time.

    The participant_id must reference a participant on THIS loan.
    Targeting a CLIENT participant is rejected server-side — clients
    must never be CC'd on lender mail.
    """

    participant_id: UUID
    cc_outbound: bool
    bcc_outbound: bool


@dataclass
class ConnectResult:
    loan: Loan
    lender: Lender
    participant_id: UUID  # the LENDER participant row (created or pre-existing)
    cc_count: int
    bcc_count: int
    stage_advanced: bool


class LenderConnectError(ValueError):
    """Raised for caller-fixable validation errors (bad lender, bad
    notify target, etc.). Routers translate these to 400 responses."""


async def connect_lender(
    db: AsyncSession,
    *,
    loan_id: UUID,
    lender_id: UUID,
    notify_toggles: list[NotifyToggle],
    actor_user_id: UUID | None,
    actor_label: str,
) -> ConnectResult:
    loan = (
        await db.execute(
            select(Loan).options(selectinload(Loan.participants)).where(Loan.id == loan_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        raise LenderConnectError("Loan not found")

    lender = (
        await db.execute(select(Lender).where(Lender.id == lender_id))
    ).scalar_one_or_none()
    if lender is None:
        raise LenderConnectError("Lender not found")
    if not lender.is_active:
        raise LenderConnectError(
            f"Lender '{lender.name}' is inactive. Reactivate it before connecting."
        )

    lender_email = lender.submission_email or lender.contact_email
    if not lender_email:
        raise LenderConnectError(
            f"Lender '{lender.name}' has no submission_email or contact_email — "
            "add one before connecting."
        )

    # 1. FK on the loan
    loan.lender_id = lender.id

    # 2. Ensure the LENDER participant row. The unique constraint is
    # (loan_id, email), so we look up by that pair before inserting.
    existing_lender_participant: LoanParticipant | None = next(
        (p for p in loan.participants if p.email.lower() == lender_email.lower()),
        None,
    )
    if existing_lender_participant is None:
        existing_lender_participant = LoanParticipant(
            loan_id=loan.id,
            email=lender_email,
            display_name=lender.name,
            role=ParticipantRole.LENDER,
            company=lender.name,
            cc_outbound=False,
            bcc_outbound=False,
            hide_identity=True,  # Gateway Rule default for lenders
        )
        db.add(existing_lender_participant)
        await db.flush()  # surface its id for the result + downstream lookups
        loan.participants.append(existing_lender_participant)
    else:
        # If the participant pre-existed (e.g. operator added them
        # manually before formalizing the connection), force-update
        # the role + hide_identity to the canonical state.
        existing_lender_participant.role = ParticipantRole.LENDER
        existing_lender_participant.hide_identity = True
        if not existing_lender_participant.display_name:
            existing_lender_participant.display_name = lender.name
        if not existing_lender_participant.company:
            existing_lender_participant.company = lender.name

    # 3. Apply notify toggles. Map by id, validate before mutating so
    # one bad row doesn't leave a half-applied state.
    by_id: dict[UUID, LoanParticipant] = {p.id: p for p in loan.participants}
    for tog in notify_toggles:
        target = by_id.get(tog.participant_id)
        if target is None:
            raise LenderConnectError(
                f"Notify target {tog.participant_id} is not a participant on this loan."
            )
        if target.role == ParticipantRole.CLIENT:
            raise LenderConnectError(
                "Clients cannot be CC'd or BCC'd on lender mail. "
                "Remove the client from the notify list."
            )
        if target.role == ParticipantRole.LENDER:
            # No-op: never CC/BCC the lender on mail going TO the lender.
            continue
    for tog in notify_toggles:
        target = by_id.get(tog.participant_id)
        if target is None or target.role in (ParticipantRole.CLIENT, ParticipantRole.LENDER):
            continue
        target.cc_outbound = tog.cc_outbound
        target.bcc_outbound = tog.bcc_outbound

    cc_count = sum(
        1
        for p in loan.participants
        if p.role != ParticipantRole.LENDER and p.cc_outbound
    )
    bcc_count = sum(
        1
        for p in loan.participants
        if p.role != ParticipantRole.LENDER and p.bcc_outbound
    )

    # 4. Stage promotion (one-way only)
    stage_advanced = False
    old_stage = loan.stage
    if loan.stage in (LoanStage.PREQUALIFIED, LoanStage.COLLECTING_DOCS):
        loan.stage = LoanStage.LENDER_CONNECTED
        stage_advanced = True
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=actor_user_id,
                actor_label=actor_label,
                kind="loan.stage_change",
                summary=f"{old_stage} → {LoanStage.LENDER_CONNECTED}",
                payload={"from": str(old_stage), "to": str(LoanStage.LENDER_CONNECTED)},
            )
        )

    # Activity audit row for the connection itself — the email
    # orchestrator and the AI both consume Activity log to know
    # what happened on a deal.
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=actor_user_id,
            actor_label=actor_label,
            kind="loan.lender_connected",
            summary=f"Connected lender: {lender.name}",
            payload={
                "lender_id": str(lender.id),
                "lender_name": lender.name,
                "lender_email": lender_email,
                "cc_count": cc_count,
                "bcc_count": bcc_count,
                "stage_advanced": stage_advanced,
            },
        )
    )

    # Living Loan File treats lender connection as material — flag
    # for the next dirty-drain summarizer pass.
    await mark_loan_dirty(db, loan.id)

    await db.flush()
    log.info(
        "lender_connect: loan=%s lender=%s cc=%d bcc=%d stage_advanced=%s",
        loan.id, lender.id, cc_count, bcc_count, stage_advanced,
    )
    return ConnectResult(
        loan=loan,
        lender=lender,
        participant_id=existing_lender_participant.id,
        cc_count=cc_count,
        bcc_count=bcc_count,
        stage_advanced=stage_advanced,
    )


async def disconnect_lender(
    db: AsyncSession,
    *,
    loan_id: UUID,
    actor_user_id: UUID | None,
    actor_label: str,
) -> Loan:
    """Strips loan.lender_id and removes the LENDER participant row.
    Stage stays where it is — we never auto-regress."""
    loan = (
        await db.execute(
            select(Loan).options(selectinload(Loan.participants)).where(Loan.id == loan_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        raise LenderConnectError("Loan not found")

    prior_lender_id = loan.lender_id
    if prior_lender_id is None:
        raise LenderConnectError("No lender is connected to this loan.")

    loan.lender_id = None

    # Remove every LENDER participant on this loan. There SHOULD be
    # one, but we handle drift defensively.
    removed = 0
    for p in list(loan.participants):
        if p.role == ParticipantRole.LENDER:
            await db.delete(p)
            removed += 1

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=actor_user_id,
            actor_label=actor_label,
            kind="loan.lender_disconnected",
            summary="Lender disconnected from loan",
            payload={
                "prior_lender_id": str(prior_lender_id),
                "participants_removed": removed,
            },
        )
    )

    await mark_loan_dirty(db, loan.id)
    await db.flush()
    return loan
