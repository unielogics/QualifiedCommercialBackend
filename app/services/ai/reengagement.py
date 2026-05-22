"""AI re-engagement escalation.

When a borrower goes quiet in the mobile app and their file is
time-sensitive, the AI escalates outreach to alternative channels —
email, then SMS, then WhatsApp — to pull them back into the process.

A scheduled pass (`run_reengagement_pass`, hourly) evaluates each
active loan:
  * borrower silent past a ladder rung's window (no app activity / no
    chat reply), AND
  * the file has an open time-sensitive item (an outstanding required
    document, or a close date approaching),
then composes a channel-appropriate message and either auto-sends it
(email, via SES) or records a draft (SMS / WhatsApp — dormant until a
Twilio adapter + the per-channel auto-send toggle land).

Every attempt is logged as `Activity(kind="ai.reengagement")` — that
log is also the rung-dedup + once-per-day frequency-cap source.

The policy (inactivity windows, the channel ladder, urgency criteria)
is config-driven — funding 'communication' playbook
`rules.reengagement` — so it's tunable without a code change. Empty
config => the safe defaults below.

Dormant-safe: with SES unconfigured the pass still runs; the email
rung just logs "dormant". Nothing here can block the platform.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DealChatRole
from app.models.activity import Activity
from app.models.ai_playbook import AIPlaybookTemplate
from app.models.client import Client
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.loan import Loan
from app.models.loan_chat_message import LoanChatMessage
from app.models.user import User
from app.services.email.ses_client import send_email, ses_configured

log = logging.getLogger(__name__)

REENGAGEMENT_KIND = "ai.reengagement"
APP_BASE_URL = "https://app.qualifiedcommercial.com"

# Open requirement statuses that count as "the file still needs the
# borrower to do something".
_OPEN_REQUIREMENT_STATUSES = {"missing", "asked", "needed_later"}

_DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "frequency_cap_hours": 24,  # at most one re-engagement attempt / file / day
    "lookback_days": 14,        # window for "which rungs already fired"
    "urgency": {
        "close_date_within_days": 21,
        "require_open_item": True,
    },
    # The escalation ladder — alternative channels only (portal nudges
    # are the normal cadence engine's job). `after_hours` = how long the
    # borrower must be silent before that rung is eligible.
    "ladder": [
        {"channel": "email", "after_hours": 48},
        {"channel": "sms", "after_hours": 96},
        {"channel": "whatsapp", "after_hours": 168},
    ],
}


async def _load_policy(db: AsyncSession) -> dict[str, Any]:
    """Re-engagement policy from the funding 'communication' playbook
    rules.reengagement, merged over the defaults. Never raises."""
    try:
        rows = (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.owner_type == "funding",
                    AIPlaybookTemplate.playbook_type == "cadence",
                    AIPlaybookTemplate.product_key == "communication",
                    AIPlaybookTemplate.is_active.is_(True),
                )
            )
        ).scalars().all()
    except Exception:
        log.exception("reengagement: policy load failed; using defaults")
        return dict(_DEFAULT_POLICY)
    policy = dict(_DEFAULT_POLICY)
    for r in rows:
        cfg = (r.rules or {}).get("reengagement") if isinstance(r.rules, dict) else None
        if isinstance(cfg, dict) and cfg:
            merged = dict(_DEFAULT_POLICY)
            merged.update({k: v for k, v in cfg.items() if v is not None})
            return merged
    return policy


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def run_reengagement_pass() -> None:
    """One scheduled tick. Walks active loans, escalates where the
    borrower has gone quiet on a time-sensitive file. Self-isolating —
    one bad loan never blocks the rest of the batch."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        log.debug("reengagement: no anthropic key; skipping")
        return

    async with SessionLocal() as db:
        policy = await _load_policy(db)
        if not policy.get("enabled", True):
            log.debug("reengagement: disabled by policy")
            return
        ladder = [r for r in policy.get("ladder", []) if isinstance(r, dict)]
        if not ladder:
            return

        now = datetime.now(timezone.utc)
        loans = (
            await db.execute(
                select(Loan).where(Loan.stage != "funded")
            )
        ).scalars().all()

        fired = 0
        for loan in loans:
            try:
                if await _evaluate_loan(db, loan, policy, ladder, now):
                    fired += 1
            except Exception:
                log.exception("reengagement: loan=%s evaluation failed", loan.id)
        if fired:
            await db.commit()
            log.info("reengagement: fired %d escalation(s)", fired)


