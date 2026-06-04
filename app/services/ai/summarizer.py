"""'Pocket Assistant' — the Lead Fintech Orchestrator summarizer.

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
so the 'Pocket Assistant' can warn brokers proactively when rates move against
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
from app.services.ai.usage import tracked_messages_create

log = logging.getLogger(__name__)

SUMMARIZER_SYSTEM = """### ROLE
You are the "Pocket Assistant," the Lead Fintech Orchestrator at Qualified Commercial. Your mission is to provide high-level deal oversight, identify market risks, and drive loans to completion.

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
  "next_actions": [
    {
      "title": "<imperative one-line task>",
      "kind": "call" | "doc" | "ai" | "inspect" | "milestone" | "lock" | "pay" | "closing",
      "owner": "ai" | "broker" | "client",
      "due_at": "<ISO 8601 datetime, optional>" | null,
      "relative_days": <integer days from today, optional> | null,
      "priority": "low" | "med" | "high",
      "cta": "upload_doc" | "run_credit" | "complete_profile" | "submit_prequal" | "respond_to_message" | null
    }
  ],
  "deal_health": "on_track" | "at_risk" | "stuck"
}

### NEXT ACTIONS GUIDANCE
- Maximum 5 actions per response. Quality over quantity.
- `owner=ai`     : something the system will auto-do (watch a doc, regenerate
                  the summary on inbound). Lands directly on the calendar.
- `owner=broker` : the operator needs to act. Routed through an AITask the
                  broker approves before it hits anyone's calendar.
- `owner=client` : the borrower needs to act. Always routed through a
                  broker AITask first — never written straight to a
                  client-visible calendar.
- Provide `due_at` (absolute, ISO 8601 with timezone) OR `relative_days`,
  not both. The system prefers `due_at`.
