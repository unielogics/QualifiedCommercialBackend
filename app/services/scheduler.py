"""In-process APScheduler for recurring AI / calendar / digest work.

⚠️ SINGLE-INSTANCE ASSUMPTION ⚠️
This scheduler runs inside every FastAPI worker. As long as we deploy
to one EC2 instance with `systemctl restart qcbackend`, only one job
fires per cron tick. The moment we add a second backend instance (or
scale uvicorn to multiple workers), this becomes a double-fire bug.

When that happens, the migration path is:
  1. Switch to AWS EventBridge → POST /admin/cron/tick?job=...
     (the FRED router already wants this; second consumer ratifies
     the pattern). Add a shared-secret token check.
  2. Either remove `start_scheduler()` from main.py or guard it on a
     "primary instance" env flag.

Until then: simple, local, no external dependencies.

Job catalog (registered in `start_scheduler`):
  summary_dirty_drain  every 5 min  refresh Living Loan File on dirty rows
  doc_reminders        cron 9am UTC reminders + escalations on stale docs
  calendar_lookahead   cron 7am UTC pre-emit reminder Activity rows
  account_summary      cron 3am UTC per-client summarizer
  pipeline_scan        cron 8am UTC stalled-loan / anomaly digest

The drain job is the hot path — it gets touched any time a Loan
flips `summary_dirty=True` (Phase 6). Cron jobs share the same
SessionLocal pattern as the rest of the app and are individually
exception-isolated so one failing job doesn't take down the others.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler() -> None:
    """Idempotent — safe to call from FastAPI startup hooks (which fire
    on every uvicorn reload in dev). `replace_existing=True` collapses
    duplicate adds into the same job.

    Each job is a coroutine that opens its own DB session. Jobs are
    individually try/except'd inside the wrapper functions so one
    failure doesn't stop the scheduler — APScheduler logs the
    exception and the next tick runs normally.
    """
    if scheduler.running:
        log.info("scheduler already running; skipping start")
        return

    # Phase 6 — debounced summarizer drain. Every 5 min picks up any
    # Loan with summary_dirty=True and refreshes its Living Loan File.
    scheduler.add_job(
        _wrap(job_summary_dirty_drain),
        "interval",
        minutes=5,
        id="summary_dirty_drain",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 5 (deferred) — prequal auto-approval evaluator. Runs every
    # 2 minutes. Picks up pending requests, runs the deterministic
    # gate, and either auto-approves (renders the PDF, flips status)
    # or stamps blockers on admin_notes for operator review.
    scheduler.add_job(
        _wrap(job_evaluate_pending_prequals),
        "interval",
        minutes=2,
        id="evaluate_pending_prequals",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 4 — daily doc reminder + escalation pass.
    scheduler.add_job(
        _wrap(job_doc_reminders),
        "cron",
        hour=9,
        minute=0,
        id="doc_reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 7 — write `Activity(kind='calendar.reminder')` rows for
    # tomorrow's calendar items. Future notifier will read these.
    scheduler.add_job(
        _wrap(job_calendar_lookahead),
        "cron",
        hour=7,
        minute=0,
        id="calendar_lookahead",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 8 — per-client account-wide AI refresh.
    scheduler.add_job(
        _wrap(job_account_summary_refresh),
        "cron",
        hour=3,
        minute=0,
        id="account_summary_refresh",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 10 — pipeline digest (stalled deals, anomalies).
    scheduler.add_job(
        _wrap(job_pipeline_scan),
        "cron",
        hour=8,
        minute=0,
        id="pipeline_scan",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Daily FRED pull — keeps fred_observations fresh so the app +
    # dashboard "today's rates" and the public marketing program-page
    # 30-day charts read live data. ~06:15 UTC (after US close).
    scheduler.add_job(
        _wrap(job_fred_refresh),
        "cron",
        hour=6,
        minute=15,
        id="fred_refresh",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Doc-vision scan drain (alembic 0017). Every 2 min picks up
    # Documents flagged scan_dirty=True (set by upload-complete) and
    # runs them through Claude vision. Hard-capped at 8 docs per
    # tick — bounds peak Anthropic spend during a mass upload.
    scheduler.add_job(
        _wrap(job_document_scan_drain),
        "interval",
        minutes=2,
        id="document_scan_drain",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Cadence engine (Phase 5, alembic 0032). Every 30 min walks
    # ai_cadence_rules + spawns draft messages / tasks / escalations.
    # Draft-first by default — auto-send is opt-in per rule.
    scheduler.add_job(
        _wrap(job_cadence_pass),
        "interval",
        minutes=30,
        id="cadence_pass",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Lender thread inbound poller. Every 60 seconds, pulls
    # `is:unread subject:"[QC-"` from the delegated Gmail mailbox and
    # writes Message(from_role=LENDER) rows so the LenderThread UI
    # picks them up. Gated by USE_FAKE_INBOX=false + Gmail config
    # present — otherwise no-ops. See inbound_poller.run_inbound_poll.
    scheduler.add_job(
        _wrap(job_lender_inbound_poll),
        "interval",
        seconds=60,
        id="lender_inbound_poll",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler shut down")


def _wrap(coro_fn):
    """APScheduler wrapper that prefixes log lines with the job id and
    swallows exceptions so a single failure doesn't poison the next
    tick. Returns the wrapped coroutine factory."""

    async def runner():
        name = coro_fn.__name__
        started = datetime.now(timezone.utc)
        try:
            await coro_fn()
        except Exception:
            log.exception("scheduler job %s failed", name)
        finally:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            log.info("scheduler job %s finished in %.2fs", name, elapsed)

    return runner


# ── Job implementations ────────────────────────────────────────────────
#
# Each job is a thin shell here. Real logic lives in the phase-specific
# service that owns it (loan_intake_automation, summarizer, etc.). This
# file is only the schedule registry.


async def job_summary_dirty_drain() -> None:
    """Phase 6 — refresh Living Loan File on every Loan that's been
    flagged dirty since the last drain. Hard-capped at 20 loans per
    tick to bound Anthropic spend."""
    from app.services.activity_log import drain_summary_dirty  # local import to avoid circular

    await drain_summary_dirty(limit=20)


async def job_evaluate_pending_prequals() -> None:
    """Phase 5 (deferred) — auto-approval pass over pending prequals.
    Local import to avoid pulling the prequal router into module
    init."""
    from app.services.prequal_evaluator import evaluate_pending_requests

    await evaluate_pending_requests(limit=20)


async def job_doc_reminders() -> None:
    """Phase 4 — reminders + escalations for outstanding docs."""
    from app.services.loan_intake_automation import evaluate_doc_reminders

    await evaluate_doc_reminders()


async def job_calendar_lookahead() -> None:
    """Phase 7+ — emit `Activity(kind='calendar.reminder')` rows for
    every pending calendar event whose `starts_at` falls in the next
    24h. The future notifier (email/SMS) consumes these activities."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.enums import CalendarEventStatus
    from app.models.activity import Activity
    from app.models.event import CalendarEvent
    from datetime import timedelta

    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=24)
        rows = (
            await db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.status == CalendarEventStatus.PENDING,
                    CalendarEvent.starts_at >= now,
                    CalendarEvent.starts_at <= horizon,
                )
            )
        ).scalars().all()
        emitted = 0
        for ev in rows:
            db.add(
                Activity(
                    loan_id=ev.loan_id,
                    actor_id=None,
                    actor_label="ai",
                    kind="calendar.reminder",
                    summary=f"Calendar reminder: {ev.title}",
                    payload={
                        "event_id": str(ev.id),
                        "starts_at": ev.starts_at.isoformat(),
                        "owner_user_id": str(ev.owner_user_id) if ev.owner_user_id else None,
                        "kind": ev.kind,
                        "priority": ev.priority,
                    },
                )
            )
            emitted += 1
        await db.commit()
        log.info("calendar_lookahead emitted=%d horizon=24h", emitted)


