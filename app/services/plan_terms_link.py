"""The recorded terms and the Financials plan are one thing.

Terms are recorded on the Underwriting tab twice over: the client terms
(every file) and the dealer's production term sheet. The plan on the
Financials tab carries the new loan the desk is modelling. They must mirror:
recording terms adopts them into the plan (payment, amount, rate, term,
structure, and the sheet's own treatment of the existing debt), withdrawing
them releases the plan, and the cockpit says whether the plan's loan still
matches what was recorded, field by field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.application_profile import ApplicationProfile

DIFFERENCE_LABELS = {
    "amount": "amount",
    "rate_pct": "rate",
    "term_months": "term",
    "interest_only": "structure",
    "payment_monthly": "payment",
}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def recorded_from_client_terms(row: Any) -> dict[str, Any] | None:
    """The current client terms as the plan reads them."""
    if row is None:
        return None
    periods = _num(getattr(row, "payments_per_year", None)) or 12.0
    payment = _num(getattr(row, "periodic_payment", None))
    return {
        "kind": "client_terms",
        "id": str(row.id),
        "version": int(row.version),
        "label": f"Client terms v{row.version}: {row.program_name}",
        "amount": _num(row.amount),
        "rate_pct": _num(row.apr_pct),
        "term_months": int(row.term_months) if row.term_months else None,
        "interest_only": (getattr(row, "loan_structure", None) or "amortizing") == "interest_only",
        "structure": getattr(row, "loan_structure", None) or "amortizing",
        "interest_only_months": int(getattr(row, "interest_only_months", None) or 0),
        "payment_monthly": round(payment * periods / 12.0, 2) if payment is not None else None,
        "treatment": str(getattr(row, "debt_service_treatment", None) or "additive"),
        "retained_annual_debt_service": _num(getattr(row, "retained_annual_debt_service", None)),
    }


def recorded_from_production_sheet(sheet: Any) -> dict[str, Any] | None:
    """The current production term sheet as the plan reads it."""
    if sheet is None:
        return None
    extra = sheet.extra if isinstance(getattr(sheet, "extra", None), dict) else {}
    structure = str(extra.get("repayment_structure") or "")
    io_months = int(_num(extra.get("interest_only_months")) or 0)
    term = int(sheet.term_months) if sheet.term_months else None
    fully_io = structure in {"interest_only", "revolving_interest_only"}
    return {
        "kind": "production_term_sheet",
        "id": str(sheet.id),
        "version": int(sheet.version),
        "label": f"Term sheet v{sheet.version}: {sheet.funding_party_name}",
        "amount": _num(sheet.approved_amount),
        "rate_pct": _num(sheet.rate_pct),
        "term_months": term,
        "interest_only": fully_io,
        "structure": "interest_only" if fully_io else "amortizing",
        "interest_only_months": (term or 0)
        if fully_io
        else (io_months if structure == "interest_only_then_amortizing" else 0),
        "payment_monthly": _num(sheet.monthly_debt_service),
        "treatment": str(extra.get("debt_service_treatment") or "additive"),
        "retained_annual_debt_service": _num(extra.get("retained_annual_debt_service")),
    }


def adopt(
    plan: dict[str, Any] | None, recorded: dict[str, Any], *, by: Any = None
) -> dict[str, Any]:
    """The plan with the recorded terms as its new loan. The sheet's own
    treatment decides the existing debt: additive keeps it; refinance sets it
    to what the sheet retains (nothing, when it retains nothing)."""
    out = dict(plan or {})
    out.update(
        {
            "new_payment_monthly": recorded.get("payment_monthly"),
            "new_payment_source": "recorded_terms",
            "new_loan_amount": recorded.get("amount"),
            "new_loan_rate_pct": recorded.get("rate_pct"),
            "new_loan_term_months": recorded.get("term_months"),
            "new_loan_interest_only": bool(recorded.get("interest_only")),
            "new_loan_structure": recorded.get("structure")
            or ("interest_only" if recorded.get("interest_only") else "amortizing"),
            "new_loan_interest_only_months": int(recorded.get("interest_only_months") or 0),
            "terms_ref": {
                "kind": recorded.get("kind"),
                "id": recorded.get("id"),
                "version": recorded.get("version"),
                "label": recorded.get("label"),
                "adopted_at": datetime.now(UTC).isoformat(),
            },
        }
    )
    overrides = dict(out.get("overrides") or {})
    if recorded.get("treatment") == "refinance":
        retained = _num(recorded.get("retained_annual_debt_service")) or 0.0
        overrides["debt_service_monthly"] = round(retained / 12.0, 2)
        overrides["debt_service_note"] = f"Refinance per {recorded.get('label')}: " + (
            f"{retained / 12.0:,.0f} a month of existing debt is retained."
            if retained
            else "the recorded terms replace every existing payment."
        )
        out["overrides"] = overrides
    elif (overrides.get("debt_service_note") or "").startswith("Refinance per "):
        # Terms re-recorded as additive: the earlier refinance override goes.
        overrides.pop("debt_service_monthly", None)
        overrides.pop("debt_service_note", None)
        out["overrides"] = overrides or None
    out["updated_at"] = datetime.now(UTC).isoformat()
    if by is not None:
        out["updated_by"] = str(by)
    return out


def release(plan: dict[str, Any] | None, recorded_id: str | None = None) -> dict[str, Any]:
    """The plan without the recorded terms it had adopted (if it had)."""
    out = dict(plan or {})
    ref = out.get("terms_ref") if isinstance(out.get("terms_ref"), dict) else None
    if ref is None or (recorded_id is not None and str(ref.get("id")) != str(recorded_id)):
        return out
    for key in (
        "new_payment_monthly",
        "new_loan_amount",
        "new_loan_rate_pct",
        "new_loan_term_months",
    ):
        out[key] = None
    out["new_payment_source"] = None
    out["new_loan_interest_only"] = False
    out["terms_ref"] = None
    overrides = dict(out.get("overrides") or {})
    if (overrides.get("debt_service_note") or "").startswith("Refinance per "):
        overrides.pop("debt_service_monthly", None)
        overrides.pop("debt_service_note", None)
        out["overrides"] = overrides or None
    out["updated_at"] = datetime.now(UTC).isoformat()
    return out


def terms_link(
    plan: dict[str, Any], evaluation: dict[str, Any], recorded: dict[str, Any] | None
) -> dict[str, Any]:
    """Does the plan's loan mirror the recorded terms? One of: none (nothing
    recorded, nothing planned), plan_only, recorded_not_in_plan, mirrored,
    differs (with the fields that differ)."""
    loan = evaluation.get("loan_terms") or {}
    payment = evaluation.get("new_payment_monthly")
    plan_loan = (
        {
            "amount": _num(loan.get("amount")),
            "rate_pct": _num(loan.get("rate_pct")),
            "term_months": loan.get("term_months"),
            "interest_only": bool(loan.get("interest_only")),
            "payment_monthly": _num(payment),
        }
        if loan or payment
        else None
    )
    ref = plan.get("terms_ref") if isinstance(plan.get("terms_ref"), dict) else None
    if recorded is None and plan_loan is None:
        state, differences = "none", []
    elif recorded is None:
        state, differences = "plan_only", []
    elif plan_loan is None:
        state, differences = "recorded_not_in_plan", []
    else:
        differences = []
        for key in ("amount", "rate_pct", "term_months", "interest_only", "payment_monthly"):
            a, b = plan_loan.get(key), recorded.get(key)
            if key == "interest_only":
                same = bool(a) == bool(b) and int(loan.get("interest_only_months") or 0) == int(
                    recorded.get("interest_only_months") or 0
                )
            elif a is None or b is None:
                same = a is None and b is None
            else:
                same = abs(float(a) - float(b)) <= (0.5 if key != "term_months" else 0)
            if not same:
                differences.append(DIFFERENCE_LABELS[key])
        state = "mirrored" if not differences else "differs"
    return {
        "state": state,
        "recorded": recorded,
        "plan_loan": plan_loan,
        "adopted_from": ref,
        "differences": differences,
        "summary": {
            "none": "No terms recorded and no loan in the plan.",
            "plan_only": "The plan models a loan that is not recorded on the Underwriting tab yet.",
            "recorded_not_in_plan": f"{recorded['label'] if recorded else 'The recorded terms'} are recorded but not in the plan.",
            "mirrored": f"The plan mirrors {recorded['label'] if recorded else 'the recorded terms'}.",
            "differs": f"The plan differs from {recorded['label'] if recorded else 'the recorded terms'}: "
            + ", ".join(differences)
            + ".",
        }[state],
    }


async def adopt_for_profile(
    db: Any, profile: ApplicationProfile, recorded: dict[str, Any], *, user: Any
) -> None:
    """Write the adopted terms onto the file's plan and say so in the log."""
    from app.services import application_profiles as profiles
    from app.services.financial_plan import normalize_plan

    profile.financial_plan = normalize_plan(
        adopt(profile.financial_plan or {}, recorded, by=getattr(user, "id", None))
    )
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "financial_plan.terms_adopted",
        f"The plan now carries {recorded.get('label')}",
        target_type="financial_plan",
        target_id=profile.id,
        metadata={
            "terms": {
                k: recorded.get(k)
                for k in (
                    "kind",
                    "id",
                    "version",
                    "amount",
                    "rate_pct",
                    "term_months",
                    "interest_only",
                    "payment_monthly",
                    "treatment",
                )
            }
        },
    )


async def release_for_profile(
    db: Any, profile: ApplicationProfile, recorded_id: str, *, user: Any
) -> None:
    from app.services import application_profiles as profiles
    from app.services.financial_plan import normalize_plan

    before = profile.financial_plan or {}
    after = release(before, recorded_id)
    if after == before:
        return
    profile.financial_plan = normalize_plan(after)
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "financial_plan.terms_released",
        "The withdrawn terms left the plan",
        target_type="financial_plan",
        target_id=profile.id,
    )
