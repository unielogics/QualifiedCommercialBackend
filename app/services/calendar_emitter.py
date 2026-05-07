"""Calendar emitter — the single place that creates `source='auto'`
calendar events from lifecycle transitions.

Every entry function is **idempotent**: it upserts on the
`(external_ref_kind, external_ref_id)` partial unique index added in
alembic 0013. Re-running a transition (e.g. operator clicks the same
PATCH twice, or a backfill script re-iterates existing rows) produces
no duplicates — only field-level updates if the underlying state has
changed.

A retreat (e.g. CLOSING → PROCESSING) calls `cancel_for(...)` which
flips the existing event's status to `cancelled`. We never delete
auto events; the audit trail and any AI feedback ride along.

Public surface:
  upsert_event(...)
  emit_for_loan_stage(...)
  emit_for_loan_close(...)
  emit_for_credit_pull(...)
  emit_for_document_request(...)
  emit_for_document_received(...)
  emit_for_prequal_approval(...)
  cancel_for(...)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    AITaskPriority,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
    CalendarExternalRefKind,
    LoanStage,
)
from app.models.credit_pull import CreditPull
from app.models.document import Document
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest

log = logging.getLogger(__name__)

# Default time-of-day for events that don't have a precise hour.
# 09:00 local-ish (we use UTC noon as a proxy for "morning anywhere
# in the continental US"). Phase 4+ will read the firm's office TZ
# from LetterheadSettings to do this correctly.
_DEFAULT_HOUR_UTC = 14  # ≈ 9am ET


def _at_default_hour(d: date) -> datetime:
    """Promote a date to a tz-aware datetime at the default hour."""
    return datetime.combine(d, time(hour=_DEFAULT_HOUR_UTC, tzinfo=timezone.utc))


async def upsert_event(
    db: AsyncSession,
    *,
    ext_kind: CalendarExternalRefKind | str,
    ext_id: str,
    defaults: dict[str, Any],
) -> CalendarEvent:
    """Idempotent insert keyed on (external_ref_kind, external_ref_id).

    We use the postgres dialect's INSERT ... ON CONFLICT to avoid a
    SELECT-then-INSERT race. The partial unique index in 0013
    (`ix_calendar_events_external WHERE external_ref_kind IS NOT
    NULL`) is the conflict target.

    On conflict we update exactly the fields the caller put in
    `defaults` (plus updated_at via the TimestampMixin trigger
    semantics — SQLAlchemy doesn't auto-update on raw INSERT, so we
    set updated_at explicitly here for parity).

    Returns the resulting row freshly re-fetched."""
    ext_kind_str = ext_kind.value if hasattr(ext_kind, "value") else str(ext_kind)

    # Always source='auto' on rows the emitter creates. Status is
    # 'pending' on insert; updates from caller only touch the data
    # fields (title / starts_at / etc.) — they don't bounce a 'done'
    # row back to 'pending'.
    payload = {
        "external_ref_kind": ext_kind_str,
        "external_ref_id": ext_id,
        "source": CalendarEventSource.AUTO.value,
        "status": CalendarEventStatus.PENDING.value,
        **defaults,
    }
    now = datetime.now(timezone.utc)
    payload.setdefault("updated_at", now)

    stmt = insert(CalendarEvent).values(**payload)
    update_cols = {k: stmt.excluded[k] for k in defaults.keys()}
    update_cols["updated_at"] = stmt.excluded.updated_at
    stmt = stmt.on_conflict_do_update(
        index_elements=["external_ref_kind", "external_ref_id"],
        index_where=CalendarEvent.external_ref_kind.is_not(None),
        set_=update_cols,
    ).returning(CalendarEvent.id)
    inserted_id = (await db.execute(stmt)).scalar_one()
    await db.flush()
    ev = await db.get(CalendarEvent, inserted_id)
    if ev is None:
        raise RuntimeError(f"upsert_event: row {inserted_id} disappeared after upsert")
    return ev


async def cancel_for(
    db: AsyncSession,
    ext_kind: CalendarExternalRefKind | str,
    ext_id: str,
) -> bool:
    """Flip an existing auto event's status to cancelled. Returns
    True when a row was found, False otherwise. Idempotent — calling
    twice has the same effect as once. We never delete the row; the
    audit trail and any AI feedback stay attached."""
    ext_kind_str = ext_kind.value if hasattr(ext_kind, "value") else str(ext_kind)
    stmt = select(CalendarEvent).where(
        CalendarEvent.external_ref_kind == ext_kind_str,
        CalendarEvent.external_ref_id == ext_id,
    )
    ev = (await db.execute(stmt)).scalar_one_or_none()
    if ev is None:
        return False
    ev.status = CalendarEventStatus.CANCELLED
    await db.flush()
    return True


# ── loan stage / close ──────────────────────────────────────────────────


async def emit_for_loan_stage(
    db: AsyncSession,
    loan: Loan,
    prev_stage: LoanStage | str | None,
    new_stage: LoanStage | str,
) -> None:
    """Emit on stage transitions. Today this fires the close milestone
    when CLOSING is entered (and cancels it if CLOSING is left).
    Other stages are audit-trailed via Activity but don't yet emit
    calendar events — they will when Phase 4 introduces stage-specific
    follow-up emitters."""
    new_str = new_stage.value if hasattr(new_stage, "value") else str(new_stage)

    if new_str == LoanStage.CLOSING.value:
        await emit_for_loan_close(db, loan)
        return

    # Retreat: was CLOSING, now isn't. Cancel the close milestone so
    # the calendar doesn't promise a date that's no longer real.
    if prev_stage is not None:
        prev_str = prev_stage.value if hasattr(prev_stage, "value") else str(prev_stage)
        if prev_str == LoanStage.CLOSING.value and new_str != LoanStage.CLOSING.value:
            await cancel_for(db, CalendarExternalRefKind.LOAN_CLOSE, str(loan.id))


async def emit_for_loan_close(db: AsyncSession, loan: Loan) -> None:
    """One CLOSING calendar event per loan, dated `loan.close_date`
    (falls back to today + 30d when none is set, so the operator
    sees something on the calendar to chase)."""
    target_date = loan.close_date or (date.today() + timedelta(days=30))
    starts_at = _at_default_hour(target_date)
    title = f"Closing — {loan.deal_id} · {loan.address}"
    await upsert_event(
        db,
        ext_kind=CalendarExternalRefKind.LOAN_CLOSE,
        ext_id=str(loan.id),
        defaults={
            "loan_id": loan.id,
            "kind": CalendarEventKind.CLOSING.value,
            "title": title,
            "description": (
                f"Targeted closing for loan {loan.deal_id}. Date is "
                f"{'set' if loan.close_date else 'estimated (no close_date on file)'}."
            ),
            "who": "Title Co.",
            "starts_at": starts_at,
            "duration_min": 90,
            "priority": AITaskPriority.HIGH.value,
        },
    )


# ── credit pulls ────────────────────────────────────────────────────────


async def emit_for_credit_pull(db: AsyncSession, pull: CreditPull) -> None:
    """Emit a credit-expiry milestone so operators get a calendar
    nudge before the soft pull stales (typically 90 days). We don't
    bother emitting a "pulled today" event — the audit log + the
    borrower's profile carry that already.

    No `loan_id` — credit pulls are client-scoped, not loan-scoped.
    The owner is the borrower's user_id when we can resolve it."""
    if pull.expires_at is None:
        return  # Nothing to schedule

    title = f"Credit pull expires for {pull.legal_first_name} {pull.legal_last_name}".strip() or "Credit pull expires"
    await upsert_event(
        db,
        ext_kind=CalendarExternalRefKind.CREDIT_EXPIRY,
        ext_id=str(pull.id),
        defaults={
            "loan_id": None,
            "kind": CalendarEventKind.MILESTONE.value,
            "title": title,
            "description": (
                "Soft credit pull is approaching expiration. Refresh before any "
                "new product offers go out — pricing matrix lookups assume a "
                "valid pull."
            ),
            "who": "Underwriting",
            "starts_at": pull.expires_at,
            "duration_min": 0,
            "priority": AITaskPriority.MEDIUM.value,
        },
    )


