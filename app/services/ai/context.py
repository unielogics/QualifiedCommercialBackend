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

    sections.append(f"## Active Loan\n{_loan_header(loan, audience=audience)}")

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

    activity_block = await _recent_activity_block(db, loan.id, audience=audience)
    if activity_block:
        sections.append(f"## Recent Activity\n{activity_block}")

    # Round-3 (2026-05-14) — lender-thread structured extract. Operator
    # audiences see the full extract (internal + external); client and
    # realtor audiences see only the externals-only filtered view that
    # the lender_extractor already prepared on
    # `loans.living_profile.lender_extract_external`. This is what
    # lets the general AI answer "what's the lender waiting on?"
    # without leaking internal commercial mechanics.
    lender_block = _lender_extract_block(loan, audience=audience)
    if lender_block:
        sections.append(f"## Lender Thread Context\n{lender_block}")

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


def _loan_header(loan: Loan, *, audience: Audience = "broker") -> str:
    """Render the loan-level facts that drive every AI reply on this
    file. The single source of truth is the Loan row — same data the
    Criteria tab UI reads from, so what the underwriter sees and what
    the AI sees stay in sync.

    Audience filtering:
      - client: stage / type / amount / property / final rate / term
        / close date / DSCR. NEVER: base_rate, discount_points,
        lender_fees, risk_score, deal_health (those reveal markup
        mechanics or internal risk scoring).
      - broker / super_admin: everything.
    """
    is_internal = audience != "client"

    lines = [
        "SCOPE: loan-level conversation",
        f"Loan ID (UUID): {loan.id}",
        f"Deal ID: {loan.deal_id}",
        f"Client ID (UUID): {loan.client_id}",
    ]

    # Property block — what the UI surfaces on the Property tab.
    prop_bits = [loan.address or "—"]
    if loan.city or loan.state:
        prop_bits.append(f"{loan.city or '?'}, {loan.state or '?'}")
    lines.append(f"Property: {', '.join(prop_bits)}")
    prop_detail = []
    if loan.property_type:
        prop_detail.append(f"type {_enum_str(loan.property_type)}")
    if loan.unit_count:
        prop_detail.append(f"{loan.unit_count} units")
    if loan.beds is not None:
        prop_detail.append(f"{loan.beds} bd")
    if loan.baths is not None:
        prop_detail.append(f"{loan.baths} ba")
    if loan.sqft:
        prop_detail.append(f"{loan.sqft:,} sqft")
    if loan.year_built:
        prop_detail.append(f"built {loan.year_built}")
    if prop_detail:
        lines.append(f"  {' · '.join(prop_detail)}")
    if is_internal and (loan.zoning or loan.parcel_id or loan.listing_status):
        zoning_bits = [
            f"zoning {loan.zoning}" if loan.zoning else None,
            f"parcel {loan.parcel_id}" if loan.parcel_id else None,
            f"listing {loan.listing_status}" if loan.listing_status else None,
        ]
        lines.append("  " + " · ".join(b for b in zoning_bits if b))

    # Loan structure — same fields the Criteria tab "Structure" section shows.
    lines.append(
        f"Loan: {_enum_str(loan.type)} · {_enum_str(loan.purpose) if loan.purpose else 'purchase'} · "
        f"side={_enum_str(loan.side)} · stage={_enum_str(loan.stage)}"
    )
    struct_bits = []
    if loan.term_months:
        struct_bits.append(f"term {loan.term_months}mo")
    if loan.amortization_style:
        struct_bits.append(f"amort {_enum_str(loan.amortization_style)}")
    if is_internal and loan.prepay_penalty:
        struct_bits.append(f"prepay {_enum_str(loan.prepay_penalty)}")
    if struct_bits:
        lines.append(f"  {' · '.join(struct_bits)}")
    if loan.close_date:
        lines.append(f"  Close date: {loan.close_date.isoformat()}")

    # Pricing — Criteria tab "Pricing" section. Client sees ONLY the
    # final rate + amount; internal sees the full breakdown.
    lines.append(f"Amount: ${float(loan.amount or 0):,.0f}")
    if loan.final_rate is not None:
        lines.append(f"  Final rate: {loan.final_rate}")
    if is_internal:
        if loan.base_rate is not None:
            lines.append(f"  Base rate: {loan.base_rate}")
        if loan.discount_points:
            lines.append(f"  Discount points: {float(loan.discount_points):.3f}")
        if loan.origination_pct:
            lines.append(f"  Origination: {float(loan.origination_pct) * 100:.2f}%")
        if loan.lender_fees is not None:
            lines.append(f"  Lender fees: ${float(loan.lender_fees):,.0f}")

    # Collateral — Criteria tab "Collateral" section.
    if loan.arv is not None or loan.ltv is not None or loan.ltc is not None:
        coll_bits = []
        if loan.arv is not None:
            coll_bits.append(f"ARV ${float(loan.arv):,.0f}")
        if loan.ltv is not None:
            coll_bits.append(f"LTV {float(loan.ltv) * 100:.2f}%")
        if is_internal and loan.ltc is not None:
            coll_bits.append(f"LTC {float(loan.ltc) * 100:.2f}%")
        lines.append(f"Collateral: {' · '.join(coll_bits)}")

    # Income / DSCR — Criteria tab "Income" section, DSCR-only fields.
    if loan.monthly_rent is not None or loan.dscr is not None:
        inc_bits = []
        if loan.monthly_rent is not None:
            inc_bits.append(f"rent ${float(loan.monthly_rent):,.0f}/mo")
        if loan.dscr is not None:
            inc_bits.append(f"DSCR {float(loan.dscr):.2f}")
        if is_internal and loan.vacancy_pct is not None:
            inc_bits.append(f"vacancy {float(loan.vacancy_pct) * 100:.1f}%")
        if is_internal and loan.expense_ratio_pct is not None:
            inc_bits.append(f"expense ratio {float(loan.expense_ratio_pct) * 100:.1f}%")
        lines.append(f"Income: {' · '.join(inc_bits)}")

    # Carrying costs — Criteria tab "Carrying costs" section.
    carry_bits = []
    if loan.annual_taxes:
        carry_bits.append(f"taxes ${float(loan.annual_taxes):,.0f}/yr")
    if loan.annual_insurance:
        carry_bits.append(f"insurance ${float(loan.annual_insurance):,.0f}/yr")
    if loan.monthly_hoa:
        carry_bits.append(f"HOA ${float(loan.monthly_hoa):,.0f}/mo")
    if is_internal and loan.reserves_required is not None:
        carry_bits.append(f"reserves ${float(loan.reserves_required):,.0f}")
    if carry_bits:
        lines.append(f"Carrying: {' · '.join(carry_bits)}")

    # Borrower — Criteria tab "Borrower" section. FICO lives in its
    # own block (_credit_block); here we surface entity + experience.
    if is_internal:
        borrower_bits = []
        if loan.entity_type:
            borrower_bits.append(f"entity {_enum_str(loan.entity_type)}")
        if loan.experience_tier:
            borrower_bits.append(f"experience {_enum_str(loan.experience_tier)}")
        if borrower_bits:
            lines.append(f"Borrower: {' · '.join(borrower_bits)}")

    # Type-specific — Criteria tab "{type}-specific" section. Only the
    # fields that apply to this loan's type appear here.
    type_bits = []
    if is_internal and loan.construction_holdback_pct is not None:
        type_bits.append(f"construction holdback {float(loan.construction_holdback_pct) * 100:.1f}%")
    if is_internal and loan.draw_count is not None:
        type_bits.append(f"{loan.draw_count} draws")
    if loan.exit_strategy:
        type_bits.append(f"exit {_enum_str(loan.exit_strategy)}")
    if is_internal and loan.cash_to_borrower is not None:
        type_bits.append(f"cash to borrower ${float(loan.cash_to_borrower):,.0f}")
    if is_internal and loan.seasoning_months is not None:
        type_bits.append(f"seasoning {loan.seasoning_months}mo")
    if is_internal and loan.property_count is not None:
        type_bits.append(f"{loan.property_count} properties")
    if type_bits:
        lines.append(f"Type-specific: {' · '.join(type_bits)}")

    # Risk + deal health — internal only.
    if is_internal:
        risk_bits = []
        if loan.risk_score is not None:
            risk_bits.append(f"risk {loan.risk_score}")
        risk_bits.append(f"deal health {_enum_str(loan.deal_health)}")
        lines.append(f"  {' · '.join(risk_bits)}")

    if loan.status_summary:
        lines.append(f"Living Loan File: {loan.status_summary}")

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


