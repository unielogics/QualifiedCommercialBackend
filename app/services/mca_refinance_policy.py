"""Intrinsic MCA refinance boundaries, independent of prompts and catalog versions.

An out-of-scope file is still an application. It belongs with the desk, never
in the automatic underwriting queue. Client declarations and signed typed
terms establish facts; AI-proposed facts can tighten this gate, not open it.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from numbers import Real
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

MAX_AMOUNT = 350_000
MAX_TERM_MONTHS = 120
MAX_ACTIVE_MCAS = 2
POLICY_KEY = "mca_refinance_policy"


def message(policy: dict[str, Any], language: str = "en") -> str:
    if policy["review_mode"] == "manual_review":
        return (
            "Su solicitud está aceptada para revisión manual del equipo. El programa estándar admite hasta $350,000, plazos de hasta 10 años y un máximo de dos MCA activos. No se ejecutará evaluación automática ni una consulta de crédito automática para este expediente. Puede continuar con los documentos y reservar una llamada."
            if language == "es" else
            "Your application is accepted for manual desk review. The standard program supports up to $350,000, terms up to 10 years and no more than two active MCAs. Automatic underwriting and an automatic credit check will not run for this file. You can continue providing documents and book a call."
        )
    return (
        "Confirme el número de MCA activos y el monto solicitado antes de la evaluación automática. Puede continuar guardando su solicitud y documentos."
        if language == "es" else
        "Confirm the active MCA count and requested amount before automatic underwriting. You can continue saving your application and documents."
    )


def blocked_result(policy: dict[str, Any]) -> dict[str, Any]:
    manual = policy["review_mode"] == "manual_review"
    return {"screening_stage": "manual_review_required" if manual else "eligibility_information_required",
            "probability_status": "Manual review required" if manual else "Eligibility information required",
            "executive_summary": message(policy), "one_next_step": "Desk review" if manual else "Confirm MCA count and requested amount",
            "booking_recommended": manual, "manual_review_required": manual, POLICY_KEY: policy}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (Real, Decimal)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    value = _number(value)
    return int(value) if value is not None and value.is_integer() else None


def _has_mca_intent(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower().replace("-", "_").replace(" ", "_") in {
        "mca_refi_v1", "mca_refinance", "mca_refinance_intake",
        "merchant_cash_advance_refinance",
    }


def is_mca_refinance(intake: Any = None, bucket: Any = None, context: Any = None) -> bool:
    if _has_mca_intent(getattr(intake, "variant", None)) or _has_mca_intent(getattr(intake, "loan_purpose", None)):
        return True
    if _has_mca_intent(getattr(bucket, "bucket_type", None)):
        return True
    state = _mapping(getattr(intake, "intake_state", None))
    sources = [_mapping(getattr(bucket, "ai_context", None)), _mapping(context), state,
               _mapping(state.get("signup_attribution")), _mapping(state.get("main_street_details"))]
    for source in sources:
        if any(_has_mca_intent(source.get(key)) for key in ("review_type", "funding_intent", "intent", "source", "program", "program_key", "loan_purpose")):
            return True
        for key in ("selected_programs", "selected_program_keys", "programs"):
            rows = source.get(key)
            if isinstance(rows, list) and any(
                _has_mca_intent(row) or any(_has_mca_intent(_mapping(row).get(k)) for k in ("program_key", "key", "slug"))
                for row in rows
            ):
                return True
    return False


def evaluate(intake: Any = None, *, bucket: Any = None, context: Any = None) -> dict[str, Any]:
    state = _mapping(getattr(intake, "intake_state", None))
    current = _mapping(getattr(bucket, "ai_context", None))
    snapshot = _mapping(context)
    declaration = _mapping(state.get("mca_qualification")) or _mapping(current.get("mca_qualification"))
    typed_terms = _mapping(state.get("mca_terms_summary")) or _mapping(current.get("mca_terms_summary"))
    hold = _mapping(state.get("mca_manual_review_hold"))
    details = [_mapping(state.get("mca_details")), _mapping(current.get("mca_details")), _mapping(snapshot.get("mca_details"))]
    confirmed_counts = [_count(declaration.get("active_mca_count")), _count(typed_terms.get("active_mca_count"))]
    counts = [*confirmed_counts, _count(hold.get("active_mca_count")), *[_count(item.get("advance_count")) for item in details]]
    counts = [value for value in counts if value is not None]
    active_count = max(counts) if counts else None
    confirmed_amounts = [
        _number(declaration.get("requested_amount")),
        _number(typed_terms.get("remaining_payback")),
        _number(getattr(intake, "requested_loan_amount", None)),
    ]
    amounts = [*confirmed_amounts, _number(hold.get("requested_amount")), *[
        _number(item.get(key)) for item in details for key in ("remaining_payback", "requested_amount")
    ]]
    amounts = [value for value in amounts if value is not None]
    amount = max(amounts) if amounts else None
    terms = [_number(declaration.get("requested_term_months")), _number(hold.get("requested_term_months")), _number(current.get("requested_term_months")), _number(snapshot.get("requested_term_months"))]
    terms = [value for value in terms if value is not None]
    term = max(terms) if terms else None
    manual = []
    if state.get("mca_manual_review_requested_at"):
        manual.append("manual_review_requested")
    if active_count is not None and active_count > MAX_ACTIVE_MCAS:
        manual.append("more_than_two_active_mcas")
    if amount is not None and amount > MAX_AMOUNT:
        manual.append("amount_above_program_maximum")
    if term is not None and term > MAX_TERM_MONTHS:
        manual.append("term_above_program_maximum")
    missing = []
    if not any(value is not None and value > 0 for value in confirmed_counts):
        missing.append("active_mca_count_required")
    if not any(value is not None and value > 0 for value in confirmed_amounts):
        missing.append("requested_amount_required")
    mode = "manual_review" if manual else "information_required" if missing else "automated_review"
    return {
        "review_mode": mode,
        "auto_underwriting_allowed": mode == "automated_review",
        "reason_codes": manual + missing,
        "active_mca_count": active_count,
        "requested_amount": amount,
        "requested_term_months": term,
        "max_amount": MAX_AMOUNT,
        "max_term_months": MAX_TERM_MONTHS,
        "max_active_mcas": MAX_ACTIVE_MCAS,
        "manual_review_requested": bool(state.get("mca_manual_review_requested_at")),
    }


def persist(intake: Any, *, request_manual: bool = False, bucket: Any = None) -> dict[str, Any]:
    state = dict(_mapping(getattr(intake, "intake_state", None)))
    if request_manual:
        state.setdefault("mca_manual_review_requested_at", datetime.now(UTC).isoformat())
        intake.intake_state = state
    bucket = bucket if bucket is not None else getattr(intake, "bucket", None)
    policy = evaluate(intake, bucket=bucket)
    if policy["review_mode"] == "manual_review":
        previous = _mapping(state.get("mca_manual_review_hold"))
        state["mca_manual_review_hold"] = {
            "active_mca_count": policy["active_mca_count"],
            "requested_amount": policy["requested_amount"],
            "requested_term_months": policy["requested_term_months"],
            "reason_codes": list(dict.fromkeys([*previous.get("reason_codes", []), *policy["reason_codes"]])),
            "observed_at": previous.get("observed_at") or datetime.now(UTC).isoformat(),
        }
    state[POLICY_KEY] = policy
    intake.intake_state = state
    if policy["review_mode"] == "manual_review" and getattr(intake, "status", None) in {"collecting", "reviewed", "completed", "reviewing"}:
        intake.status = "manual_review"
    if bucket is not None:
        bucket.ai_context = {**_mapping(getattr(bucket, "ai_context", None)), POLICY_KEY: policy,
                             "mca_qualification": _mapping(state.get("mca_qualification")),
                             "mca_terms_summary": _mapping(state.get("mca_terms_summary"))}
    return policy


def evaluate_fit_context(context: dict[str, Any]) -> dict[str, Any]:
    """Apply the same intrinsic policy to deterministic, materialized fit facts."""
    source = SimpleNamespace(intake_state={"mca_qualification": {
        "active_mca_count": context.get("active_mca_count"),
        "requested_amount": context.get("requested_amount"),
        "requested_term_months": context.get("requested_term_months"),
    }}, requested_loan_amount=None)
    return evaluate(source)


def evidence_bounds(analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """Conservative obligation bounds, deduplicating agreements and payoffs.

    A file is not a position. Distinct documented identifiers distinguish
    same-funder advances; absent identifiers, distinct funders are only a
    lower bound. A payoff without an identifier cannot add a second advance
    to an already identified funder.
    """
    counts: list[int] = []
    balances: list[float] = []
    groups: dict[str, dict[str, Any]] = {}
    for item in analyses:
        facts = _mapping(item.get("key_facts"))
        count = _count(facts.get("advance_count"))
        if count is not None:
            counts.append(count)
        if item.get("ai_classification") not in {"debt_schedule", "advance_agreement", "mca_agreement", "payoff_letter"}:
            continue
        total = _number(facts.get("total_outstanding_balance"))
        if total is not None:
            balances.append(total)
        rows = facts.get("debts")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            funder = re.sub(r"[^a-z0-9]", "", str(row.get("lender") or row.get("funder") or "").casefold())
            identity = next((str(row[key]).strip().casefold() for key in ("contract_id", "advance_id", "agreement_id", "account_number", "account_id") if row.get(key)), None)
            if not funder and not identity:
                continue
            key = funder or f"identified:{identity}"
            group = groups.setdefault(key, {"ids": {}, "unidentified_balance": 0.0})
            balance = _number(row.get("current_balance")) or 0.0
            if identity:
                group["ids"][identity] = max(balance, group["ids"].get(identity, 0.0))
            else:
                group["unidentified_balance"] = max(balance, group["unidentified_balance"])
    if groups:
        counts.append(sum(max(1, len(group["ids"])) for group in groups.values()))
        balances.append(sum(max(sum(group["ids"].values()), group["unidentified_balance"]) for group in groups.values()))
    return {"active_mca_count": max(counts) if counts else None, "remaining_payback": max(balances) if balances else None}


async def for_bucket(db: Any, bucket: Any, *, intake: Any = None, context: Any = None) -> dict[str, Any] | None:
    if intake is None and is_mca_refinance(bucket=bucket, context=context):
        from app.models.public_underwriting_intake import PublicUnderwritingIntake
        intake = (await db.execute(select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id == bucket.id))).scalar_one_or_none()
    if not is_mca_refinance(intake, bucket, context):
        return None
    policy = evaluate(intake, bucket=bucket, context=context)
    if intake is not None:
        persist(intake, bucket=bucket)
    if bucket is not None:
        bucket.ai_context = {**_mapping(getattr(bucket, "ai_context", None)), POLICY_KEY: policy}
    return policy


async def require_automatic(db: Any, bucket: Any, *, intake: Any = None) -> dict[str, Any] | None:
    policy = await for_bucket(db, bucket, intake=intake)
    if policy is not None and not policy["auto_underwriting_allowed"]:
        raise HTTPException(409, detail={"code": "mca_refinance_manual_review_required", POLICY_KEY: policy,
                                         "message": "This application is accepted for desk review; automatic underwriting is unavailable."})
    return policy
