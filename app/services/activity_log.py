"""Activity-log helper + summary-dirty drain.

Two responsibilities:

1. `log_activity(db, *, loan_id, ...)` — drop-in replacement for the
   `db.add(Activity(...))` calls scattered across routers. ALSO
   flips the parent `Loan.summary_dirty=True` flag so the next drain
   tick picks it up. Routers gradually migrate to this; for now the
   highest-traffic call sites are touched (chat, prequal approve,
   doc receipt). Idempotency: setting `summary_dirty=True` twice is
   a no-op.

2. `drain_summary_dirty(limit=N)` — scheduler job (every 5 min). Picks
   up to N dirty Loan rows and runs the summarizer on each. Bounds
   Anthropic spend by `limit` per tick; failed refreshes leave the
   dirty flag set so they retry next tick.

Why a flag rather than synchronous summarizer-on-write?
  - Summarizer call to Haiku is ~1-2s. Doing it inside a request
    blocks the API.
  - Activity inserts often come in bursts (operator updates 5 fields,
    each writes an Activity). We want one summary refresh per burst,
    not five.
  - Dirty + drain keeps the model fresh-enough (5-min worst-case lag)
    without coupling refresh latency to user-facing requests.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.activity import Activity
from app.models.loan import Loan
from app.services.ai import engagement, summarizer

log = logging.getLogger(__name__)


async def log_activity(
    db: AsyncSession,
    *,
    loan_id: UUID | None,
    actor_id: UUID | None = None,
    actor_label: str | None = None,
    kind: str,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    mark_dirty: bool = True,
) -> Activity:
    """Drop-in for `db.add(Activity(...))` that ALSO flips the parent
    Loan's `summary_dirty=True` so the dirty-drain job picks it up.

    `mark_dirty=False` opts out — used for the dirty-drain job's own
    success Activity rows so we don't infinite-loop ourselves.

    Returns the Activity (already in the session, not yet committed)."""
    act = Activity(
        loan_id=loan_id,
        actor_id=actor_id,
        actor_label=actor_label,
        kind=kind,
        summary=summary,
        payload=payload,
    )
    db.add(act)
    if mark_dirty and loan_id is not None:
        loan = await db.get(Loan, loan_id)
        if loan is not None and not loan.summary_dirty:
            loan.summary_dirty = True
    return act


async def mark_loan_dirty(db: AsyncSession, loan_id: UUID) -> None:
    """Plain dirty-flag flipper for paths that are creating Activity
    rows directly (not through log_activity). Cheap, idempotent."""
    loan = await db.get(Loan, loan_id)
    if loan is not None and not loan.summary_dirty:
        loan.summary_dirty = True


async def drain_summary_dirty(*, limit: int = 20) -> int:
    """Scheduler job (every 5 min). Picks up to `limit` Loan rows
    where summary_dirty=True, runs the summarizer on each, then
    clears the flag. Failures leave the flag set so the next tick
    retries.

    Returns the count of successful refreshes (for telemetry).

    Bounded concurrency: we run summaries SEQUENTIALLY (with an
    asyncio.sleep(0) between iterations so the event loop stays
    responsive). The summarizer hits Anthropic; running 20 in
    parallel would burst the rate limit and fight the orchestrator
    for tokens. Sequential at ~1-2s each = ~20-40s per tick which
    fits comfortably inside the 5-min interval.

    Engagement-paused loans are short-circuited (no LLM call) but
    we still clear the dirty flag so they don't accumulate forever.
    The next user-driven event will set the flag again."""
    refreshed = 0
    failed = 0

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Loan).where(Loan.summary_dirty == True).limit(limit)
            )
        ).scalars().all()

        for loan in rows:
            # Engagement gate — don't burn LLM tokens on paused loans.
            # `is_paused` is sync; takes the loan instance directly.
            if engagement.is_paused(loan):
                loan.summary_dirty = False
                loan.summary_refreshed_at = datetime.now(timezone.utc)
                continue

            try:
                await summarizer.refresh_summary(db, loan.id)
                loan.summary_dirty = False
                loan.summary_refreshed_at = datetime.now(timezone.utc)
                refreshed += 1
            except Exception:
                # Leave dirty=True so the next tick retries. The
                # exception is logged but doesn't propagate — we want
                # the rest of the batch to keep flowing.
                log.exception(
                    "drain_summary_dirty: refresh failed loan=%s deal_id=%s",
                    loan.id, getattr(loan, "deal_id", "?"),
                )
                failed += 1

            # Yield to the event loop so the API stays responsive.
            await asyncio.sleep(0)

        await db.commit()

    log.info(
        "drain_summary_dirty: refreshed=%d failed=%d limit=%d",
        refreshed, failed, limit,
    )
    return refreshed