- `cta` is the borrower-facing button code when owner=client. Use null
  when no concrete UI route applies.

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
    # Prefer the typed list when available — surfaces the highest-priority
    # broker-owned action. Falls back to the legacy dict for older
    # profiles that pre-date Phase 7.
    typed = profile.get("next_actions_v2") or []
    headline: str | None = None
    if isinstance(typed, list) and typed:
        broker_items = [a for a in typed if isinstance(a, dict) and a.get("owner") == "broker"]
        if broker_items:
            headline = str(broker_items[0].get("title", "")).strip() or None
    if headline is None:
        legacy = profile.get("next_actions") or {}
        broker = legacy.get("broker") or []
        if broker:
            headline = str(broker[0])
    if headline:
        parts.append("Broker action: " + headline)
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

    # Synthesize a typed next_actions_v2 list. Legacy dict is derived
    # from it via _normalize_profile, so the stub goes through the
    # same shape coercion the LLM output does.
    stub_actions: list[dict[str, Any]] = []
    if pending:
        stub_actions.append({
            "title": f"Push borrower on {pending[0]}",
            "kind": "doc",
            "owner": "broker",
            "due_at": None,
            "relative_days": 1,
            "priority": "high" if len(pending) >= 3 else "med",
            "cta": None,
        })
    elif market_pulse and market_pulse.get("warning") == "Rate Pressure":
        stub_actions.append({
            "title": "Lock rate while index is climbing",
            "kind": "lock",
            "owner": "broker",
            "due_at": None,
            "relative_days": 0,
            "priority": "high",
            "cta": None,
        })
    if activities:
        stub_actions.append({
            "title": f"Watching for: {last_activity}",
            "kind": "ai",
            "owner": "ai",
            "due_at": None,
            "relative_days": 1,
            "priority": "low",
            "cta": None,
        })

    return {
        "current_status": status,
        "market_context": market_block,
        "bottlenecks": pending[:5],
        "next_actions_v2": stub_actions,
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
    # feedback bend the next summary AND gives the 'Pocket Assistant' the live
    # FRED data to warn about rate pressure.
    extra_context = ""
    try:
        extra_context = await assemble_loan_context(db, loan, audience="super_admin")
    except Exception:  # noqa: BLE001
        extra_context = ""

    # Keep the system prompt STABLE (cacheable) and push the volatile
    # per-loan context into the user message — otherwise the cached
    # prefix changes every call (loan ids, FRED rates, activity) and we
    # get ~0% cache hits. With this split the SUMMARIZER_SYSTEM prefix
    # caches across all loans.
    user_content = "Generate the Living Loan Profile JSON for this deal:\n\n" + json.dumps(payload, indent=2)
    if extra_context:
        user_content += "\n\nLOAN CONTEXT:\n" + extra_context

    try:
        client = get_client()
        resp = await tracked_messages_create(
            db,
            feature="loan_summary",
            client=client,
            model=model_light(),
            client_id=loan.client_id,
            loan_id=loan.id,
            max_tokens=600,
            system=[
                {"type": "text", "text": SUMMARIZER_SYSTEM, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        from app.services.ai.orchestrator import record_usage

        await record_usage(
            model_light(),
            resp.usage,
            {"activity": "summarizer", "loan_id": loan.id, "broker_id": loan.broker_id},
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


_ALLOWED_OWNERS = {"ai", "broker", "client"}
_ALLOWED_KINDS = {"call", "doc", "ai", "inspect", "milestone", "lock", "pay", "closing"}
_ALLOWED_PRIORITIES = {"low", "med", "medium", "high"}
_ALLOWED_CTAS = {
    "upload_doc", "run_credit", "complete_profile", "submit_prequal",
    "respond_to_message", "accept_prequal_offer", "decline_prequal_offer",
}


def _coerce_typed_action(item: Any) -> dict[str, Any] | None:
    """Coerce a single next_actions[] entry into the canonical typed
    shape. Returns None when the input is unrecoverable (e.g. dict
    with no `title`). Caller filters Nones out."""
    if isinstance(item, str):
        # The LLM (or stub) gave us a bare string — assume broker-owned,
        # generic "call" kind, no due date.
        return {
            "title": item.strip(),
            "kind": "call",
            "owner": "broker",
            "due_at": None,
            "relative_days": None,
            "priority": "med",
            "cta": None,
        }
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    if not title:
        return None
    owner = str(item.get("owner") or "broker").lower()
    if owner not in _ALLOWED_OWNERS:
        owner = "broker"
    kind = str(item.get("kind") or "call").lower()
    if kind not in _ALLOWED_KINDS:
        kind = "call"
    priority = str(item.get("priority") or "med").lower()
    if priority not in _ALLOWED_PRIORITIES:
        priority = "med"
    if priority == "medium":
        priority = "med"
    cta = item.get("cta")
    if cta is not None:
        cta = str(cta).lower()
        if cta not in _ALLOWED_CTAS:
            cta = None
    due_at = item.get("due_at")
    if due_at is not None and not isinstance(due_at, str):
        due_at = None
    relative_days = item.get("relative_days")
    if relative_days is not None:
        try:
            relative_days = int(relative_days)
        except (TypeError, ValueError):
            relative_days = None
    return {
        "title": title,
        "kind": kind,
        "owner": owner,
        "due_at": due_at,
        "relative_days": relative_days,
        "priority": priority,
        "cta": cta,
    }


def _legacy_dict_from_typed_actions(actions: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Project the typed list back into the old {ai: [], broker: []}
    dict so frontends still reading the legacy shape (e.g.
    LoanSummaryCard) don't break during the transition."""
    legacy: dict[str, list[str]] = {"ai": [], "broker": []}
    for a in actions:
        owner = a.get("owner")
        title = a.get("title", "")
        if owner == "ai":
            legacy["ai"].append(title)
        else:
            # Both broker- and client-owned actions surface to the
            # broker in the legacy view (operator decides which
            # client-facing items to relay).
            legacy["broker"].append(title)
    return legacy


def _normalize_profile(raw: dict[str, Any], market_pulse: dict | None) -> dict[str, Any]:
    """Defensive shape coercion — model may omit fields, return strings
    where lists are expected, may produce the old dict shape OR the
    new typed list. Always emits BOTH shapes:
      next_actions       — canonical typed list (Phase 7 contract)
      next_actions_legacy — {ai: [], broker: []} for back-compat with
                            consumers that haven't migrated yet (e.g.
                            LoanSummaryCard.tsx)."""
    market_ctx = raw.get("market_context") or {}
    if isinstance(market_ctx, str):
        market_ctx = {"narrative": market_ctx, "warning": None}
    bottlenecks = raw.get("bottlenecks") or []
    if isinstance(bottlenecks, str):
        bottlenecks = [bottlenecks]

    raw_next = raw.get("next_actions")
    typed_actions: list[dict[str, Any]] = []
    if isinstance(raw_next, list):
        # New shape — list of objects (or strings).
        for item in raw_next:
            coerced = _coerce_typed_action(item)
            if coerced is not None:
                typed_actions.append(coerced)
    elif isinstance(raw_next, dict):
        # Legacy shape — synthesize typed entries from the dict's
        # string lists. Owner derived from which key holds them.
        for owner_key, items in raw_next.items():
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list):
                continue
            owner = owner_key if owner_key in _ALLOWED_OWNERS else "broker"
            for s in items:
                coerced = _coerce_typed_action({"title": str(s), "owner": owner})
                if coerced is not None:
                    typed_actions.append(coerced)
    typed_actions = typed_actions[:5]
    legacy_next = _legacy_dict_from_typed_actions(typed_actions)

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
        # Legacy compat — frontends reading next_actions.ai / .broker.
        "next_actions": legacy_next,
        # New canonical contract — typed list with kind/owner/due/etc.
        # Phase 9 frontend reads from this.
        "next_actions_v2": typed_actions,
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
        # Stub returns the new typed shape but doesn't go through
        # _normalize_profile; pass it through here so the legacy
        # next_actions dict gets derived consistently with the LLM
        # path.
        profile = _normalize_profile(
            _stub_profile(loan, list(activities), list(docs), market_pulse),
            market_pulse,
        )

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

    # Phase 7 — materialize the typed next_actions onto the calendar
    # / AITask queue. Local import to avoid circular at module load
    # (calendar_intent depends on app.services.ai.engagement which
    # depends on app.models.loan which is already imported above).
    typed_actions = profile.get("next_actions_v2") or []
    if isinstance(typed_actions, list) and typed_actions:
        try:
            from app.services.ai.calendar_intent import persist_next_actions
            await persist_next_actions(db, loan, typed_actions)
        except Exception:
            log.exception("calendar_intent.persist_next_actions failed loan=%s", loan.id)
            # Don't fail the summary write on calendar persistence — the
            # profile is already saved; calendar materialization can
            # retry on the next refresh.

    return SummaryResult(
        summary=summary_text,
        deal_health=deal_health,
        living_profile=profile,
        used_stub=used_stub,
    )
