"""One transaction for versioned client terms and the desk's unsaved scenario."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

from fastapi import HTTPException

from app.schemas.financial_cockpit import FinancialPlanRead
from app.services import application_profiles as profiles
from app.services import application_terms, financial_cockpit, financial_plan


async def save_combined_terms(db, profile, user, payload):
    """Build both response projections before the caller commits either write."""
    draft = payload.financial_plan
    if draft is None or "expected_updated_at" not in draft.model_fields_set:
        raise HTTPException(422, "Include the financial plan revision, or null for a new plan, before saving both views")
    with getattr(db, "no_autoflush", nullcontext()):
        await application_terms.lock_profile_terms(db, profile.id)
    await db.refresh(profile, attribute_names=["financial_plan"])
    current = await application_terms.current_term_sheet(db, profile.id)
    if (current.version if current is not None else 0) != payload.expected_version:
        raise HTTPException(409, "These terms changed in another session. Reload the file before saving a new version.")
    financial_plan.validate_plan_write(profile.financial_plan, draft)

    # Stage only the submitted scenario. save_terms uses this basis for DSCR,
    # then its existing adoption service replaces the new-loan fields with the
    # accepted terms while retaining payoff selections, exclusions and addbacks.
    record = draft.model_dump(exclude={"expected_updated_at", "expected_deal_budget_revision"})
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
    await profiles.log_profile_action(db, profile, user, "financial_plan.updated",
        "Saved the financial scenario with the new client terms version",
        metadata={"retire": draft.retire, "exclude": draft.exclude, "terms_version": row.version})
    result = await application_terms.read_terms(db, profile, row)
    cockpit = await financial_cockpit.load_cockpit(db, profile)
    result.financial_plan = FinancialPlanRead(profile_id=profile.id,
        obligations=cockpit["debt"]["obligations"], **cockpit["plan"])
    return result
