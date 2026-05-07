"""Persist `next_actions_v2` from the summarizer as calendar events
or AI tasks (Phase 7).

  owner=ai     → CalendarEvent(source='ai') via calendar_emitter.upsert_event
                  ext_kind='ai_action', ext_id=hash(loan, title, week_bucket)
  owner=broker → AITask(source=CALENDAR, status=PENDING) — admin
                  approves through /ai-tasks/{id}/decision before it
                  hits anyone's calendar.
  owner=client → broker AITask first; surfaces in client view only
                  after broker approval.

Idempotency: the week-bucket in the ext_id key prevents the LLM from
spawning five copies of "Watch for tax returns" across consecutive
daily refreshes — same logical action collapses to one row per week.

Engagement-paused loans short-circuit (no writes at all). The pause
flag is the operator's "I'm taking over here, AI stay out" lever.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    AITaskPriority,
    AITaskSource,
    AITaskStatus,
    CalendarEventKind,
    CalendarExternalRefKind,
)
from app.models.ai_task import AITask
from app.models.event import CalendarEvent
from app.models.loan import Loan
from app.services import calendar_emitter
from app.services.ai import engagement

log = logging.getLogger(__name__)

# Per-loan cap on open AI calendar events. When the LLM produces more
# we evict the oldest. Prevents the calendar from drowning in stale
# AI suggestions if the borrower goes quiet for weeks.
_AI_EVENTS_CAP_PER_LOAN = 5

# When neither due_at nor relative_days is provided, default to
# tomorrow at 14:00 UTC (~9am ET) low-priority. Bias toward "soon"
# so the operator at least sees the suggestion.
_FALLBACK_TIME = time(hour=14, tzinfo=timezone.utc)


def _priority_to_enum(priority: str) -> AITaskPriority:
    p = priority.lower()
    if p == "high":
        return AITaskPriority.HIGH
    if p == "low":
        return AITaskPriority.LOW
    return AITaskPriority.MEDIUM


def _kind_to_calendar(kind: str) -> str:
    """Map the typed `kind` to a CalendarEventKind value. Defensively
    falls back to AI when the kind isn't recognized."""
    try:
        return CalendarEventKind(kind).value
    except ValueError:
        return CalendarEventKind.AI.value


def _resolve_starts_at(action: dict[str, Any]) -> datetime:
    """Prefer due_at (absolute ISO8601) over relative_days. Fall back
    to tomorrow @ 14:00 UTC if neither is supplied or both fail
    parse. The point is to never drop a typed action on the floor
    just because the LLM misformatted the date."""
    raw = action.get("due_at")
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            pass

    rel = action.get("relative_days")
    if rel is not None:
        try:
            target = date.today() + timedelta(days=int(rel))
            return datetime.combine(target, _FALLBACK_TIME)
        except (ValueError, TypeError):
            pass

    return datetime.combine(date.today() + timedelta(days=1), _FALLBACK_TIME)


def _week_bucket(dt: datetime) -> str:
    """ISO-week stamp. Two AI suggestions in the same week with the
    same loan + title collapse to one calendar row."""
    iso = dt.isocalendar()
    return f"{iso.year}W{iso.week:02d}"


def _ai_action_ext_id(loan_id: UUID, title: str, starts_at: datetime) -> str:
    """Deterministic key — loan+title+ISO-week. Same suggestion in
    the same week, same row. Different week, different row (makes
    space for the LLM to re-emit a stale suggestion as urgency
    rises)."""
    norm_title = " ".join(title.lower().split())
    digest = hashlib.sha256(
        f"{loan_id}|{norm_title}|{_week_bucket(starts_at)}".encode("utf-8")
    ).hexdigest()[:16]
    return digest


async def _enforce_per_loan_cap(db: AsyncSession, loan_id: UUID) -> None:
    """Evict the oldest open `source='ai'` event when the per-loan
    cap is exceeded. Soft cap — keeps the calendar manageable while
    leaving the most recent suggestions visible."""
    rows = (
        await db.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.loan_id == loan_id,
                CalendarEvent.source == "ai",
                CalendarEvent.status == "pending",
            )
            .order_by(CalendarEvent.created_at.asc())
        )
    ).scalars().all()
    if len(rows) <= _AI_EVENTS_CAP_PER_LOAN:
        return
    excess = rows[: len(rows) - _AI_EVENTS_CAP_PER_LOAN]
    for r in excess:
        r.status = "cancelled"


async def persist_next_actions(
    db: AsyncSession,
    loan: Loan,
    actions: list[dict[str, Any]],
) -> tuple[int, int]:
    """Persist a list of typed next_actions onto the calendar /
    AITask queue. Returns (calendar_events_created, ai_tasks_created).

    Engagement-paused loans return (0, 0) — the AI is muted.
    """
    if engagement.is_paused(loan):
        log.debug("calendar_intent: skipping paused loan=%s", loan.id)
        return 0, 0
    if not actions:
        return 0, 0

    cal_count = 0
    task_count = 0

    for action in actions:
        title = (action.get("title") or "").strip()
        if not title:
            continue

        owner = action.get("owner") or "broker"
        starts_at = _resolve_starts_at(action)

        if owner == "ai":
            # Land directly on the calendar as source='ai'.
            ext_id = _ai_action_ext_id(loan.id, title, starts_at)
            await calendar_emitter.upsert_event(
                db,
                ext_kind=CalendarExternalRefKind.AI_ACTION,
                ext_id=ext_id,
                defaults={
                    "loan_id": loan.id,
                    "kind": _kind_to_calendar(action.get("kind") or "ai"),
                    "title": title,
                    "description": (
                        "AI-suggested action. Auto-emitted from the "
                        "loan summarizer's next_actions output."
                    ),
                    "who": "AI",
                    "starts_at": starts_at,
                    "duration_min": 0,
                    "priority": _priority_to_enum(
                        action.get("priority") or "med"
                    ).value,
                    "source": "ai",
                },
            )
            cal_count += 1
            continue

        # owner in {broker, client} → AITask awaiting approval
        priority = _priority_to_enum(action.get("priority") or "med")
        action_kind = "scheduled_followup"
        if owner == "client":
            action_kind = "borrower_action"
        db.add(
            AITask(
                loan_id=loan.id,
                source=AITaskSource.CALENDAR,
                priority=priority,
                status=AITaskStatus.PENDING,
                action=action_kind,
                title=title,
                summary=(
                    f"Suggested {owner} action — due "
                    f"{starts_at.strftime('%Y-%m-%d %H:%M UTC')}. "
                    f"Approve to schedule on the calendar."
                ),
                confidence=0.7,
                agent="summarizer",
                draft_payload={
                    **action,
                    "starts_at": starts_at.isoformat(),
                    "loan_id": str(loan.id),
                },
            )
        )
        task_count += 1

    if cal_count > 0:
        await _enforce_per_loan_cap(db, loan.id)

    log.info(
        "calendar_intent: loan=%s calendar=%d ai_tasks=%d",
        loan.id, cal_count, task_count,
    )
    return cal_count, task_count