async def job_account_summary_refresh() -> None:
    """Phase 8 — refresh per-client living profile across all active
    clients. Hard-capped at 50 clients per tick."""
    from app.services.ai.client_summarizer import refresh_all_active_clients

    await refresh_all_active_clients(limit=50)


async def job_pipeline_scan() -> None:
    """Phase 10 — pipeline digest. Stalled deals, expiring credit
    pulls, rate-pressure clusters, prequal closing windows."""
    from app.services.ai.pipeline_digest import scan_pipeline

    await scan_pipeline()


async def job_fred_refresh() -> None:
    """Daily pull of every tracked FRED series → upsert into
    fred_observations. Powers the in-app 'today's rates' widgets and
    the public program-page 30-day charts. No-ops (logs) if
    FRED_API_KEY is unset."""
    from app.db import SessionLocal
    from app.services import fred as fred_service

    async with SessionLocal() as db:
        try:
            summary = await fred_service.refresh_all(db)
            await db.commit()
            log.info("fred_refresh: %s", summary)
        except Exception:
            await db.rollback()
            log.exception("fred_refresh: failed; rolled back")


async def job_cadence_pass() -> None:
    """Phase 5 — walk ai_cadence_rules every 30 min and fire
    eligible actions. Draft-first: borrower-facing messages always
    queue as AI Inbox drafts, not direct sends, unless the rule
    explicitly opts into auto-send."""
    from app.db import SessionLocal
    from app.services.ai.cadence_engine import run_cadence_pass

    async with SessionLocal() as db:
        try:
            stats = await run_cadence_pass(db)
            await db.commit()
            log.info("cadence_pass: %s", stats)
        except Exception:
            await db.rollback()
            log.exception("cadence_pass: failed; rolled back")


async def job_document_scan_drain() -> None:
    """Vision-scan freshly-uploaded Documents flagged
    `scan_dirty=True`. Hard-cap 8 per tick — bounds Anthropic spend
    during a mass upload. Per-doc failure is isolated; one bad
    file doesn't poison the rest of the batch.

    Cost discipline: empty ticks cost nothing (no docs flagged →
    no Anthropic calls). Realistic steady state at borrower-side
    volume is single-digit scans per day.
    """
    import asyncio

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.document import Document
    from app.services.document_scanner import scan_document

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Document.id)
                .where(Document.scan_dirty.is_(True))
                .order_by(Document.created_at.asc())
                .limit(8)
            )
        ).scalars().all()
        if not rows:
            return
        log.info("document_scan_drain: scanning %d docs", len(rows))
        for doc_id in rows:
            try:
                await scan_document(db, doc_id)
                await db.commit()
            except Exception:  # noqa: BLE001
                log.exception("document_scan_drain: scan failed for %s", doc_id)
                await db.rollback()
            await asyncio.sleep(0)


async def job_lender_inbound_poll() -> None:
    """Pull new lender-tagged emails from Gmail every 60s and append
    them to the LenderThread mailbox. Single-instance assumption (one
    container = one poller). Self-no-ops when USE_FAKE_INBOX=true or
    when GMAIL_* env vars aren't set."""
    from app.services.email.inbound_poller import run_inbound_poll

    await run_inbound_poll()
