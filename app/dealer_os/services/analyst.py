"""Dealer OS AI Analyst (Phase 2).

Feeds the SAME one-call bundle the lender package renders (dealer profile,
latest snapshot, targets, periods, addbacks, plan, forecast, funding paths)
to the intake pipeline's Bedrock stack (tracked usage, feature
'dealer_os_analyst') and returns a strict-JSON advisory:

    {"narrative": str, "strengths": [str], "risks": [str],
     "suggested_actions": [{"title", "category", "owner", "timeline",
                            "expected_effect", "rationale"}]}

Guardrail (in the system prompt, non-negotiable): only legitimate treasury /
structuring recommendations — never statement window-dressing, temporary
balance-inflating transfers, or tax figures massaged toward bank activity.

build_user_prompt / coerce_insights are pure (unit-testable, no IO);
generate_insights owns the model call. Flushes nothing — read-only apart
from the usage row tracked_messages_create records.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

# READ-ONLY reuse of the intake pipeline's Bedrock client + usage tracking.
from app.services.ai.bedrock_client import get_client, model_heavy
from app.services.ai.usage import tracked_messages_create

from ..models import DealerBusiness
from .extract import _parse_model_json

logger = logging.getLogger(__name__)

_ALLOWED_CATEGORIES = {"dscr", "ebitda", "liquidity", "docs"}

ANALYST_SYSTEM = """You are the Dealer Capital OS credit analyst — a conservative commercial-lending advisor reviewing one auto dealer's monitored financials for the internal team.

The user message contains the dealer's full lender bundle as JSON: profile, latest metric snapshot (EBITDA, DSCR, average daily balance, score/tier), metric targets, monthly financial periods, add-backs, the current action plan, the 12-month forecast, and funding-path/ladder readiness.

Return ONLY strict JSON (no markdown, no code fences, no commentary) with exactly this shape:
{
  "narrative": "3-6 sentence plain-English credit assessment of where this dealer stands and what moves the needle",
  "strengths": ["short bullet", ...],
  "risks": ["short bullet", ...],
  "suggested_actions": [
    {"title": "string (<=200 chars)",
     "category": "dscr" | "ebitda" | "liquidity" | "docs",
     "owner": "string or null (who should do it, e.g. 'Dealer principal', 'QC team')",
     "timeline": "string or null (e.g. '30 days', 'immediate · ongoing')",
     "expected_effect": "string or null (<=120 chars, e.g. 'DSCR +0.10x')",
     "rationale": "string or null (why this action, tied to the bundle's numbers)"}
  ]
}

Hard rules — never break these:
- Recommend ONLY legitimate treasury and deal-structuring actions: real cost reductions, verified add-back documentation, debt restructuring/refinancing, genuine deposit-consolidation of actual operating revenue, reserve building, documentation hygiene.
- NEVER recommend statement window-dressing: no temporary transfers to inflate balances, no timing deposits around statement cut-offs, no round-tripping funds between accounts, no cosmetic activity designed to make bank statements look stronger than the business is.
- Tax figures must reflect accurate filings — never suggest adjusting reported revenue or tax positions to "match" bank activity or dress up a package. If filed revenue and observed deposits disagree, the action is to investigate and document the real cause, not to change numbers.
- Ground every claim in the bundle's actual numbers; do not invent figures. Use null for any field you cannot support.
- 3-6 suggested_actions, each concretely tied to this dealer's data."""


def build_user_prompt(dealer_name: str, bundle: dict[str, Any]) -> str:
    """Pure: the user-message text handed to the model."""
    return (
        f"Dealer: {dealer_name}\n"
        "Full lender bundle JSON follows. Assess it and return only the required JSON.\n\n"
        + json.dumps(bundle, default=str)
    )


def _str_list(raw: Any, limit: int = 12) -> list[str]:
    if not isinstance(raw, list):
        return []
    out = [str(x).strip() for x in raw if isinstance(x, (str, int, float)) and str(x).strip()]
    return out[:limit]


def coerce_insights(parsed: dict[str, Any]) -> dict[str, Any]:
    """Pure defensive coercion of model JSON into the response contract —
    drops malformed actions, clamps categories to the allowed set."""
    actions: list[dict[str, Any]] = []
    raw_actions = parsed.get("suggested_actions")
    if isinstance(raw_actions, list):
        for a in raw_actions[:10]:
            if not isinstance(a, dict):
                continue
            title = str(a.get("title") or "").strip()[:200]
            if not title:
                continue
            category = str(a.get("category") or "").strip().lower()
            if category not in _ALLOWED_CATEGORIES:
                category = "liquidity"

            def _opt(key: str, cap: int) -> str | None:
                v = a.get(key)
                s = str(v).strip() if v is not None else ""
                return s[:cap] or None

            actions.append(
                {
                    "title": title,
                    "category": category,
                    "owner": _opt("owner", 80),
                    "timeline": _opt("timeline", 80),
                    "expected_effect": _opt("expected_effect", 120),
                    "rationale": _opt("rationale", 2000),
                }
            )
    return {
        "narrative": str(parsed.get("narrative") or "").strip(),
        "strengths": _str_list(parsed.get("strengths")),
        "risks": _str_list(parsed.get("risks")),
        "suggested_actions": actions,
    }


async def generate_insights(
    db: AsyncSession, dealer: DealerBusiness, bundle: dict[str, Any]
) -> dict[str, Any]:
    """Call the analyst model over the lender bundle. Raises ValueError when
    the model returns unusable JSON (router maps that to a 502)."""
    resp = await tracked_messages_create(
        db,
        feature="dealer_os_analyst",
        client=get_client(),
        model=model_heavy(),
        metadata={"dealer_id": str(dealer.id)},
        max_tokens=4000,
        system=ANALYST_SYSTEM,
        messages=[{"role": "user", "content": build_user_prompt(dealer.name, bundle)}],
    )
    text = "".join(
        getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text"
    )
    return coerce_insights(_parse_model_json(text))
