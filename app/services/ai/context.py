"""Prompt-assembly hook — the single piece that makes operator feedback,
loan instructions, AI Modify corrections, and the live simulator scenario
actually steer the AI.

Used by:
  - routers/ai.py POST /ai/chat (Deal Workspace + AI Rail)
  - routers/loan_workspace.py auto-reply path
  - services/ai/summarizer.py (Living Loan File)
  - services/ai/orchestrator.py run() (when loan_id is in scope)

Sections are emitted only when non-empty so we don't send "no instructions"
walls of whitespace into the system prompt.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.ai_feedback import AIFeedback
from app.models.ai_modify_correction import AIModifyCorrection
from app.models.ai_task import AITask
from app.models.fred_observation import FredObservation
from app.models.hud import HudLineItem
from app.models.lender_spread import LenderSpread
from app.models.loan import Loan
from app.models.loan_chat_message import LoanChatMessage
from app.models.loan_instruction import LoanInstruction
from app.models.loan_scenario import LoanScenario
from app.services.fred import PRODUCT_SERIES_MAP, SERIES_LABELS

Audience = Literal["client", "broker", "super_admin"]

_TONE_PREAMBLES: dict[Audience, str] = {
    "client": (
        "Audience: borrower. Use plain, friendly language. Don't expose internal "
        "decisions, risk scoring, or lender names. Confirm next steps and what "
        "the borrower needs to do."
    ),
    "broker": (
        "Audience: broker / account exec. Use direct, deal-focused language. "
        "Surface blockers, missing docs, and what the broker should chase. "
        "Internal pricing detail is fine; lender identities still masked."
    ),
    "super_admin": (
        "Audience: operator. Be candid. Surface uncertainty, conflicting "
        "signals, and your own reasoning. You may reference internal risk "
        "scoring and lender identities."
    ),
}


async def assemble_loan_context(
    db: AsyncSession,
    loan: Loan | None,
    *,
    audience: Audience,
    include_chat_history: bool = False,
    chat_history_limit: int = 10,
) -> str:
    """Render the loan-context block to append to the system prompt.

    Returns an empty string if `loan` is None so callers can do
    `system += await assemble_loan_context(...)` unconditionally.
    """
    if loan is None:
        return ""

    sections: list[str] = []
    sections.append(f"## Active Loan\n{_loan_header(loan)}")

    instructions = await _active_instructions(db, loan.id)
    if instructions:
        body = "\n".join(f"  {i + 1}. {x.body}" for i, x in enumerate(instructions))
        sections.append(f"## Active Instructions (must honor)\n{body}")

    scenario_block = await _current_scenario_block(db, loan)
    if scenario_block:
        sections.append(f"## Current Scenario\n{scenario_block}")

    hud_block = await _hud_block(db, loan.id)
    if hud_block:
        sections.append(f"## HUD-1 Draft\n{hud_block}")

    market_block = await _market_pulse_block(db, loan)
    if market_block:
        sections.append(f"## Market Pulse (FRED)\n{market_block}")

    feedback_block = await _negative_feedback_block(db, loan.id)
    if feedback_block:
        sections.append(
            "## Recent Feedback (the team flagged these earlier outputs — avoid the patterns below)\n"
            + feedback_block
        )

    if audience == "super_admin":
        corrections_block = await _ai_modify_block(db, loan.id)
        if corrections_block:
            sections.append(
                "## AI Modify Corrections (operator notes on past turns — apply going forward)\n"
                + corrections_block
            )

    activity_block = await _recent_activity_block(db, loan.id)
    if activity_block:
        sections.append(f"## Recent Activity\n{activity_block}")

    if include_chat_history:
        chat_block = await _chat_history_block(db, loan.id, audience, chat_history_limit)
        if chat_block:
            sections.append(f"## Recent Workspace Chat\n{chat_block}")

    sections.append(f"## Tone\n{_TONE_PREAMBLES[audience]}")

    return "\n\n".join(sections)


# ── Section helpers ────────────────────────────────────────────────────────


def _loan_header(loan: Loan) -> str:
    # Scope marker + DB key first so the AI knows exactly what
    # conversation it's in. The bare deal_id (L-XXXX) is what humans
    # speak; the UUID is the unique key the rest of the platform uses.
    lines = [
        f"SCOPE: loan-level conversation",
        f"Loan ID (UUID): {loan.id}",
        f"Deal ID: {loan.deal_id}",
        f"Property: {loan.address}",
        f"Client ID (UUID): {loan.client_id}",
        f"  Stage: {loan.stage.value} | Type: {loan.type.value} | Amount: ${float(loan.amount):,.0f}",
        f"  LTV: {loan.ltv} | DSCR: {loan.dscr} | Risk: {loan.risk_score}",
        f"  Deal health: {loan.deal_health.value}",
    ]
    if loan.status_summary:
        lines.append(f"  Living Loan File: {loan.status_summary}")
    return "\n".join(lines)


async def _active_instructions(db: AsyncSession, loan_id: UUID) -> list[LoanInstruction]:
    rows = (
        await db.execute(
            select(LoanInstruction)
            .where(LoanInstruction.loan_id == loan_id, LoanInstruction.is_active.is_(True))
            .order_by(LoanInstruction.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def _current_scenario_block(db: AsyncSession, loan: Loan) -> str:
    """Most recent saved scenario, or the loan's own current terms if none."""
    row = (
        await db.execute(
            select(LoanScenario)
            .where(LoanScenario.loan_id == loan.id)
            .order_by(LoanScenario.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        # Fall back to the loan's stored terms so the AI always has *some*
        # quantitative footing when discussing the scenario.
        return (
            f"  (no saved scenario — using stored loan terms)\n"
            f"  Points: {float(loan.discount_points):.2f}, "
            f"Amount: ${float(loan.amount):,.0f}, "
            f"Base rate: {loan.base_rate}, "
            f"Final rate: {loan.final_rate}"
        )
    snap = row.recalc_snapshot or {}
    return (
        f"  Saved as: {row.name}\n"
        f"  Points: {float(row.discount_points):.2f}, "
        f"Amount: {row.loan_amount}, LTV: {row.ltv}\n"
        f"  Final rate: {snap.get('final_rate')}, "
        f"Monthly P&I: {snap.get('monthly_pi')}, "
        f"DSCR: {snap.get('dscr')}, "
        f"Cash to close: {snap.get('cash_to_close_pricing')}"
    )


async def get_market_pulse(db: AsyncSession, loan: Loan) -> dict | None:
    """Resolve the relevant FRED series for this loan's product type and
    return a structured snapshot of the index + spread + 7d/30d trend.

    Returns None if (a) the loan's type isn't mapped to a FRED series, or
    (b) we have no observations yet (first cron run hasn't fired).

    Used by both the summarizer (to compose the Market Context section)
    and `_market_pulse_block` (to splice into the system prompt).
    """
    loan_type_value = loan.type.value if hasattr(loan.type, "value") else str(loan.type)
    series_id = PRODUCT_SERIES_MAP.get(loan_type_value)
    if series_id is None:
        return None

    rows = (
        await db.execute(
            select(FredObservation)
            .where(FredObservation.series_id == series_id)
            .order_by(FredObservation.observation_date.desc())
            .limit(35)
        )
    ).scalars().all()
    valid = [r for r in rows if r.value is not None]
    if not valid:
        return None
    latest = valid[0]

    def _earliest_after(days: int) -> FredObservation | None:
        # Find the most-recent observation that is *at least* `days` ago.
        from datetime import timedelta as _td
        cutoff = latest.observation_date - _td(days=days)
        for r in valid:
            if r.observation_date <= cutoff:
                return r
        return None

    seven_ago = _earliest_after(7)
    thirty_ago = _earliest_after(30)

    spread = (
        await db.execute(
            select(LenderSpread)
            .where(LenderSpread.series_id == series_id)
            .order_by(LenderSpread.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    spread_bps = spread.spread_bps if spread else 0
    latest_value = float(latest.value)
    estimated_rate = latest_value + spread_bps / 100.0

    def _delta_bps(prior: FredObservation | None) -> int | None:
        if prior is None or prior.value is None:
            return None
        return round((latest_value - float(prior.value)) * 100)

    trend_7d = _delta_bps(seven_ago)
    trend_30d = _delta_bps(thirty_ago)

    # Risk flag — "Rate Pressure" when the index has climbed materially in
    # the recent window. Threshold is intentionally tight (10 bps in 7d)
    # so the warning surfaces during the kind of move brokers actually act on.
    if trend_7d is not None and trend_7d >= 10:
        warning = "Rate Pressure"
    elif trend_7d is not None and trend_7d <= -10:
        warning = "Rate Easing"
    elif trend_7d is not None and abs(trend_7d) < 5:
        warning = "Rate Stability"
    else:
        warning = None

    return {
        "series_id": series_id,
        "series_label": SERIES_LABELS.get(series_id, series_id),
        "loan_product": loan_type_value,
        "index_value": latest_value,
        "index_date": latest.observation_date.isoformat(),
        "spread_bps": spread_bps,
        "estimated_rate": round(estimated_rate, 3),
        "trend_7d_bps": trend_7d,
        "trend_30d_bps": trend_30d,
        "warning": warning,
    }


async def _market_pulse_block(db: AsyncSession, loan: Loan) -> str:
    pulse = await get_market_pulse(db, loan)
    if pulse is None:
        return ""
    parts = [
        f"  Series: {pulse['series_id']} ({pulse['series_label']}) — drives pricing for product '{pulse['loan_product']}'",
        f"  Index: {pulse['index_value']:.3f}% as of {pulse['index_date']}",
        f"  Spread: {pulse['spread_bps']} bps ({pulse['spread_bps'] / 100:.2f}%)",
        f"  Estimated rate: {pulse['estimated_rate']:.3f}% (= index + spread)",
    ]
    if pulse["trend_7d_bps"] is not None:
        sign = "+" if pulse["trend_7d_bps"] > 0 else ""
        parts.append(f"  7-day trend: {sign}{pulse['trend_7d_bps']} bps")
    if pulse["trend_30d_bps"] is not None:
        sign = "+" if pulse["trend_30d_bps"] > 0 else ""
        parts.append(f"  30-day trend: {sign}{pulse['trend_30d_bps']} bps")
    if pulse["warning"]:
        parts.append(f"  ⚠ Risk flag: {pulse['warning']}")
    return "\n".join(parts)


async def _hud_block(db: AsyncSession, loan_id: UUID) -> str:
    rows = (
        await db.execute(select(HudLineItem).where(HudLineItem.loan_id == loan_id))
    ).scalars().all()
    if not rows:
        return ""
    total = sum(float(r.amount or 0) for r in rows)
    return f"  Total: ${total:,.2f} ({len(rows)} line items)"


async def _negative_feedback_block(db: AsyncSession, loan_id: UUID, limit: int = 10) -> str:
    """Recent thumbs-down on AI tasks for this loan, with the operator comment.

    Joined to ai_tasks so we can include the task title for context. Other
    output_types (chat_reply, email_draft, summary) are accepted by the
    feedback table but not rendered here yet — add joins as those surfaces
    light up.
    """
    stmt = (
        select(AIFeedback, AITask.title)
        .join(AITask, AIFeedback.output_id == AITask.id, isouter=True)
        .where(
            AIFeedback.loan_id == loan_id,
            AIFeedback.rating == "down",
        )
        .order_by(AIFeedback.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return ""
    out: list[str] = []
    for fb, title in rows:
        title_part = f"[{title}] " if title else ""
        comment_part = f"— {fb.comment}" if fb.comment else "— (no comment)"
        out.append(f"  - {title_part}{comment_part}")
    return "\n".join(out)


async def _ai_modify_block(db: AsyncSession, loan_id: UUID, limit: int = 5) -> str:
    rows = (
        await db.execute(
            select(AIModifyCorrection)
            .where(AIModifyCorrection.loan_id == loan_id)
            .order_by(AIModifyCorrection.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return ""
    return "\n".join(f"  - {r.correction}" for r in rows)


async def _recent_activity_block(db: AsyncSession, loan_id: UUID, limit: int = 5) -> str:
    rows = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan_id)
            .order_by(Activity.occurred_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return ""
    return "\n".join(f"  - [{a.kind}] {a.summary}" for a in rows)


async def _chat_history_block(
    db: AsyncSession, loan_id: UUID, audience: Audience, limit: int
) -> str:
    """Last N workspace chat turns. Filters out non-client-visible turns when
    the audience is 'client' so the prompt stays consistent with what they
    can see."""
    stmt = (
        select(LoanChatMessage)
        .where(LoanChatMessage.loan_id == loan_id)
        .order_by(LoanChatMessage.created_at.desc())
        .limit(limit)
    )
    if audience == "client":
        stmt = stmt.where(LoanChatMessage.client_visible.is_(True))
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()  # oldest first when rendered
    if not rows:
        return ""
    return "\n".join(f"  {m.from_role}: {m.body}" for m in rows)