async def _recent_activity_block(
    db: AsyncSession,
    loan_id: UUID,
    *,
    audience: Audience = "broker",
) -> str:
    """Recent file activity, rendered as a chronological digest the AI
    can reference when asked "what changed?" / "what happened on this
    file?".

    Audience-aware:
      - Internal audiences (broker / super_admin): 20 most recent rows,
        all kinds. Diff payloads render inline ("base_rate 7.5 → 7.8")
        so the AI can speak to specific edits.
      - Client audience: 8 most recent rows, filtered to client-visible
        kinds only. Pricing-only diffs are stripped from any
        criteria_changed payloads that do reach the client.
    """
    from app.services.activity_log import (
        filter_payload_for_audience,
        format_field_change,
        is_visible_to,
    )

    over_fetch = 60 if audience != "client" else 24
    rows = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan_id)
            .order_by(Activity.occurred_at.desc())
            .limit(over_fetch)
        )
    ).scalars().all()
    if not rows:
        return ""

    limit = 20 if audience != "client" else 8
    out: list[str] = []
    for a in rows:
        if not is_visible_to(a.kind, audience):
            continue
        ts = a.occurred_at.strftime("%Y-%m-%d %H:%M") if a.occurred_at else "?"
        payload = filter_payload_for_audience(a.payload, kind=a.kind, audience=audience)
        line = f"  - [{ts}] {a.summary}"
        changes = (payload or {}).get("changes") if isinstance(payload, dict) else None
        if isinstance(changes, list) and changes:
            # Inline the structured diff so the AI sees what actually
            # changed, not just that something did. Cap at 5 per row.
            # format_field_change humanizes both label and value
            # ("Base rate: 7.50% → 7.80%") so the AI talks about the
            # same numbers a person would read in the UI.
            for c in changes[:5]:
                if not isinstance(c, dict):
                    continue
                line += f"\n      · {format_field_change(c)}"
            if len(changes) > 5:
                line += f"\n      · …and {len(changes) - 5} more"
        out.append(line)
        if len(out) >= limit:
            break

    return "\n".join(out) if out else ""


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


