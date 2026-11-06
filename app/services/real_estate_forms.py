"""Read adapters for the real estate form's existing versioned workspace."""

from __future__ import annotations

from sqlalchemy import select

from app.models.bucket import BucketRequestedDocument
from app.models.financial_form_version import FinancialFormDraft, FinancialFormVersion
from app.services import financial_workspace, real_estate_schema

KIND = "real_estate_schedule"
LABEL = "Real estate schedule"


async def requested_slot(db, profile):
    if not profile.primary_bucket_id:
        return None
    slots = (await db.execute(select(BucketRequestedDocument).where(
        BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
    ))).scalars().all()
    for slot in slots:
        source = slot.requirement_source or {}
        config = source.get("collection_config") or {}
        if (slot.requirement_key == KIND or config.get("form_kind") == KIND
                or source.get("form_kind") == KIND
                or slot.category == "Real estate" and slot.name.casefold() in {LABEL.casefold(), "schedule of real estate owned"}):
            return slot
    return None


async def ensure_workspace(db, profile, *, actor=None):
    return await financial_workspace.load_workspace(db, profile, kind=KIND, actor=actor)


async def ensure_request(db, profile, *, actor):
    """Pin the existing library requirement so readiness keeps the form task."""
    from app.models.application_profile import ApplicationRequirementState
    from app.services import application_programs, requirement_library

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
        state.requested_document_id == previous.id and state.requirement_key != KIND
        and state.status != "not_applicable" and not (state.provenance or {}).get("cancelled_at")
        and not (state.provenance or {}).get("inactive_definition") for state in states
    ):
        # Bind a plain intake/legacy ask before readiness chooses a destination.
        # Its wording, files and original provenance remain on the same row.
        previous.requirement_key = KIND
    plan = await requirement_library.plan_add(db, profile, requirement_key=KIND, custom=None,
        required_level="required", note=None, user_id=actor.id, user_name=getattr(actor, "name", None), on_file={})
    if not plan.already:
        profile.extra_requirements = requirement_library.with_entry(profile, plan.entry)
    await application_programs.get_program_readiness(db, profile)
    materialized = (await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
        ApplicationRequirementState.requirement_key == KIND,
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

        raise HTTPException(409, "The real estate form request could not be prepared")
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
    facts = version.key_facts if version else {}
    published_form = version is not None and version.source != "original"
    return FinancialFormStatus(kind=KIND, label=LABEL, requested=slot is not None,
        satisfied=bool(slot and slot.status == "uploaded"),
        source="filled" if published_form else "uploaded" if version is not None or slot and slot.status == "uploaded" else "none",
        figures_from="form" if published_form else "document" if version else None,
        updated_at=version.created_at if version else draft.updated_at if draft else None,
        filled_by_staff=bool(version and version.actor_user_id),
        period_label=f"As of {facts['as_of']}" if facts.get("as_of") else None,
        **{key: facts.get(key) for key in ("property_count", "estimated_value_total", "mortgage_balance_total", "estimated_equity", "monthly_rent_total", "active_mortgage_count")})


def _display(value, *, money=False):
    if value in (None, ""):
        return "Not provided"
    if money:
        amount = real_estate_schema._amount(value)
        return f"${amount:,.2f}" if amount is not None else "Not provided"
    return str(value).replace("_", " ")


async def answers(db, profile):
    from app.services import form_answers

    _family, version, draft = await current(db, profile)
    if version is None and draft is None:
        return form_answers.empty_answers(KIND, "No real estate schedule has been started or uploaded yet."), None
    body = version.body if version else draft.body
    facts = real_estate_schema.key_facts(body)
    money_keys = {field["key"] for field in (*real_estate_schema.PROPERTY_FIELDS, *real_estate_schema.MORTGAGE_FIELDS) if field["input"] == "money"}
    property_names = {row["id"]: row.get("address") or "Property address not provided" for row in real_estate_schema.properties(body)}
    tables = []
    for block, fields in real_estate_schema.COLLECTIONS.items():
        rows = []
        for row in getattr(real_estate_schema, block)(body):
            rows.append([_display(property_names.get(row.get("property_row_id")), money=False) if field["key"] == "property_row_id"
                         else _display(row.get(field["key"]), money=field["key"] in money_keys) for field in fields])
        tables.append({"key": block, "title": block.capitalize(), "columns": [field["label"] for field in fields], "rows": rows})
    headline = [{"label": real_estate_schema.TOTAL_LABELS[key], "value": _display(facts.get(key), money=key != "property_count"),
                 "amount": float(facts[key]) if facts.get(key) is not None else None}
                for key in ("property_count", "estimated_value_total", "mortgage_balance_total", "estimated_equity")]
    return {"kind": KIND, "label": LABEL, "source": "filled" if not version or version.source != "original" else "uploaded",
            "figures_from": "form" if not version or version.source != "original" else "document", "draft": version is None,
            "who": body.get("owner_name"), "as_of": body.get("as_of"), "updated_at": version.created_at if version else draft.updated_at,
            "filled_by": version.actor_name if version else None, "headline": headline, "tables": tables,
            "notes": body.get("notes"), "files": []}, (version.pdf_file_id or version.source_file_id) if version else None
