"""One transaction for versioned client terms and the desk's unsaved scenario."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.financial_cockpit import FinancialPlanRead
from app.services import application_profiles as profiles
from app.services import (
    application_terms,
    deal_budget,
    financial_cockpit,
    financial_plan,
    investor_collateral,
)


async def _read_combined(db, profile, row):
    result = await application_terms.read_terms(db, profile, row)
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    result.financial_plan = FinancialPlanRead(profile_id=profile.id,
        obligations=cockpit["debt"]["obligations"], **cockpit["plan"])
    return result


async def save_combined_terms(db, profile, user, payload):
    """Build both response projections before the caller commits either write."""
    draft = payload.financial_plan
    if draft is None or "expected_updated_at" not in draft.model_fields_set:
        raise HTTPException(422, "Include the financial plan revision, or null for a new plan, before saving both views")
    with getattr(db, "no_autoflush", nullcontext()):
        await application_terms.lock_profile_terms(db, profile.id)
    atomic_budget = payload.deal_budget is not None
    original_key = payload.idempotency_key
    await db.refresh(profile, attribute_names=["financial_plan", "deal_budget", "investor_collateral"] if atomic_budget else ["financial_plan"])
    current = await application_terms.current_term_sheet(db, profile.id)
    fingerprint = None
    if atomic_budget:
        fingerprint = deal_budget.save_request_fingerprint("application_term_sheet", profile.id, payload)
        replay = deal_budget.find_terms_save_receipt(profile, "application_term_sheet", payload.idempotency_key, fingerprint,
            user, current.id if current else None, current.version if current else 0)
        if replay is not None:
            return await _read_combined(db, profile, current)
    if (current.version if current is not None else 0) != payload.expected_version:
        raise HTTPException(409, "These terms changed in another session. Reload the file before saving a new version.")
    financial_plan.validate_plan_write(profile.financial_plan, draft)

    before_budget = None
    if atomic_budget:
        before_budget = deal_budget.snapshot(profile)
        if payload.expected_collateral_revision != investor_collateral.snapshot(profile)["revision"]:
            raise HTTPException(409, "Collateral changed in another session. Reload before saving the budget and terms.")
        if draft.expected_deal_budget_revision is not None and draft.expected_deal_budget_revision != payload.deal_budget.expected_revision:
            raise HTTPException(409, "The financial plan and draft budget refer to different cost revisions. Reload before saving.")
        terms = {"amount": payload.amount, "rate_pct": payload.apr_pct, "term_months": payload.term_months,
            "structure": payload.loan_structure, "interest_only_months": payload.interest_only_months,
            "payments_per_year": payload.custom_payments_per_year if payload.repayment_frequency == "custom" else application_terms.CADENCE_PERIODS_PER_YEAR[payload.repayment_frequency],
            "repayment_config": payload.repayment_config.model_dump(mode="json") if payload.repayment_config is not None else None}
        staged_budget, solution = await deal_budget.stage_for_terms(db, profile, payload.deal_budget, terms, user)
        terms["amount"] = solution["gross_loan_amount"]
        data = payload.model_dump(mode="python")
        plan_data = deal_budget._loan_plan(draft.model_dump(mode="python"), terms)
        plan_data["expected_deal_budget_revision"] = staged_budget["revision"]
        # Revalidate the flexible schedule against canonical G before any
        # schedule, DSCR, or immutable version is generated.
        data.update(amount=solution["gross_loan_amount"], financial_plan=plan_data,
            expected_deal_budget_revision=staged_budget["revision"], deal_budget=None, idempotency_key=None)
        try:
            payload = type(payload).model_validate(data)
        except ValidationError as error:
            raise HTTPException(422,
                f"The calculated gross loan of ${solution['gross_loan_amount']:,.2f} does not satisfy the loan amount or repayment schedule limits. Review the net target, financed costs, and repayment terms.") from error
        draft = payload.financial_plan

    # Stage only the submitted scenario. save_terms uses this basis for DSCR,
    # then its existing adoption service replaces the new-loan fields with the
    # accepted terms while retaining payoff selections, exclusions and addbacks.
    record = draft.model_dump(exclude={"expected_updated_at", "expected_deal_budget_revision"})
    from app.services.loan_dates import preserve_plan_dates
    record = preserve_plan_dates(record, profile.financial_plan, draft.model_fields_set)
    overrides = dict(record.get("overrides") or {})
    if payload.debt_service_treatment != "refinance" and (overrides.get("debt_service_note") or "").startswith("Refinance per "):
        # Adoption clears this obsolete automatic basis too. Clear it before
        # save_terms snapshots DSCR so the immutable terms and returned plan
        # use the same scenario. Staff-authored and income overrides survive.
        overrides.pop("debt_service_monthly", None)
        overrides.pop("debt_service_note", None)
        record["overrides"] = overrides or None
    record.update(updated_at=datetime.now(UTC).isoformat(), updated_by=str(user.id))
    profile.financial_plan = record
    # save_terms refreshes the plan under the same lock. Persist the guarded
    # draft to this transaction first, without publishing either resource.
    await db.flush()
    row = await application_terms.save_terms(db, profile, user, payload)
    if atomic_budget:
        after_budget = deal_budget.snapshot(profile)
        if after_budget["revision"] != before_budget["revision"]:
            await profiles.log_profile_action(db, profile, user, "deal_budget.update", "Saved draft deal costs with the new client terms version",
                metadata={"before": before_budget, "after": after_budget, "terms_version": row.version})
        # The receipt is projection metadata, not a cost revision or a new
        # contractual fact. Capture the final adopted plan timestamp.
        deal_budget.record_terms_save_receipt(profile, "application_term_sheet", original_key, fingerprint,
            user, row.id, row.version)
        await db.flush()
    await profiles.log_profile_action(db, profile, user, "financial_plan.updated",
        "Saved the financial scenario with the new client terms version",
        metadata={"retire": draft.retire, "exclude": draft.exclude, "terms_version": row.version})
    return await _read_combined(db, profile, row)