# ── documents ───────────────────────────────────────────────────────────


async def emit_for_document_request(
    db: AsyncSession,
    doc: Document,
    *,
    due_in_days: int = 7,
) -> None:
    """One DOC calendar event per requested document, due
    `requested_on + due_in_days`. Phase 4 will swap the constant for
    `LoanTypeChecklist.first_reminder_days`."""
    if doc.requested_on is None:
        return
    due = doc.requested_on + timedelta(days=due_in_days)
    starts_at = _at_default_hour(due)
    title = f"Doc due: {doc.name}"
    await upsert_event(
        db,
        ext_kind=CalendarExternalRefKind.DOCUMENT_DUE,
        ext_id=str(doc.id),
        defaults={
            "loan_id": doc.loan_id,
            "kind": CalendarEventKind.DOC.value,
            "title": title,
            "description": (
                f"{doc.name} requested on {doc.requested_on.isoformat()}. "
                f"Reminder cadence drives this date — operators may extend "
                f"by editing the underlying document."
            ),
            "who": "Borrower upload",
            "starts_at": starts_at,
            "duration_min": 0,
            "priority": AITaskPriority.MEDIUM.value,
        },
    )


async def emit_for_document_received(db: AsyncSession, doc: Document) -> None:
    """When a doc lands, mark its prior 'document_due' event as done
    so the calendar reflects reality. Idempotent — receiving twice
    just leaves the row done."""
    stmt = select(CalendarEvent).where(
        CalendarEvent.external_ref_kind == CalendarExternalRefKind.DOCUMENT_DUE.value,
        CalendarEvent.external_ref_id == str(doc.id),
    )
    ev = (await db.execute(stmt)).scalar_one_or_none()
    if ev is None:
        return
    if ev.status != CalendarEventStatus.DONE:
        ev.status = CalendarEventStatus.DONE
        await db.flush()


# ── prequalifications ───────────────────────────────────────────────────


async def emit_for_prequal_approval(db: AsyncSession, req: PrequalRequest) -> None:
    """Approved prequals with an `expected_closing_date` get a
    CLOSING milestone so operators see urgency rolling toward the
    seller-accept window. Skips when no closing date is on file —
    no point pinning a milestone to nothing."""
    if req.expected_closing_date is None:
        return
    starts_at = _at_default_hour(req.expected_closing_date)
    quote = req.quote_number or "no quote#"
    title = f"Pre-qual closing window — {quote} · {req.target_property_address}"
    await upsert_event(
        db,
        ext_kind=CalendarExternalRefKind.PREQUAL_CLOSE,
        ext_id=str(req.id),
        defaults={
            "loan_id": req.loan_id,  # may be None pre-acceptance
            "kind": CalendarEventKind.CLOSING.value,
            "title": title,
            "description": (
                "Borrower's expected closing date on the issued pre-qualification "
                "letter. Ping the borrower 5-7 days before to confirm the offer "
                "outcome."
            ),
            "who": req.target_property_address,
            "starts_at": starts_at,
            "duration_min": 0,
            "priority": AITaskPriority.MEDIUM.value,
        },
    )