async def _evaluate_loan(
    db: AsyncSession,
    loan: Loan,
    policy: dict[str, Any],
    ladder: list[dict[str, Any]],
    now: datetime,
) -> bool:
    """Evaluate one loan; dispatch the appropriate rung if due. Returns
    True when an escalation fired."""
    client = await db.get(Client, loan.client_id) if loan.client_id else None
    if client is None:
        return False

    # --- borrower silence ---
    silent_since: datetime | None = None
    if client.user_id:
        user = await db.get(User, client.user_id)
        silent_since = _aware(getattr(user, "last_seen_at", None)) if user else None
    last_client_msg = (
        await db.execute(
            select(LoanChatMessage.created_at)
            .where(
                LoanChatMessage.loan_id == loan.id,
                LoanChatMessage.from_role == DealChatRole.CLIENT.value,
            )
            .order_by(LoanChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_client_msg = _aware(last_client_msg)
    candidates = [d for d in (silent_since, last_client_msg) if d is not None]
    if not candidates:
        # No activity signal at all — too new / unknown. Don't nag.
        return False
    silent_since = max(candidates)
    silent_hours = (now - silent_since).total_seconds() / 3600.0

    # --- time-sensitivity ---
    urgency = policy.get("urgency", {}) or {}
    open_item = await _first_open_requirement(db, loan.id)
    close_soon = _close_date_soon(loan, urgency.get("close_date_within_days", 21), now)
    if urgency.get("require_open_item", True) and not open_item and not close_soon:
        return False
    if not open_item and not close_soon:
        return False

    # --- frequency cap + already-fired rungs ---
    cap_hours = float(policy.get("frequency_cap_hours", 24))
    lookback = int(policy.get("lookback_days", 14))
    recent = (
        await db.execute(
            select(Activity)
            .where(
                Activity.loan_id == loan.id,
                Activity.kind == REENGAGEMENT_KIND,
                Activity.occurred_at >= now - timedelta(days=lookback),
            )
            .order_by(Activity.occurred_at.desc())
        )
    ).scalars().all()
    if recent:
        last_at = _aware(recent[0].occurred_at)
        if last_at and (now - last_at).total_seconds() / 3600.0 < cap_hours:
            return False  # within the frequency cap
    fired_channels = {
        (a.payload or {}).get("channel")
        for a in recent
        if isinstance(a.payload, dict)
    }

    # --- pick the rung: furthest-along rung that's time-eligible and
    # whose channel hasn't been used yet ---
    rung: dict[str, Any] | None = None
    for r in ladder:
        if silent_hours < float(r.get("after_hours", 0)):
            continue
        if r.get("channel") in fired_channels:
            continue
        rung = r  # keep walking — take the last (furthest) eligible
    if rung is None:
        return False

    channel = str(rung.get("channel"))
    reason = open_item or ("an upcoming closing date" if close_soon else "your file")
    await _dispatch_rung(db, loan, client, channel, reason, silent_hours, now)
    return True


async def _first_open_requirement(db: AsyncSession, loan_id) -> str | None:
    """Human label of the first outstanding required item on the loan,
    or None when nothing is outstanding."""
    row = (
        await db.execute(
            select(ClientRequirementStatus)
            .where(
                ClientRequirementStatus.loan_id == loan_id,
                ClientRequirementStatus.status.in_(_OPEN_REQUIREMENT_STATUSES),
            )
            .order_by(ClientRequirementStatus.last_requested_at.asc().nullsfirst())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return (row.requirement_key or "a required document").replace("_", " ")


def _close_date_soon(loan: Loan, within_days: int, now: datetime) -> bool:
    cd = getattr(loan, "close_date", None)
    if cd is None:
        return False
    try:
        cd_dt = datetime(cd.year, cd.month, cd.day, tzinfo=timezone.utc)
    except Exception:
        return False
    delta_days = (cd_dt - now).total_seconds() / 86400.0
    return 0 <= delta_days <= float(within_days)


async def _dispatch_rung(
    db: AsyncSession,
    loan: Loan,
    client: Client,
    channel: str,
    reason: str,
    silent_hours: float,
    now: datetime,
) -> None:
    """Compose + send/draft one re-engagement rung, then log Activity."""
    from app.services.ai.reengagement_composer import compose_reengagement_message

    composed = await compose_reengagement_message(
        db, loan=loan, client=client, channel=channel, reason=reason
    )
    status = "skipped"
    detail = ""

    if channel == "email":
        if not client.email:
            status, detail = "skipped", "no borrower email"
        elif not ses_configured():
            status, detail = "dormant", "SES not configured"
            log.info("reengagement: email rung dormant (SES unconfigured) loan=%s", loan.id)
        else:
            res = send_email(
                to_email=client.email,
                subject=composed["subject"],
                body_text=composed["body"],
            )
            status = "sent" if res.ok else "failed"
            detail = res.detail
    else:
        # SMS / WhatsApp — draft-first. The Twilio adapter + the
        # per-channel auto-send toggle are a follow-up; until then the
        # composed draft is recorded for audit + future review.
        status, detail = "drafted", "channel adapter not yet provisioned"

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="ai",
            kind=REENGAGEMENT_KIND,
            summary=f"AI re-engagement via {channel} ({status}) — {reason}",
            payload={
                "channel": channel,
                "status": status,
                "detail": detail,
                "silent_hours": round(silent_hours, 1),
                "reason": reason,
                "subject": composed.get("subject"),
                "body": composed.get("body"),
            },
        )
    )
    await db.flush()
    log.info(
        "reengagement: loan=%s channel=%s status=%s silent=%.1fh",
        loan.id, channel, status, silent_hours,
    )
