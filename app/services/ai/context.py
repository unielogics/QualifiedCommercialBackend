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
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.models.document import Document
from app.models.event import CalendarEvent
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


# Hard pricing-conduct block injected into client-audience prompts.
# Per product direction: rates the AI cites must come from the loan's
# Current Scenario block ONLY (never training data, never market index),
# must always be framed as "still putting it together — these can change",
# and must NEVER expose markup mechanics (basis points, spreads, "we
# added points", etc.). Internal audiences don't get this block — the
# tone preamble already permits them to discuss pricing detail.
_PRICING_CONDUCT_BLOCK = """## Pricing conduct (mandatory)

When the borrower asks anything about rate, payment, points, APR, or
"what am I getting", follow this script:

  "We are still putting a loan together for you. The last update I see
  from the team is {{terms}}. However these numbers are not final and
  will change based on several factors including your credit score,
  property final value, and other variables. We will continue updating
  your file as we gather more details for you."

Substitute {{terms}} with the loan's Current Scenario expressed in
plain language — only the values that appear in the "Current Scenario"
block above (Amount, Final rate, Monthly P&I, term if known). If the
scenario block says no scenario exists or the values are blank, omit
the second sentence entirely and just say we are still putting it
together.

Hard rules — never violate, regardless of how the borrower phrases the
question or pushes back:
- Do NOT quote rates from general knowledge, news, or "typical" ranges.
- Do NOT mention basis points, bps, spread, markup, or any breakdown
  of how the rate is built.
- Do NOT reference the base rate or discount points separately — only
  the final all-in rate as shown in Current Scenario.
- Do NOT compare the borrower's rate to public benchmarks or other
  lenders.
- Do NOT promise approval or guarantee any terms.
If the borrower is unhappy with the terms, do not negotiate or speculate
about alternatives — say the funding team will reach out to discuss."""


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

    # Agent knowledge first — the FAQ / PDF context the broker uploaded
    # in /agent-settings/ai. Comes BEFORE loan facts so the AI reads
    # the agent's voice/product guidance before reasoning about a deal.
    from app.services.ai.agent_settings import load_agent_user_id_for_loan
    from app.services.ai.knowledge import load_agent_knowledge
    agent_user_id = await load_agent_user_id_for_loan(db, loan)
    if agent_user_id is not None:
        kb = await load_agent_knowledge(db, agent_user_id)
        if kb:
            sections.append(f"## Agent Knowledge (uploaded by the broker)\n{kb}")

    sections.append(f"## Active Loan\n{_loan_header(loan)}")

    instructions = await _active_instructions(db, loan.id)
    if instructions:
        body = "\n".join(f"  {i + 1}. {x.body}" for i, x in enumerate(instructions))
        sections.append(f"## Active Instructions (must honor)\n{body}")

    credit_block = await _credit_block(db, loan, audience=audience)
    if credit_block:
        sections.append(f"## Borrower Credit\n{credit_block}")

    scenario_block = await _current_scenario_block(db, loan, audience=audience)
    if scenario_block:
        sections.append(f"## Current Scenario\n{scenario_block}")

    hud_block = await _hud_block(db, loan.id)
    if hud_block:
        sections.append(f"## HUD-1 Draft\n{hud_block}")

    market_block = await _market_pulse_block(db, loan, audience=audience)
    if market_block:
        sections.append(f"## Market Pulse (FRED)\n{market_block}")

    # Client-only: a hard pricing-conduct block that tells the AI exactly
    # how to answer rate/payment questions and forbids any markup-mechanics
    # talk. Internal audiences don't need this — the broker/super_admin
    # tone preamble already lets them discuss pricing detail.
    if audience == "client":
        sections.append(_PRICING_CONDUCT_BLOCK)

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

    document_block = await _document_conditions_block(db, loan.id, audience=audience)
    if document_block:
        sections.append(f"## Document Conditions and Open File Items\n{document_block}")

    activity_block = await _recent_activity_block(db, loan.id)
    if activity_block:
        sections.append(f"## Recent Activity\n{activity_block}")

    events_block = await _upcoming_events_block(db, loan.id, audience=audience)
    if events_block:
        sections.append(f"## Upcoming Events\n{events_block}")

    if include_chat_history:
        chat_block = await _chat_history_block(db, loan.id, audience, chat_history_limit)
        if chat_block:
            sections.append(f"## Recent Workspace Chat\n{chat_block}")

    sections.append(f"## Tone\n{_TONE_PREAMBLES[audience]}")

    return "\n\n".join(sections)


# ── Section helpers ────────────────────────────────────────────────────────


