"""'The Associate' — the Lead Fintech Orchestrator summarizer.

Every time a loan-touching activity lands (loan.created, doc.received,
message.inbound, stage_change, etc.), call `refresh_summary(loan_id)` to
regenerate the structured Living Loan Profile and write it back to the
loans row (status_summary + deal_health + living_profile).

Outputs the 4-section profile defined in the AI brief:
  1. CURRENT STATUS  — 1-sentence executive brief
  2. MARKET CONTEXT  — index + spread + trend, with a "Rate Pressure" flag
                       when the FRED benchmark has climbed in the last week
  3. BOTTLENECKS     — list of the docs/responses stalling the deal
  4. NEXT ACTIONS    — split into [AI ACTION] + [BROKER ACTION]

The market context is grounded in the live FRED feed (services/fred.py)
so 'The Associate' can warn brokers proactively when rates move against
them — the same number the dashboard widget renders.

Uses Claude Haiku — cheap, fast, perfect for this short summarization. When
ANTHROPIC_API_KEY is unset we fall back to a deterministic heuristic so the
UI is never empty.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import DealHealth, DocStatus
from app.models.activity import Activity
from app.models.document import Document
from app.models.loan import Loan
from app.services.ai.anthropic_client import get_client, model_light
from app.services.ai.context import assemble_loan_context, get_market_pulse

log = logging.getLogger(__name__)

SUMMARIZER_SYSTEM = """### ROLE
You are "The Associate," the Lead Fintech Orchestrator at Qualified Commercial. Your mission is to provide high-level deal oversight, identify market risks, and drive loans to completion.

### DATA CONTEXT PROVIDED
1. **Activity Log:** Recent emails, uploads, and system events.
2. **Market Pulse (FRED):** Live Index values (DGS10, SOFR, etc.) + your defined Lender Spreads.
3. **Doc Vault:** Current status of required files (S3).
4. **Previous Summary:** The last known state of the loan.

### OPERATING PRINCIPLES
- **Institutional Tone:** You are professional, organized, and institutional. No "fluff."
- **Privacy Gate:** NEVER reveal specific lender names (e.g., "JPM") to brokers or clients. Refer to them as "The Lender" or "The Underwriter."
- **Market Sensitivity:** Use live FRED data to warn of interest rate pressure. If benchmarks rise, treat it as a high-priority risk.
- **Human-in-the-Loop:** You draft; you suggest; you log. You do not finalize major legal or financial commitments without Broker approval.

### OUTPUT FORMAT
Respond in strict JSON with this shape (no markdown, no code fences):

{
  "current_status": "<one sentence executive brief, e.g. 'Underwriting: Preliminary Doc Review'>",
  "market_context": {
    "narrative": "<1-2 sentence market analysis. Reference the index, spread, and the 7-day or 30-day trend. Use phrases like 'Rate Pressure' when the benchmark is climbing, 'Rate Stability' when it's flat, 'Rate Easing' when falling.>",
    "warning": "Rate Pressure" | "Rate Stability" | "Rate Easing" | null
  },
  "bottlenecks": ["<specific doc or response missing>", "..."],
  "next_actions": {
    "ai": ["<what you are auto-drafting or watching>", "..."],
    "broker": ["<what the human needs to do, e.g. 'Call Client to push for tax returns'>", "..."]
  },
  "deal_health": "on_track" | "at_risk" | "stuck"
}

### DEAL HEALTH RULES
- "on_track" = no blockers; trajectory is normal.
- "at_risk"  = soft slowdown, stale doc requests, or material rate-pressure warning that affects pricing.
- "stuck"    = hard blocker (failed UW gate, client unresponsive 7+ days, missing critical doc).