def _lender_extract_block(loan: Loan, *, audience: Audience) -> str:
    """Render the lender-thread structured extract for the system prompt.

    Operator audiences (super_admin) get the full extract — internal
    AND external action items. Broker (realtor) and client get the
    externals-only filtered view that the extractor pre-computed on
    `loans.living_profile.lender_extract_external`. The internal/
    external split is the same load-bearing principle as the
    pii_filter one-way mirror — items tagged internal stay internal.
    """
    profile = loan.living_profile or {}
    if audience == "super_admin":
        extract = profile.get("lender_extract") or {}
    else:
        # broker (realtor) AND client see the same softened view.
        extract = profile.get("lender_extract_external") or {}
    if not extract:
        return ""

    situation = (extract.get("current_situation") or "").strip()
    items = extract.get("action_items") or []
    status_changes = extract.get("status_changes") or []
    if not (situation or items or status_changes):
        return ""

    lines: list[str] = []
    if situation:
        lines.append(f"Where we stand: {situation}")
    if items:
        lines.append("Open action items:")
        for it in items[:12]:  # cap to keep token cost bounded
            owner = it.get("owner", "?")
            summary = (it.get("summary") or "").strip()
            priority = it.get("priority", "med")
            due = it.get("due_date")
            sensitivity = it.get("sensitivity", "?")
            extras: list[str] = []
            if due:
                extras.append(f"due {due}")
            docs = it.get("requested_documents") or []
            if docs:
                extras.append("docs: " + ", ".join(docs[:3]))
            amts = it.get("amounts") or []
            if amts:
                extras.append("amounts: " + ", ".join(amts[:3]))
            tail = f" ({'; '.join(extras)})" if extras else ""
            lines.append(
                f"  - [{owner}, {priority}, {sensitivity}] {summary}{tail}"
            )
    if status_changes:
        lines.append("Recent status changes:")
        for sc in status_changes[:6]:
            kind = sc.get("kind", "other")
            summary = (sc.get("summary") or "").strip()
            lines.append(f"  - {kind}: {summary}")
    return "\n".join(lines)
