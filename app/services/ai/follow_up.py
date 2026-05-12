"""AI re-engagement cadence — resolver + cadence-engine helpers.

Three knobs the user wanted, configurable per loan (operator pipeline)
AND per client (agent pipeline):

    stall_threshold_minutes  — minimum silence before AI nudges
    max_attempts_per_day     — daily ceiling
    max_days_without_reply   — global stop after N days of no response

Three-tier resolution, most-specific wins:

    per-file (override)   →  per-firm (meta)  →  hard-coded floor

The hard-coded floor exists so a brand-new deployment with no firm
defaults configured still has sane behavior (1-day stall, 3/day cap,
14-day stop).

`should_followup_now()` is the single check the cadence engine + the
outreach dispatcher run before drafting / sending. Returns
FollowUpDecision so the caller can log _why_ a fire was skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


# ── Floor defaults ───────────────────────────────────────────────────

DEFAULT_STALL_MIN = 60 * 24       # 24h
DEFAULT_MAX_PER_DAY = 3
DEFAULT_MAX_DAYS = 14


@dataclass(frozen=True)
class FollowUpSettings:
    stall_threshold_minutes: int
    max_attempts_per_day: int
    max_days_without_reply: int
    quiet_hours_start: int | None = None  # 0-23, borrower-local
    quiet_hours_end: int | None = None    # 0-23, borrower-local

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "FollowUpSettings | None":
        if not raw or not isinstance(raw, dict):
            return None
        try:
            return cls(
                stall_threshold_minutes=int(raw.get("stall_threshold_minutes") or 0) or DEFAULT_STALL_MIN,
                max_attempts_per_day=int(raw.get("max_attempts_per_day") or 0) or DEFAULT_MAX_PER_DAY,
                max_days_without_reply=int(raw.get("max_days_without_reply") or 0) or DEFAULT_MAX_DAYS,
                quiet_hours_start=_clamp_hour(raw.get("quiet_hours_start")),
                quiet_hours_end=_clamp_hour(raw.get("quiet_hours_end")),
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stall_threshold_minutes": self.stall_threshold_minutes,
            "max_attempts_per_day": self.max_attempts_per_day,
            "max_days_without_reply": self.max_days_without_reply,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
        }


def _clamp_hour(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n < 0 or n > 23:
        return None
    return n


# ── Resolver ─────────────────────────────────────────────────────────


async def resolve_follow_up(
    db: AsyncSession,
    *,
    loan_id: Any | None = None,
    client_id: Any | None = None,
) -> FollowUpSettings:
    """Resolve effective follow-up settings for a loan/client by walking
    file-override → firm-default → floor."""
    # 1) Per-file override (loan side — read off ClientAIPlan).
    if loan_id is not None:
        from app.models.client_ai_plan import ClientAIPlan
        plan = (
            await db.execute(
                select(ClientAIPlan).where(ClientAIPlan.loan_id == loan_id)
            )
        ).scalar_one_or_none()
        if plan is not None and plan.ai_secretary_settings:
            override = FollowUpSettings.from_dict(plan.ai_secretary_settings.get("follow_up"))
            if override is not None:
                return override

    # 2) Per-client override (agent side — read off Client.ai_cadence_override).
    if client_id is not None:
        from app.models.client import Client
        client = (
            await db.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if client is not None and client.ai_cadence_override:
            override = FollowUpSettings.from_dict(client.ai_cadence_override.get("follow_up"))
            if override is not None:
                return override

    # 3) Firm-default (the funding meta playbook with playbook_type="follow_up").
    from app.models.ai_playbook import AIPlaybookTemplate
    pb = (
        await db.execute(
            select(AIPlaybookTemplate).where(
                AIPlaybookTemplate.owner_type == "funding",
                AIPlaybookTemplate.playbook_type == "follow_up",
            ).limit(1)
        )
    ).scalar_one_or_none()
    if pb is not None and pb.rules:
        firm = FollowUpSettings.from_dict(pb.rules)
        if firm is not None:
            return firm

    # 4) Floor.
    return FollowUpSettings(
        stall_threshold_minutes=DEFAULT_STALL_MIN,
        max_attempts_per_day=DEFAULT_MAX_PER_DAY,
        max_days_without_reply=DEFAULT_MAX_DAYS,
    )


# ── Decision ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FollowUpDecision:
    can_send: bool
    reason: str   # "ok" | "stall_not_met" | "daily_cap" | "max_days_exceeded" | "quiet_hours"
    next_eligible_at: datetime | None = None

    def __bool__(self) -> bool:  # convenience
        return self.can_send


async def should_followup_now(
    db: AsyncSession,
    *,
    loan_id: Any | None,
    client_id: Any | None,
    last_outbound_at: datetime | None,
    last_borrower_reply_at: datetime | None,
    first_attempt_at: datetime | None,
    sent_today_count: int,
    now: datetime | None = None,
) -> FollowUpDecision:
    """Single gate the cadence engine + outreach dispatcher consult
    before drafting / sending a follow-up.

    Args are observables the caller already has (cheaper than us
    re-fetching them):
      • last_outbound_at        — most recent AI-→-borrower message
      • last_borrower_reply_at  — most recent borrower-→-AI message
      • first_attempt_at        — first AI follow-up after stall began
      • sent_today_count        — count of AI follow-ups in last 24h
    """
    now = now or datetime.now(timezone.utc)
    settings = await resolve_follow_up(db, loan_id=loan_id, client_id=client_id)

    # Max-days global stop: if the AI's been chasing for N days with no
    # borrower reply, give up. Operator can re-arm via patch.
    if first_attempt_at is not None and last_borrower_reply_at is None:
        days = (now - first_attempt_at).total_seconds() / 86_400
        if days >= settings.max_days_without_reply:
            return FollowUpDecision(False, "max_days_exceeded")

    # Daily cap: skip if we've already nudged max_attempts_per_day in
    # the last 24h.
    if sent_today_count >= settings.max_attempts_per_day:
        # Next eligible = 24h after the oldest message in the window
        # (approximated to 24h-from-last-send since we don't have the
        # oldest's timestamp at this layer).
        next_at = (last_outbound_at or now) + timedelta(hours=24)
        return FollowUpDecision(False, "daily_cap", next_at)

    # Stall threshold: don't poke until the borrower has been silent
    # for at least N minutes. Anchor on whichever is more recent —
    # borrower's reply or our own last outbound (a borrower reply
    # resets the clock).
    anchor = max(filter(None, [last_borrower_reply_at, last_outbound_at]), default=None)
    if anchor is not None:
        stall_due = anchor + timedelta(minutes=settings.stall_threshold_minutes)
        if now < stall_due:
            return FollowUpDecision(False, "stall_not_met", stall_due)

    # Quiet hours (borrower-local). For now we use server UTC; a future
    # iteration can wire timezone from Client.address. Skip the gate
    # entirely when either bound is unset.
    if settings.quiet_hours_start is not None and settings.quiet_hours_end is not None:
        h = now.hour
        s, e = settings.quiet_hours_start, settings.quiet_hours_end
        # Wraparound (e.g. 20→8) handled by allowing h >= s or h < e.
        in_quiet = (h >= s or h < e) if s > e else (s <= h < e)
        if in_quiet:
            return FollowUpDecision(False, "quiet_hours")

    return FollowUpDecision(True, "ok")


# ── Convenience: count attempts in the last 24h ──────────────────────


async def count_attempts_in_last_24h(
    db: AsyncSession,
    *,
    loan_id: Any,
) -> int:
    """How many AI outreach events fired in the last 24h for this loan.
    Used by the cadence engine to enforce max_attempts_per_day.

    Joins outreach_events → ai_task_assignments → client_requirement_status
    so we can scope by loan_id (events don't carry it directly)."""
    from app.models.ai_outreach_event import AIOutreachEvent
    from app.models.ai_task_assignment import AITaskAssignment
    from app.models.client_requirement_status import ClientRequirementStatus
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = (
        select(func.count(AIOutreachEvent.id))
        .join(AITaskAssignment, AITaskAssignment.id == AIOutreachEvent.assignment_id)
        .join(
            ClientRequirementStatus,
            ClientRequirementStatus.id == AITaskAssignment.client_requirement_status_id,
        )
        .where(ClientRequirementStatus.loan_id == loan_id)
        .where(AIOutreachEvent.direction == "outbound")
        .where(AIOutreachEvent.status.in_(("sent", "delivered", "drafted")))
        .where(AIOutreachEvent.created_at >= since)
    )
    return int((await db.execute(stmt)).scalar_one() or 0)