Honor any operator instructions and avoid the patterns flagged under "Recent Feedback" in the appended context.
"""


@dataclass(frozen=True)
class SummaryResult:
    summary: str  # human-readable formatted text (back-compat for older UI)
    deal_health: DealHealth
    living_profile: dict[str, Any]  # the structured 4-section output
    used_stub: bool


def _format_summary_text(profile: dict[str, Any]) -> str:
    """Render the structured profile as a single paragraph for back-compat
    consumers that read loans.status_summary as a plain string."""
    parts: list[str] = []
    status = (profile.get("current_status") or "").strip()
    if status:
        parts.append(status)
    market = profile.get("market_context") or {}
    market_narrative = (market.get("narrative") or "").strip()
    if market_narrative:
        warning = market.get("warning")
        prefix = f"[{warning}] " if warning and warning != "Rate Stability" else ""
        parts.append(prefix + market_narrative)
    bottlenecks = profile.get("bottlenecks") or []
    if bottlenecks:
        parts.append("Bottlenecks: " + "; ".join(str(b) for b in bottlenecks[:3]))
    next_actions = profile.get("next_actions") or {}
    broker = next_actions.get("broker") or []
    if broker:
        parts.append("Broker action: " + str(broker[0]))
    return " ".join(parts) if parts else "No current status."


def _stub_profile(
    loan: Loan,
    activities: list[Activity],
    docs: list[Document],
    market_pulse: dict | None,
) -> dict[str, Any]:
    """Deterministic fallback when no Anthropic key is configured."""
    pending = [d.name for d in docs if d.status in (DocStatus.PENDING, DocStatus.REQUESTED, DocStatus.FLAGGED)]
    flagged = [d.name for d in docs if d.status == DocStatus.FLAGGED]
    last_activity = activities[0].summary if activities else "No activity yet."

    if flagged:
        deal_health = DealHealth.STUCK
        status = f"{loan.stage.value.replace('_', ' ').title()}: hard block on {len(flagged)} flagged document{'s' if len(flagged) > 1 else ''}."
    elif len(pending) >= 3:
        deal_health = DealHealth.AT_RISK
        status = f"{loan.stage.value.replace('_', ' ').title()}: {len(pending)} doc requests still open."
    elif pending:
        deal_health = DealHealth.AT_RISK
        status = f"{loan.stage.value.replace('_', ' ').title()}: awaiting {pending[0]}."
    else:
        deal_health = DealHealth.ON_TRACK
        status = f"{loan.stage.value.replace('_', ' ').title()}: no open blockers."

    if market_pulse:
        warning = market_pulse.get("warning")
        narrative_parts = [
            f"{market_pulse['series_id']} at {market_pulse['index_value']:.3f}% + {market_pulse['spread_bps']} bps spread = {market_pulse['estimated_rate']:.3f}% estimated."
        ]
        if market_pulse.get("trend_7d_bps") is not None:
            sign = "+" if market_pulse["trend_7d_bps"] > 0 else ""
            narrative_parts.append(f"7-day trend {sign}{market_pulse['trend_7d_bps']} bps.")
        if warning == "Rate Pressure":
            narrative_parts.append("Recommend locking the rate soon.")
            if deal_health == DealHealth.ON_TRACK:
                deal_health = DealHealth.AT_RISK
        market_block = {"narrative": " ".join(narrative_parts), "warning": warning}
    else:
        market_block = {
            "narrative": "No FRED data available for this product yet — run the morning refresh.",
            "warning": None,
        }

    return {
        "current_status": status,
        "market_context": market_block,
        "bottlenecks": pending[:5],
        "next_actions": {
            "ai": [f"Watching for: {last_activity}"] if activities else [],
            "broker": (
                [f"Push borrower on {pending[0]}"] if pending
                else (["Lock rate while index is climbing"] if market_pulse and market_pulse.get("warning") == "Rate Pressure" else [])
            ),
        },
        "deal_health": deal_health.value,
    }


async def _llm_profile(
    db: AsyncSession,
    loan: Loan,
    activities: list[Activity],
    docs: list[Document],
    market_pulse: dict | None,
) -> dict[str, Any] | None:
    """Call Anthropic with the assembled context. Returns the parsed JSON
    profile or None if anything fails (caller falls back to the stub)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    payload: dict[str, Any] = {
        "loan": {
            "deal_id": loan.deal_id,
            "address": loan.address,
            "city": loan.city,
            "stage": loan.stage.value if hasattr(loan.stage, "value") else str(loan.stage),
            "type": loan.type.value if hasattr(loan.type, "value") else str(loan.type),
            "amount": float(loan.amount) if loan.amount else None,
            "ltv": float(loan.ltv) if loan.ltv else None,
            "dscr": float(loan.dscr) if loan.dscr else None,
            "risk_score": loan.risk_score,
        },
        "recent_activity": [
            {"kind": a.kind, "summary": a.summary, "actor": a.actor_label, "occurred_at": a.occurred_at.isoformat()}
            for a in activities[:8]
        ],
        "documents": [
            {"name": d.name, "category": d.category, "status": d.status.value if hasattr(d.status, "value") else str(d.status)}
            for d in docs
        ],
        "market_pulse": market_pulse,
        "previous_summary": loan.status_summary,
    }

    # Append the unified loan context (instructions + feedback + scenarios +
    # AI-modify corrections + market pulse). This is what makes operator
    # feedback bend the next summary AND gives 'The Associate' the live
    # FRED data to warn about rate pressure.
    extra_context = ""
    try:
        extra_context = await assemble_loan_context(db, loan, audience="super_admin")
    except Exception:  # noqa: BLE001
        extra_context = ""

    system_prompt = SUMMARIZER_SYSTEM + ("\n\n" + extra_context if extra_context else "")

    try:
        client = get_client()
        resp = await client.messages.create(
            model=model_light(),
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": "Generate the Living Loan Profile JSON for this deal:\n\n" + json.dumps(payload, indent=2)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip("` \n")
        parsed = json.loads(text)
        # Coerce shape — enforce the keys we need before handing back to the
        # caller. Missing pieces are filled with defaults so the UI never NPEs.
        return _normalize_profile(parsed, market_pulse)
    except Exception as exc:  # noqa: BLE001
        log.warning("Living Loan Profile LLM call failed: %s — falling back to stub", exc)
        return None


def _normalize_profile(raw: dict[str, Any], market_pulse: dict | None) -> dict[str, Any]:
    """Defensive shape coercion — model may omit fields, return strings
    where lists are expected, etc."""
    market_ctx = raw.get("market_context") or {}
    if isinstance(market_ctx, str):
        market_ctx = {"narrative": market_ctx, "warning": None}
    bottlenecks = raw.get("bottlenecks") or []
    if isinstance(bottlenecks, str):
        bottlenecks = [bottlenecks]
    next_actions = raw.get("next_actions") or {}
    if isinstance(next_actions, list):
        next_actions = {"ai": [], "broker": [str(x) for x in next_actions]}
    ai_actions = next_actions.get("ai") or []
    broker_actions = next_actions.get("broker") or []
    if isinstance(ai_actions, str):
        ai_actions = [ai_actions]
    if isinstance(broker_actions, str):
        broker_actions = [broker_actions]
    health_str = str(raw.get("deal_health", "on_track")).lower()
    try:
        DealHealth(health_str)
    except ValueError:
        health_str = "on_track"

    # If the model omitted the market_context but we have live pulse data,
    # synthesize a minimal one so the UI always shows something.
    if not market_ctx.get("narrative") and market_pulse:
        market_ctx = {
            "narrative": (
                f"{market_pulse['series_id']} at {market_pulse['index_value']:.3f}% + "
                f"{market_pulse['spread_bps']} bps = {market_pulse['estimated_rate']:.3f}% estimated."
            ),
            "warning": market_pulse.get("warning"),
        }

    return {
        "current_status": str(raw.get("current_status", "")).strip() or "Status pending.",
        "market_context": {
            "narrative": str(market_ctx.get("narrative", "")).strip(),
            "warning": market_ctx.get("warning"),
        },
        "bottlenecks": [str(b) for b in bottlenecks][:8],
        "next_actions": {
            "ai": [str(x) for x in ai_actions][:5],
            "broker": [str(x) for x in broker_actions][:5],
        },
        "deal_health": health_str,
    }


async def refresh_summary(db: AsyncSession, loan_id: UUID) -> SummaryResult:
    """Pull recent activity + docs + live FRED pulse, regenerate the
    structured Living Loan Profile, write back to the loan row."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise ValueError(f"Loan {loan_id} not found")

    activities = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan_id)
            .order_by(Activity.occurred_at.desc())
            .limit(20)
        )
    ).scalars().all()

    docs = (
        await db.execute(select(Document).where(Document.loan_id == loan_id).order_by(Document.name))
    ).scalars().all()

    market_pulse = await get_market_pulse(db, loan)

    profile = await _llm_profile(db, loan, list(activities), list(docs), market_pulse)
    used_stub = profile is None
    if profile is None:
        profile = _stub_profile(loan, list(activities), list(docs), market_pulse)

    deal_health = DealHealth(profile["deal_health"])
    summary_text = _format_summary_text(profile)

    loan.status_summary = summary_text
    loan.deal_health = deal_health
    loan.living_profile = profile

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="ai-summarizer",
            kind="summary.refreshed",
            summary=f"Living Loan Profile updated → {deal_health.value}",
            payload={"summary": summary_text, "deal_health": deal_health.value, "used_stub": used_stub, "warning": (profile.get("market_context") or {}).get("warning")},
        )
    )
    await db.flush()
    return SummaryResult(
        summary=summary_text,
        deal_health=deal_health,
        living_profile=profile,
        used_stub=used_stub,
    )