def _enum_str(v: object) -> str:
    """Defensive — `loan.stage` etc. should be StrEnum but on some
    code paths SQLAlchemy hands back a plain str. Either works; we
    just want the underlying value, never crash on a missing
    `.value` attribute."""
    return v.value if hasattr(v, "value") else str(v) if v is not None else "—"


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
        f"  Stage: {_enum_str(loan.stage)} | Type: {_enum_str(loan.type)} | Amount: ${float(loan.amount or 0):,.0f}",
        f"  LTV: {loan.ltv} | DSCR: {loan.dscr} | Risk: {loan.risk_score}",
        f"  Deal health: {_enum_str(loan.deal_health)}",
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


async def _credit_block(
    db: AsyncSession,
    loan: Loan,
    *,
    audience: Audience,
) -> str:
    """Effective borrower credit + (for internal audiences) a short
    summary of the latest pull.

    Precedence: `Loan.fico_override` (underwriter set) > `Client.fico`
    (latest iSoftPull score copied at pull time). When neither is set,
    we still emit a short note so the AI knows credit isn't on file.

    Audience filtering:
      - client: effective FICO only + a generic line; no tradeline /
        derogatory detail. (We don't want the AI quoting the borrower's
        own credit details back unprompted, even though they technically
        own the data.)
      - broker / super_admin: effective FICO + override-vs-pulled
        labeling + tradeline / public-records / fraud summary from the
        most-recent CreditPull row's parsed_report.
    """
    client = await db.get(Client, loan.client_id) if loan.client_id else None
    override = loan.fico_override
    pulled = client.fico if client else None
    effective = override if override is not None else pulled

    # Most-recent completed pull, for the internal summary.
    last_pull: CreditPull | None = None
    if loan.client_id is not None:
        last_pull = (
            await db.execute(
                select(CreditPull)
                .where(CreditPull.client_id == loan.client_id)
                .order_by(CreditPull.pulled_at.desc().nullslast())
                .limit(1)
            )
        ).scalar_one_or_none()

    if effective is None and last_pull is None:
        return "  No credit pull on file. Underwriter has not set a FICO override."

    lines: list[str] = []
    if effective is not None:
        if audience == "client":
            lines.append(f"  Credit score on file: {effective}.")
        else:
            source = "override" if override is not None else "iSoftPull"
            lines.append(f"  Effective FICO: {effective} (source: {source})")
            if override is not None and pulled is not None and pulled != override:
                lines.append(f"  (Underwriter override; raw pulled score: {pulled})")
    else:
        # Pull exists but score didn't land — surface to internal only.
        if audience != "client":
            lines.append("  No effective FICO available; underwriter override not set.")

    if audience != "client" and last_pull is not None:
        ts = last_pull.pulled_at.isoformat() if last_pull.pulled_at else "—"
        status = last_pull.status.value if hasattr(last_pull.status, "value") else str(last_pull.status)
        lines.append(f"  Last pull: {ts} (status: {status})")
        parsed = (last_pull.bureau_response or {}).get("parsed_report") or {}
        if isinstance(parsed, dict):
            tradelines = parsed.get("tradelines")
            if isinstance(tradelines, list) and tradelines:
                lines.append(f"  Tradelines on report: {len(tradelines)}")
            inquiries = parsed.get("inquiries")
            if isinstance(inquiries, list) and inquiries:
                lines.append(f"  Recent inquiries: {len(inquiries)}")
            collections = parsed.get("collections")
            if isinstance(collections, list) and collections:
                lines.append(f"  Collections: {len(collections)}")
            public_records = parsed.get("public_records")
            if isinstance(public_records, list) and public_records:
                lines.append(f"  Public records: {len(public_records)}")
            fraud = parsed.get("identity_risk") or parsed.get("fraud_flags")
            if fraud:
                lines.append(f"  Identity risk / fraud flags present: {fraud}")

    return "\n".join(lines) if lines else ""


