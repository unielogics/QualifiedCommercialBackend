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

    scheduler.add_job(
        _wrap(job_bucket_ai_review_drain),
        "interval",
        minutes=1,
        id="bucket_ai_review_drain",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    scheduler.add_job(
        _wrap(job_bucket_file_analysis_drain),
        "interval",
        minutes=1,
        id="bucket_file_analysis_drain",
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

    # Google Calendar pull — fold each connected user's Google changes back into
    # internal calendar_events every 5 min (incremental sync token; idempotent on
    # google_event_id so a double-fire is harmless). Push (internal→Google) is
    # inline on the calendar write path, not here. No-ops when nobody has a
    # connected calendar. Single-instance assumption (see file header).
    scheduler.add_job(
        _wrap(job_google_calendar_pull),
        "interval",
        minutes=5,
        id="google_calendar_pull",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Phase 4 — Workspace-mailbox inbox sync every 2 min. Reads the delegated
    # mailbox into the EmailMessage store (bodies encrypted, matched to loan/client,
    # body-less breadcrumb Activity). No-ops unless user_inbox_sync_enabled AND
    # Gmail DWD is configured — ships dormant. Idempotent (dedup on gmail id).
    scheduler.add_job(
        _wrap(job_user_inbox_sync),
        "interval",
        minutes=2,
        id="user_inbox_sync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Gmail Pub/Sub watch renewal. A users.watch() registration expires
    # after ~7 days; we re-register every 24h (and once ~10s after
    # startup, so a fresh deploy re-arms push immediately). No-ops when
    # GMAIL_PUBSUB_TOPIC isn't set — the 60s poller above is the
    # fallback. See app/services/email/gmail_push.register_gmail_watch.
    from datetime import timedelta as _timedelta

    scheduler.add_job(
        _wrap(job_gmail_watch_renew),
        "interval",
        hours=24,
        id="gmail_watch_renew",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + _timedelta(seconds=10),
    )

    # AI re-engagement escalation. Hourly: walks active loans and, for
    # any borrower who's gone quiet on a time-sensitive file, escalates
    # outreach (email auto-send via SES; SMS/WhatsApp draft-first).
    # Self-no-ops without an Anthropic key; email rung dormant until
    # SES is configured. See app/services/ai/reengagement.
    scheduler.add_job(
        _wrap(job_reengagement_pass),
        "interval",
        hours=1,
        id="reengagement_pass",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # AI Agents (alembic 0063). Three additive jobs — none touch the
    # existing cadence engine. Synthesis drain (heavy Sonnet playbook /
    # showing-guide generation) every 2 min; targeting re-evaluation
    # hourly; the outreach pass every 30 min. All self-no-op when no
    # AI Agent is active.
    scheduler.add_job(
        _wrap(job_ai_agent_synthesis_drain),
        "interval",
        minutes=2,
        id="ai_agent_synthesis_drain",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _wrap(job_ai_agent_targeting_pass),
        "interval",
        hours=1,
        id="ai_agent_targeting_pass",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _wrap(job_ai_agent_pass),
        "interval",
        minutes=30,
        id="ai_agent_pass",
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
    queue as Elara Inbox drafts, not direct sends, unless the rule
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


async def job_bucket_ai_review_drain() -> None:
    """Run manually queued bucket AI reviews outside the request path."""
    from app.db import SessionLocal
    from app.services.bucket_ai import drain_bucket_ai_reviews

    async with SessionLocal() as db:
        try:
            count = await drain_bucket_ai_reviews(db, limit=3)
            if count:
                log.info("bucket_ai_review_drain: processed=%d", count)
        except Exception:
            await db.rollback()
            log.exception("bucket_ai_review_drain: failed; rolled back")


async def job_bucket_file_analysis_drain() -> None:
    """Analyze files queued on upload-complete so reviews compose from a warm
    per-file cache (no per-review attachment limit, minimal review-time tokens)."""
    from app.db import SessionLocal
    from app.services.bucket_ai import drain_file_analyses

    async with SessionLocal() as db:
        try:
            count = await drain_file_analyses(db, limit=5)
            if count:
                log.info("bucket_file_analysis_drain: processed=%d", count)
        except Exception:
            await db.rollback()
            log.exception("bucket_file_analysis_drain: failed; rolled back")


async def job_lender_inbound_poll() -> None:
    """Pull new lender-tagged emails from Gmail every 60s and append
    them to the LenderThread mailbox. Single-instance assumption (one
    container = one poller). Self-no-ops when USE_FAKE_INBOX=true or
    when GMAIL_* env vars aren't set."""
    from app.services.email.inbound_poller import run_inbound_poll

    await run_inbound_poll()


async def job_user_inbox_sync() -> None:
    """Phase 4 — sync the delegated Workspace mailbox into the isolated EmailMessage
    inbox (encrypted bodies, matched to loan/client, body-less breadcrumbs). Self-
    no-ops unless USER_INBOX_SYNC_ENABLED and Gmail DWD is configured; dedups on the
    gmail message id, so a double-fire is harmless."""
    from app.services.email.user_inbox_sync import run_user_inbox_sync

    await run_user_inbox_sync()


async def job_gmail_watch_renew() -> None:
    """Re-register the Gmail Pub/Sub watch (expires ~7 days). Runs
    every 24h + once ~10s after startup. Self-no-ops when
    GMAIL_PUBSUB_TOPIC isn't set or Gmail isn't configured — the 60s
    inbound poller remains the fallback either way."""
    from app.services.email.gmail_push import register_gmail_watch

    register_gmail_watch()


async def job_google_calendar_pull() -> None:
    """Fold each connected user's Google Calendar changes into internal
    calendar_events. Walks users whose calendar is connected and runs an
    incremental (syncToken) pull each. No-ops when nobody's connected.
    Idempotent on google_event_id, so a double-fire is harmless."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.google_account import GoogleAccount
    from app.services.google.calendar_sync import pull_events

    async with SessionLocal() as db:
        user_ids = (
            await db.execute(
                select(GoogleAccount.user_id).where(
                    GoogleAccount.calendar_connected.is_(True),
                    GoogleAccount.status == "active",
                )
            )
        ).scalars().all()
        for uid in user_ids:
            try:
                await pull_events(db, uid)
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
                log.exception("google_calendar_pull failed user=%s", uid)


async def job_reengagement_pass() -> None:
    """Hourly AI re-engagement pass — escalates outreach to email /
    SMS / WhatsApp for borrowers who've gone quiet on a time-sensitive
    file. Self-no-ops without an Anthropic key; the email rung is
    dormant until SES is configured. See
    app/services/ai/reengagement.run_reengagement_pass."""
    from app.services.ai.reengagement import run_reengagement_pass

    await run_reengagement_pass()


# ── AI Agents — the broker's configurable AI workers (alembic 0063) ──


async def job_ai_agent_synthesis_drain() -> None:
    """Heavy-AI synthesis drain. Every 2 min, picks AI-Agent playbooks
    + showing guides flagged `generation_status='generating'` and runs
    the Sonnet synthesis. Hard-capped per tick to bound spend. Mirrors
    job_document_scan_drain. Self-no-ops with nothing pending."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.ai_agent import AIAgent, AIAgentPlaybook, AIAgentShowingGuide
    from app.services.ai.ai_agent import generate_playbook, generate_showing_guide

    async with SessionLocal() as db:
        playbooks = (
            await db.execute(
                select(AIAgentPlaybook)
                .where(AIAgentPlaybook.generation_status == "generating")
                .limit(4)
            )
        ).scalars().all()
        guides = (
            await db.execute(
                select(AIAgentShowingGuide)
                .where(AIAgentShowingGuide.generation_status == "generating")
                .limit(4)
            )
        ).scalars().all()
        if not playbooks and not guides:
            return
        log.info(
            "ai_agent_synthesis_drain: %d playbooks, %d guides",
            len(playbooks), len(guides),
        )
        for pb in playbooks:
            try:
                agent = await db.get(AIAgent, pb.ai_agent_id)
                if agent is not None:
                    await generate_playbook(db, agent)
                    await db.commit()
            except Exception:  # noqa: BLE001
                log.exception("ai_agent_synthesis_drain: playbook %s failed", pb.id)
                await db.rollback()
        for g in guides:
            try:
                agent = await db.get(AIAgent, g.ai_agent_id)
                if agent is not None:
                    await generate_showing_guide(db, agent)
                    await db.commit()
            except Exception:  # noqa: BLE001
                log.exception("ai_agent_synthesis_drain: guide %s failed", g.id)
                await db.rollback()


async def job_ai_agent_targeting_pass() -> None:
    """Hourly. Re-evaluates each active AI Agent's targeting rules
    against the broker's current pipeline + clients, enrolling new
    matches into ai_agent_leads and retiring stale ones. Separate from
    the cadence engine — touches only ai_agent_leads."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.enums import AIAgentStatus
    from app.models.ai_agent import AIAgent
    from app.services.ai.ai_agent import run_targeting

    async with SessionLocal() as db:
        agents = (
            await db.execute(
                select(AIAgent).where(AIAgent.status == AIAgentStatus.ACTIVE)
            )
        ).scalars().all()
        if not agents:
            return
        for agent in agents:
            try:
                result = await run_targeting(db, agent)
                await db.commit()
                if result.get("enrolled") or result.get("retired"):
                    log.info(
                        "ai_agent_targeting_pass: agent=%s enrolled=%s retired=%s",
                        agent.id, result["enrolled"], result["retired"],
                    )
            except Exception:  # noqa: BLE001
                log.exception("ai_agent_targeting_pass: agent %s failed", agent.id)
                await db.rollback()


async def job_ai_agent_pass() -> None:
    """Every 30 min — the AI Agent outreach engine. For each active AI
    Agent, walks its due `ai_agent_leads`, composes a touchpoint
    message, and either drafts it (draft-first / warm-up) or sends it
    (auto). Honors per-agent cadence + exit rules. Separate job from
    the existing cadence engine — scoped only to ai_agent_leads."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.enums import AIAgentStatus
    from app.models.ai_agent import (
        AIAgent,
        AIAgentExitRules,
        AIAgentLead,
        AIAgentMessage,
    )
    from app.models.client import Client
    from app.services.ai.ai_agent import compose_message, touchpoint_for_attempt

    now = datetime.now(timezone.utc)
    PER_TICK = 25  # bound Anthropic spend per pass

    async with SessionLocal() as db:
        agents = (
            await db.execute(
                select(AIAgent).where(AIAgent.status == AIAgentStatus.ACTIVE)
            )
        ).scalars().all()
        if not agents:
            return

        processed = 0
        for agent in agents:
            if processed >= PER_TICK:
                break
            exit_rules = (
                await db.execute(
                    select(AIAgentExitRules).where(
                        AIAgentExitRules.ai_agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
            max_attempts = exit_rules.max_email_attempts if exit_rules else 5
            max_days = exit_rules.max_days_in_sequence if exit_rules else 14
            cadence = agent.cadence or [0]

            leads = (
                await db.execute(
                    select(AIAgentLead)
                    .where(AIAgentLead.ai_agent_id == agent.id)
                    .where(AIAgentLead.status == "active")
                    .where(
                        (AIAgentLead.next_action_at.is_(None))
                        | (AIAgentLead.next_action_at <= now)
                    )
                    .limit(PER_TICK - processed)
                )
            ).scalars().all()

            for lead in leads:
                processed += 1
                try:
                    # Exit-rule checks.
                    enrolled = lead.enrolled_at or lead.created_at or now
                    if (
                        lead.attempts_made >= max_attempts
                        or lead.attempts_made >= len(cadence)
                        or (now - enrolled) > timedelta(days=max_days)
                    ):
                        lead.status = "exited"
                        continue

                    client = await db.get(Client, lead.client_id)
                    if client is None:
                        lead.status = "exited"
                        continue

                    tp = touchpoint_for_attempt(agent, lead.attempts_made)
                    composed = await compose_message(
                        db, agent, client=client, touchpoint_key=tp
                    )
                    # Auto-send only when fully active (not warm-up).
                    auto = agent.send_mode == "auto" and not agent.warmup_mode
                    msg_status = "draft"
                    sent_at = None
                    provider_id = None
                    if auto:
                        sent_ok, provider_id = await _ai_agent_send_email(
                            client.email, composed["subject"], composed["body"]
                        )
                        msg_status = "sent" if sent_ok else "draft"
                        sent_at = now if sent_ok else None

                    db.add(
                        AIAgentMessage(
                            ai_agent_id=agent.id,
                            lead_id=lead.id,
                            client_id=client.id,
                            touchpoint_key=tp,
                            channel="email",
                            subject=composed["subject"][:300],
                            body=composed["body"],
                            status=msg_status,
                            sent_at=sent_at,
                            provider_message_id=provider_id,
                        )
                    )
                    lead.attempts_made += 1
                    lead.last_outbound_at = now
                    # Schedule the next touchpoint, or retire.
                    if lead.attempts_made >= len(cadence):
                        lead.next_action_at = None
                    else:
                        gap = max(
                            1,
                            cadence[lead.attempts_made]
                            - cadence[lead.attempts_made - 1],
                        )
                        lead.next_action_at = now + timedelta(days=gap)
                    await db.commit()
                except Exception:  # noqa: BLE001
                    log.exception("ai_agent_pass: lead %s failed", lead.id)
                    await db.rollback()
                await asyncio.sleep(0)


async def _ai_agent_send_email(
    to_email: str | None, subject: str, body: str
) -> tuple[bool, str | None]:
    """Best-effort SES send for an auto-send AI Agent. Returns
    (sent, provider_message_id). No-ops gracefully when SES isn't
    configured or there's no recipient."""
    if not (to_email or "").strip():
        return False, None
    try:
        from app.services.email.ses_client import send_email

        result = send_email(to_email=to_email, subject=subject, body_text=body)
        return bool(result.ok), result.message_id
    except Exception as exc:  # noqa: BLE001
        log.warning("ai_agent send_email failed: %s", exc)
        return False, None
