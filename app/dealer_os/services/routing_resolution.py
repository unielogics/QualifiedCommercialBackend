"""Audited Step 4 routing recommendations and blocker presentation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    DealerApplicationProfile,
    DealerApplicationRecommendation,
    DealerBusiness,
)


PROGRAM_RANGES: dict[str, tuple[float, float]] = {
    "term_loan_3_5_year": (25_000.0, 500_000.0),
    "term_loan_10_year": (15_000.0, 50_000.0),
}


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in {float("inf"), float("-inf")} else None


def classify_rule(rule_id: str, explanation: str, *, unresolved: bool = False) -> str:
    text = f"{rule_id} {explanation}".lower()
    if unresolved or any(word in text for word in ("confirm", "calculate", "statement", "missing", "classification review")):
        return "missing_evidence"
    if "amount" in text or "cap" in text or "range" in text:
        return "unsupported_amount"
    if any(word in text for word in ("naics", "credit", "bankruptcy", "felony", "ofac", "residency", "citizen", "foreclosure", "legal", "tax_lien", "judgment", "restricted")):
        return "hard_restriction"
    return "conflicting_information"


def blockers(routing_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for program in (routing_result or {}).get("programs") or []:
        key = str(program.get("program_key") or "")
        name = str(program.get("name") or key)
        for rule in program.get("matched_rules") or []:
            rule_id = str(rule.get("rule_id") or "rule")
            explanation = str(rule.get("explanation") or "This fact blocks the selected path.")
            result.append({
                "program_key": key,
                "program_name": name,
                "rule_key": rule_id,
                "kind": classify_rule(rule_id, explanation),
                "source": str(rule.get("matched_value") or "Application and verified evidence"),
                "explanation": explanation,
                "corrective_action": "Correct the source fact or request a documented super-admin exception." if classify_rule(rule_id, explanation) == "hard_restriction" else "Review the current fact and apply a supported structure.",
                "hard": classify_rule(rule_id, explanation) == "hard_restriction",
            })
        for index, explanation_value in enumerate(program.get("unresolved") or []):
            explanation = str(explanation_value)
            result.append({
                "program_key": key,
                "program_name": name,
                "rule_key": f"{key}.unresolved.{index}",
                "kind": "missing_evidence",
                "source": "Application or document evidence",
                "explanation": explanation,
                "corrective_action": "Answer the question, confirm the extracted value, or upload the named evidence.",
                "hard": False,
            })
    return result


def _program_by_key(routing_result: dict[str, Any], key: str | None) -> dict[str, Any] | None:
    return next(
        (row for row in routing_result.get("programs") or [] if row.get("program_key") == key),
        None,
    )


def _recommendation_values(
    dealer: DealerBusiness,
    profile: DealerApplicationProfile | None,
    routing_result: dict[str, Any],
) -> dict[str, Any] | None:
    amount = _float(dealer.funding_goal or dealer.client_requested_amount)
    original = _float(dealer.client_requested_amount or dealer.funding_goal)
    current_program = (profile.selected_program if profile else None) or dealer.client_requested_program
    rows = list(routing_result.get("programs") or [])
    current_row = _program_by_key(routing_result, current_program)
    viable = [row for row in rows if row.get("status") != "blocked"]

    recommended_program: str | None = None
    recommended_amount: float | None = None
    reasons: list[dict[str, Any]] = []

    if current_row and current_row.get("status") == "blocked" and viable:
        alternate = sorted(viable, key=lambda row: row.get("status") != "recommended")[0]
        recommended_program = str(alternate.get("program_key") or "") or None
        recommended_amount = min(amount or 0, _float(alternate.get("estimated_max_amount")) or amount or 0) or None
        reasons.append({
            "kind": "alternative_program",
            "message": f"{current_row.get('name') or current_program} is blocked; {alternate.get('name') or recommended_program} remains available for review.",
        })

    amount_candidates: list[tuple[float, str, float, float, float]] = []
    if amount is not None:
        for row in rows:
            key = str(row.get("program_key") or "")
            bounds = PROGRAM_RANGES.get(key)
            if not bounds:
                continue
            non_amount_rules = [
                rule for rule in row.get("matched_rules") or []
                if ".amount" not in str(rule.get("rule_id") or "")
                and "amount_cap" not in str(rule.get("rule_id") or "")
            ]
            if non_amount_rules:
                continue
            low, configured_high = bounds
            evidence_high = _float(row.get("estimated_max_amount")) or configured_high
            high = min(configured_high, evidence_high)
            adjusted = min(max(amount, low), high)
            amount_candidates.append((abs(amount - adjusted), key, adjusted, low, high))
    if amount_candidates and not any(
        low <= (amount or 0) <= high for _, _, _, low, high in amount_candidates
    ):
        _, key, adjusted, low, high = sorted(amount_candidates)[0]
        recommended_program = key
        recommended_amount = adjusted
        reasons.append({
            "kind": "unsupported_amount",
            "message": f"The original ${original or amount:,.0f} request is outside the supported ${low:,.0f}-${high:,.0f} range for this path.",
            "matched_rule": f"{key}.amount",
        })

    if recommended_program is None and routing_result.get("amount_adjustment_required"):
        recommended_amount = _float(routing_result.get("recommended_amount"))
        candidate = sorted(viable, key=lambda row: row.get("status") != "recommended")
        recommended_program = str(candidate[0].get("program_key")) if candidate else current_program
        reasons.append({
            "kind": "unsupported_amount",
            "message": "Verified evidence supports a lower working amount than the client originally requested.",
        })

    if not recommended_program or recommended_amount is None:
        return None
    low, high = PROGRAM_RANGES.get(recommended_program, (0.0, recommended_amount))
    if current_program == recommended_program and amount == recommended_amount:
        return None
    return {
        "current_amount": amount,
        "current_program": current_program,
        "recommended_amount": recommended_amount,
        "recommended_program": recommended_program,
        "supported_min": low,
        "supported_max": high,
        "reasons": reasons,
    }


async def ensure_recommendation(
    db: AsyncSession,
    dealer: DealerBusiness,
    profile: DealerApplicationProfile | None,
    routing_result: dict[str, Any] | None,
    actor_id,
) -> tuple[DealerApplicationRecommendation | None, bool]:
    if not routing_result:
        return None, False
    values = _recommendation_values(dealer, profile, routing_result)
    if values is None:
        return None, False
    existing = (
        await db.execute(
            select(DealerApplicationRecommendation)
            .where(
                DealerApplicationRecommendation.dealer_id == dealer.id,
                DealerApplicationRecommendation.status == "pending",
            )
            .order_by(DealerApplicationRecommendation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    comparable = (
        values["current_amount"], values["current_program"],
        values["recommended_amount"], values["recommended_program"],
    )
    if existing and (
        _float(existing.current_amount), existing.current_program,
        _float(existing.recommended_amount), existing.recommended_program,
    ) == comparable:
        return existing, False
    if existing:
        existing.status = "superseded"
    row = DealerApplicationRecommendation(
        dealer_id=dealer.id,
        rules_version=str(routing_result.get("rules_version") or "unknown"),
        created_by_user_id=actor_id,
        **values,
    )
    db.add(row)
    await db.flush()
    return row, True
