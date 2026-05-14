"""Side effects we run AFTER a successful lender-thread AI extract.

Three propagations, each independently isolated so a single failure
doesn't block the others:

  1. Due-dated action items → CalendarEvent rows
     - One event per item that has a `due_date`.
     - external_ref_kind=AI_ACTION + external_ref_id="lender_extract:{item.id}"
       ensures we don't double-insert across re-extractions.
     - owner_user_id is resolved from item.owner:
         "client" → loan.client.user_id
         "broker" → loan.broker.user_id
         else     → None
     - Status flips to AI source (per CalendarEventSource policy:
       borrower never sees raw AI events without an approved AITask
       wrapper, so the visibility filter on /calendar handles
       audience scoping for us).

  2. Client-owned external action items → AITask rows
     - One task per (item.id, action="lender_request") combo,
       deduped against draft_payload.lender_extract_item_id.
     - Powers the existing cadence engine: tasks here get picked up
       by the chase-the-borrower flow without us building anything new.
     - confidence ← item.confidence (defaults to 0.85 if absent).
     - Internal items are SKIPPED — the cadence engine never chases
       a client for something they aren't supposed to know about.

  3. Lender-requested documents → noted on the AITask draft_payload
     - We do NOT auto-create RequiredDocument rows here. The doc-
     - intake state machine is owned elsewhere (loan_intake_automation)
       and has its own rules; piggybacking on it would risk false
       "missing" badges on the loan card. Instead the AITask carries
       the doc list so the cadence engine can render it in the
       outbound message templates.

Idempotency is the bedrock — the extractor regenerates the whole
extract on every poll/reply, and `extract_and_persist` rewrites
`loans.living_profile.lender_extract`. If we naively re-propagated,
we'd churn duplicate calendar events / tasks on every tick. The
`external_ref_id` + `draft_payload.lender_extract_item_id` dedup keys
prevent that.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import (
    AITaskPriority,
    AITaskSource,
    AITaskStatus,
    CalendarEventKind,
    CalendarEventSource,
    CalendarEventStatus,
    CalendarExternalRefKind,
)
from app.models.ai_task import AITask
from app.models.event import CalendarEvent
from app.models.loan import Loan

log = logging.getLogger(__name__)


async def propagate_extract(
    db: AsyncSession,
    *,
    loan_id: UUID,
    extract: dict[str, Any],
) -> dict[str, int]:
    """Run all side-effect propagations for a freshly-persisted extract.

    Returns counts for logging — does NOT commit; caller is responsible
    for `db.commit()` after this returns. Failure of any individual
    propagation is logged and counted but never raised."""
    counts = {
        "calendar_created": 0,
        "calendar_skipped_dedup": 0,
        "tasks_created": 0,
        "tasks_skipped_dedup": 0,
        "items_skipped_internal": 0,
    }
    items = (extract or {}).get("action_items") or []
    if not items:
        return counts

    loan = (
        await db.execute(
            select(Loan)
            .options(
                selectinload(Loan.client),
                selectinload(Loan.broker),
                selectinload(Loan.calendar_events),
                selectinload(Loan.ai_tasks),
            )
            .where(Loan.id == loan_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        return counts

    # Build dedup sets so a single pass is enough — re-running the
    # extractor 100 times must produce zero new rows.
    existing_cal_refs = {
        e.external_ref_id
        for e in loan.calendar_events
        if e.external_ref_kind == CalendarExternalRefKind.AI_ACTION
        and e.external_ref_id
        and e.external_ref_id.startswith("lender_extract:")
    }
    existing_task_item_ids: set[str] = set()
    for t in loan.ai_tasks:
        payload = t.draft_payload or {}
        item_id = payload.get("lender_extract_item_id")
        if isinstance(item_id, str):
            existing_task_item_ids.add(item_id)

    for item in items:
        try:
            await _propagate_one(
                db,
                loan=loan,
                item=item,
                counts=counts,
                existing_cal_refs=existing_cal_refs,
                existing_task_item_ids=existing_task_item_ids,
            )
        except Exception:
            log.exception(
                "lender_followup: propagation failed for item=%s (non-fatal)",
                (item or {}).get("id"),
            )

    log.info(
        "lender_followup: loan=%s %s",
        loan_id,
        " ".join(f"{k}={v}" for k, v in counts.items()),
    )
    return counts


async def _propagate_one(
    db: AsyncSession,
    *,
    loan: Loan,
    item: dict[str, Any],
    counts: dict[str, int],
    existing_cal_refs: set[str],
    existing_task_item_ids: set[str],
) -> None:
    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return
    sensitivity = (item.get("sensitivity") or "internal").lower()
    owner = (item.get("owner") or "").lower()
    summary = (item.get("summary") or "").strip()
    if not summary:
        return

    # ---- Calendar event (any owner with a due_date) ----
    due_date_str = (item.get("due_date") or "").strip() or None
    if due_date_str:
        ref_id = f"lender_extract:{item_id}"
        if ref_id in existing_cal_refs:
            counts["calendar_skipped_dedup"] += 1
        else:
            starts_at = _parse_due_date(due_date_str)
            if starts_at is not None:
                kind = _classify_kind(item)
                owner_user_id = _resolve_owner_user_id(loan, owner)
                db.add(
                    CalendarEvent(
                        loan_id=loan.id,
                        kind=kind,
                        title=summary[:200],
                        description=_render_description(item),
                        who=owner or None,
                        starts_at=starts_at,
                        duration_min=None,
                        priority=_map_priority(item.get("priority")),
                        status=CalendarEventStatus.PENDING,
                        source=CalendarEventSource.AI,
                        owner_user_id=owner_user_id,
                        external_ref_kind=CalendarExternalRefKind.AI_ACTION,
                        external_ref_id=ref_id,
                    )
                )
                existing_cal_refs.add(ref_id)
                counts["calendar_created"] += 1

    # ---- AITask for client-owned externals ----
    if owner == "client" and sensitivity == "external":
        if item_id in existing_task_item_ids:
            counts["tasks_skipped_dedup"] += 1
        else:
            confidence = float(item.get("confidence") or 0.85)
            db.add(
                AITask(
                    loan_id=loan.id,
                    source=AITaskSource.MESSAGES,
                    priority=_map_priority(item.get("priority"))
                    or AITaskPriority.MEDIUM,
                    status=AITaskStatus.PENDING,
                    action="lender_request",
                    title=summary[:255],
                    summary=_render_description(item),
                    confidence=confidence,
                    agent="lender_extractor",
                    draft_payload={
                        "lender_extract_item_id": item_id,
                        "requested_documents": item.get("requested_documents") or [],
                        "amounts": item.get("amounts") or [],
                        "named_people": item.get("named_people") or [],
                        "due_date": due_date_str,
                    },
                )
            )
            existing_task_item_ids.add(item_id)
            counts["tasks_created"] += 1
    elif sensitivity == "internal":
        counts["items_skipped_internal"] += 1


def _classify_kind(item: dict[str, Any]) -> CalendarEventKind:
    """Pick the best CalendarEventKind for the action item. Document
    requests → DOC; anything mentioning a call/meeting → CALL; closing
    milestones → CLOSING; rate lock → LOCK; else generic AI."""
    summary = (item.get("summary") or "").lower()
    if (item.get("requested_documents") or []):
        return CalendarEventKind.DOC
    if any(w in summary for w in ("call", "schedule a meeting", "phone")):
        return CalendarEventKind.CALL
    if "rate lock" in summary or "rate-lock" in summary:
        return CalendarEventKind.LOCK
    if "closing" in summary or "fund" in summary:
        return CalendarEventKind.CLOSING
    if "inspect" in summary or "appraisal" in summary:
        return CalendarEventKind.INSPECT
    return CalendarEventKind.AI


def _resolve_owner_user_id(loan: Loan, owner: str) -> UUID | None:
    """Map the extract's owner string to a User UUID for calendar
    scoping. Returns None when the mapping is ambiguous (unknown
    owner string, or the relationship row is missing)."""
    if owner == "client":
        return loan.client.user_id if loan.client else None
    if owner == "broker":
        return loan.broker.user_id if loan.broker else None
    # super_admin / lender don't get owner-scoped events; they show
    # up on the operator surfaces anyway.
    return None


def _parse_due_date(due: str) -> datetime | None:
    """Accept ISO date (YYYY-MM-DD) or full ISO datetime. Anything
    else is dropped — Calendar entries require a real timestamp.

    Pure date strings land at 09:00 UTC of that day (operator's
    typical morning glance; the existing reminder cadence already
    handles before/after."""
    if not due:
        return None
    s = due.strip()
    try:
        if "T" in s or " " in s:
            # ISO datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        # Pure date — land at 09:00 UTC of that day so the existing
        # reminder cadence has something concrete to fire against.
        y, m, d = (int(x) for x in s.split("-", 2))
        return datetime.combine(
            date(y, m, d),
            time(hour=9, minute=0, second=0),
            tzinfo=timezone.utc,
        )
    except Exception:
        log.warning("lender_followup: unparseable due_date=%r", due)
        return None


def _map_priority(p: Any) -> AITaskPriority | None:
    if not p:
        return None
    s = str(p).lower()
    if s == "high":
        return AITaskPriority.HIGH
    if s in ("med", "medium"):
        return AITaskPriority.MEDIUM
    if s == "low":
        return AITaskPriority.LOW
    return None


def _render_description(item: dict[str, Any]) -> str:
    """Compact human-readable description for the calendar tooltip
    and AITask summary field. Surfaces the structured fields the AI
    extracted so the operator/borrower doesn't have to open the
    audit drawer to see them."""
    parts: list[str] = [str(item.get("summary") or "").strip()]
    docs = item.get("requested_documents") or []
    if docs:
        parts.append("Documents: " + ", ".join(docs[:5]))
    amts = item.get("amounts") or []
    if amts:
        parts.append("Amounts: " + ", ".join(amts[:5]))
    ppl = item.get("named_people") or []
    if ppl:
        parts.append("People: " + ", ".join(ppl[:5]))
    return "\n".join(p for p in parts if p)
