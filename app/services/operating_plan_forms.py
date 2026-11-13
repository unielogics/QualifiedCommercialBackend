"""Request adapters over the operating plan's shared versioned workspace."""

from __future__ import annotations

from sqlalchemy import select

from app.models.bucket import BucketRequestedDocument
from app.models.financial_form_version import FinancialFormDraft, FinancialFormVersion
from app.services import financial_workspace, operating_plan_schema

KIND = "operating_plan"
REQUIREMENT_KEY = "operating_expenses_and_earnings_plan"
LABEL = "Operating expenses and earnings plan"


async def requested_slot(db, profile):
    if not profile.primary_bucket_id:
        return None
    slots = (await db.execute(select(BucketRequestedDocument).where(
        BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
    ))).scalars().all()
    for slot in slots:
        source = slot.requirement_source or {}
        config = source.get("collection_config") or {}
        if (slot.requirement_key == REQUIREMENT_KEY or config.get("form_kind") == KIND
                or source.get("form_kind") == KIND):
            return slot
    return None


async def ensure_workspace(db, profile, *, actor=None):
    return await financial_workspace.load_workspace(db, profile, kind=KIND, actor=actor)


async def ensure_request(db, profile, *, actor=None):
    """Pin the existing library requirement so readiness keeps the form task."""
    from app.models.application_profile import ApplicationRequirementState
    from app.services import application_programs, requirement_library
    from app.services.operating_plan_goals import require_goal

    await require_goal(db, profile)
    await financial_workspace.lock_financial_profile(db, profile)
    previous = await requested_slot(db, profile)
    states = (await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
    ))).scalars().all()
    for state in states:
        metadata = state.provenance or {}
        if (state.status == "not_applicable" or metadata.get("cancelled_at")
                or metadata.get("inactive_definition")
                or metadata.get("requirement_kind") != "form"
                or (metadata.get("collection_config") or {}).get("form_kind") != KIND
                or not state.requested_document_id):
            continue
        # An older plain upload ask can precede an active batch form in the
        # room. Reuse that form's own destination rather than add a second ask.
        owned = previous if previous is not None and previous.id == state.requested_document_id else await db.get(
            BucketRequestedDocument, state.requested_document_id)
        if (owned is not None and owned.bucket_id == profile.primary_bucket_id
                and owned.status not in {"closed", "not_applicable"}):
            return owned, True
    if previous is not None and not any(
        state.requested_document_id == previous.id and state.requirement_key != REQUIREMENT_KEY
        and state.status != "not_applicable" and not (state.provenance or {}).get("cancelled_at")
        and not (state.provenance or {}).get("inactive_definition") for state in states
    ):
        # Bind a plain intake/legacy ask before readiness chooses a destination.
        # Its wording, files and original provenance remain on the same row.
        previous.requirement_key = REQUIREMENT_KEY
    plan = await requirement_library.plan_add(db, profile, requirement_key=REQUIREMENT_KEY, custom=None,
        required_level="required", note=None, user_id=getattr(actor, "id", None), user_name=getattr(actor, "name", None), on_file={})
    if not plan.already:
        profile.extra_requirements = requirement_library.with_entry(profile, plan.entry)
    await application_programs.get_program_readiness(db, profile)
    materialized = (await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
        ApplicationRequirementState.requirement_key == REQUIREMENT_KEY,
    ))).scalars().all()
    slot = None
    for state in materialized:
        metadata = state.provenance or {}
        if (state.status == "not_applicable" or metadata.get("cancelled_at") or metadata.get("inactive_definition")
                or metadata.get("requirement_kind") != "form"
                or (metadata.get("collection_config") or {}).get("form_kind") != KIND):
            continue
        owned = await db.get(BucketRequestedDocument, state.requested_document_id) if state.requested_document_id else None
        if owned is not None and owned.bucket_id == profile.primary_bucket_id and owned.status not in {"closed", "not_applicable"}:
            slot = owned
            break
    if slot is None:
        from fastapi import HTTPException

        raise HTTPException(409, "The operating expenses and earnings plan request could not be prepared")
    return slot, bool(plan.already)


async def current(db, profile):
    family = await financial_workspace.existing_family(db, profile.id, KIND)
    if family is None:
        return None, None, None
    version = await db.get(FinancialFormVersion, family.current_version_id) if family.current_version_id else None
    draft = await db.get(FinancialFormDraft, family.id)
    return family, version, draft


async def status(db, profile):
    from app.schemas.application_profile import FinancialFormStatus

    slot = await requested_slot(db, profile)
    _family, version, draft = await current(db, profile)
    allocation_complete = True
    cash = (version.body.get("released_cash_flow") or {}) if version else {}
    saving_actions = any(not row.get("excluded") and row.get("status") != "rejected" and row.get("kind") in {"expense_reduction", "expense_group_change", "product_cost_change", "processing_cost_change"}
                         for row in version.body.get("adjustments", [])) if version else False
    if version and ((getattr(profile, "financial_plan", None) or {}).get("updated_at") or cash.get("source_fingerprint") or cash.get("allocations") or saving_actions):
        from app.services.earnings_improvement import evaluate
        from app.services.financial_cockpit import load_cockpit

        allocation_complete = evaluate(version.body, await load_cockpit(db, profile))["released_cash_flow"]["complete"]
    return FinancialFormStatus(kind=KIND, label=LABEL, requested=slot is not None,
        satisfied=bool(slot and slot.status == "uploaded" and allocation_complete),
        source="filled" if version else "none", figures_from="form" if version else None,
        updated_at=version.created_at if version else draft.updated_at if draft else None,
        filled_by_staff=bool(version and version.actor_user_id))


async def answers(db, profile):
    from app.services import form_answers

    _family, version, draft = await current(db, profile)
    if version is None and draft is None:
        return form_answers.empty_answers(KIND, "No operating expense disclosure or earnings plan has been started."), None
    body = version.body if version else draft.body
    def display(value):
        return "Not provided" if value in (None, "") else str(value).replace("_", " ")
    tables = [{"key": block, "title": "Operating expenses" if block == "expenses" else "Proposed adjustments",
        "columns": [spec["label"] for spec in specs],
        "rows": [[display(row.get(spec["key"])) for spec in specs] for row in body.get(block) or [] if not row.get("excluded")]}
        for block, specs in operating_plan_schema.COLLECTIONS.items()]
    return {"kind": KIND, "label": LABEL, "source": "filled", "figures_from": "form", "draft": version is None,
        "who": body.get("business_name"), "as_of": body.get("period_end"), "updated_at": version.created_at if version else draft.updated_at,
        "filled_by": version.actor_name if version else None, "headline": [], "tables": tables,
        "notes": body.get("notes"), "files": []}, version.pdf_file_id if version else None