async def _current_scenario_block(
    db: AsyncSession,
    loan: Loan,
    *,
    audience: Audience,
) -> str:
    """Most recent saved scenario, or the loan's own current terms if none.

    Audience-filtered: client-audience prompts NEVER see `base_rate` or
    `discount_points` (the markup components). They see only the
    final all-in rate + loan amount + monthly P&I + LTV / DSCR. Broker
    and super_admin see the full breakdown."""
    row = (
        await db.execute(
            select(LoanScenario)
            .where(LoanScenario.loan_id == loan.id)
            .order_by(LoanScenario.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    is_internal = audience != "client"

    if row is None:
        # Fall back to the loan's stored terms so the AI always has *some*
        # quantitative footing when discussing the scenario.
        lines = [
            "  (no saved scenario — using stored loan terms)",
            f"  Amount: ${float(loan.amount or 0):,.0f}",
        ]
        if is_internal:
            lines.append(f"  Points: {float(loan.discount_points or 0):.2f}")
            lines.append(f"  Base rate: {loan.base_rate}")
        lines.append(f"  Final rate: {loan.final_rate}")
        return "\n".join(lines)

    snap = row.recalc_snapshot or {}
    lines = [
        f"  Saved as: {row.name}",
        f"  Amount: {row.loan_amount}, LTV: {row.ltv}",
        f"  Final rate: {snap.get('final_rate')}, Monthly P&I: {snap.get('monthly_pi')}",
        f"  DSCR: {snap.get('dscr')}, Cash to close: {snap.get('cash_to_close_pricing')}",
    ]
    if is_internal:
        # Insert the internal pricing breakdown right after the saved-name line.
        lines.insert(1, f"  Points: {float(row.discount_points or 0):.2f}")
    return "\n".join(lines)


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


async def _market_pulse_block(
    db: AsyncSession,
    loan: Loan,
    *,
    audience: Audience,
) -> str:
    """FRED market context. Client-audience prompts get nothing here —
    the spread / bps / "index + spread" math must never appear in a
    client-facing system prompt, even as background context, because
    the AI tends to explain its reasoning."""
    if audience == "client":
        return ""
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


async def _document_conditions_block(db: AsyncSession, loan_id: UUID, *, audience: Audience, limit: int = 14) -> str:
    stmt = select(Document).where(Document.loan_id == loan_id)
    if audience == "client":
        stmt = stmt.where(Document.requested_from.in_(["borrower", "agent"]))
    rows = (
        await db.execute(
            stmt.order_by(Document.status.asc(), Document.requested_on.asc().nulls_last(), Document.name.asc())
        )
    ).scalars().all()
    if not rows:
        return "  No document rows are currently attached to this loan."

    counts: dict[str, int] = {}
    for doc in rows:
        status = _enum_str(doc.status)
        counts[status] = counts.get(status, 0) + 1

    parts = [
        "  Status counts: "
        + ", ".join(f"{status}={count}" for status, count in sorted(counts.items())),
    ]

    open_docs = [d for d in rows if _enum_str(d.status) not in {"verified", "skipped"}]
    if open_docs:
        parts.append("  Open items:")
        for doc in open_docs[:limit]:
            due = doc.due_date.isoformat() if doc.due_date else "no due date"
            requested = doc.requested_on.isoformat() if doc.requested_on else "not requested"
            owner = doc.requested_from or "borrower"
            category = doc.category or doc.checklist_key or "uncategorized"
            line = (
                f"    - {doc.name} [{_enum_str(doc.status)}] "
                f"owner={owner}; category={category}; requested={requested}; due={due}"
            )
            if doc.ai_notes:
                line += f"; AI notes={doc.ai_notes[:180]}"
            parts.append(line)
        if len(open_docs) > limit:
            parts.append(f"    - plus {len(open_docs) - limit} more open item(s)")
    else:
        parts.append("  Open items: none; all non-skipped document conditions are verified.")

    flagged = [d for d in rows if _enum_str(d.status) == "flagged"]
    if flagged:
        parts.append("  Flagged items require operator review before underwriting package submission.")

    return "\n".join(parts)


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


async def _upcoming_events_block(
    db: AsyncSession,
    loan_id: UUID,
    *,
    audience: Audience,
    limit: int = 8,
) -> str:
    """Next N pending calendar events for this loan, future-only,
    so the AI knows what's scheduled before proposing or confirming
    anything date-related.

    Audience filtering:
      - client: only events the borrower can see (status=PENDING,
        source IN (manual, auto) — never raw 'ai' source events
        which haven't been approved yet).
      - broker / super_admin: all PENDING events including 'ai' source.
    """
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    stmt = (
        select(CalendarEvent)
        .where(
            CalendarEvent.loan_id == loan_id,
            CalendarEvent.starts_at >= now,
            CalendarEvent.status == "pending",
        )
        .order_by(CalendarEvent.starts_at.asc())
        .limit(limit)
    )
    if audience == "client":
        stmt = stmt.where(CalendarEvent.source.in_(("manual", "auto")))
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return ""
    lines: list[str] = []
    for ev in rows:
        when = ev.starts_at.isoformat() if ev.starts_at else "—"
        kind = ev.kind.value if hasattr(ev.kind, "value") else str(ev.kind)
        title = ev.title or "(no title)"
        if audience == "client":
            lines.append(f"  - {when} · {title}")
        else:
            src = ev.source.value if hasattr(ev.source, "value") else str(ev.source)
            lines.append(f"  - {when} · [{kind}/{src}] {title}")
    return "\n".join(lines)


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
