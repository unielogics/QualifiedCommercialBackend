"""First-class staff document definitions over the existing collection engine.

The requested document is the upload destination, requirement state is the
coverage authority, and collection task is its borrower/account projection.
The request UUID groups definitions and is also the initial email idempotency
key. No parallel checklist, reminder queue, or duplicate loan Document is made.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationProfile,
    ApplicationRequirementState,
    ApplicationRoomDelivery,
)
from app.models.bucket import BucketRequestedDocument
from app.schemas.file_request import FileRequestItem
from app.services import application_profiles as profiles
from app.services import collection_tasks, missing_item_automation

DOCUMENT_CLASSES = {
    "bank_statement": ["bank_statement"],
    "tax_return": ["tax_return"],
    "debt_schedule": ["debt_schedule"],
    "real_estate_schedule": ["real_estate_schedule"],
    "operating_plan": ["operating_plan"],
    "pfs": ["personal_financial_statement"],
    "personal_financial_statement": ["personal_financial_statement"],
    "profit_and_loss": ["current_p_and_l"],
    "p_and_l": ["current_p_and_l"],
    "balance_sheet": ["balance_sheet"],
    "payoff_letter": ["payoff_or_mortgage_statement", "collateral_debt_evidence"],
    "mortgage_statement": ["payoff_or_mortgage_statement"],
    "loan_agreement": ["collateral_debt_evidence"],
}
REQUEST_SOURCE_KINDS = {"staff_request", "financial_form_request"}


def item_config(item: FileRequestItem, request_id: UUID) -> dict:
    return {
        "file_request_id": str(request_id),
        "document_type": item.document_type,
        "quantity": item.quantity,
        "unit": item.unit,
        "subject": item.subject,
        "period": item.period,
        "minimum_count": item.quantity if item.unit == "files" else 1,
        **({f"required_{item.unit}": item.quantity} if item.unit != "files" else {}),
        "evidence_classes": DOCUMENT_CLASSES.get(item.document_type, ["other"]),
        # Two requests can concern different people/periods but the same type.
        # Explicit upload/association selects which obligation a file satisfies.
        "explicit_assignment_only": True,
        "automation_approved": True,
        **({"form_kind": item.document_type} if _online_form(item) else {}),
    }


def _online_form(item: FileRequestItem) -> bool:
    return item.document_type in {"real_estate_schedule", "operating_plan"} and item.quantity == 1 and item.unit == "files"



def _same_operating_request(state, item):
    if item.document_type != "operating_plan" or not _online_form(item):
        return False
    metadata = state.provenance or {}
    config = metadata.get("collection_config") or {}
    if (state.status == "not_applicable" or metadata.get("inactive_definition")
            or metadata.get("cancelled_at") or metadata.get("redirect_requirement_key")):
        return False
    if state.requirement_key != "operating_expenses_and_earnings_plan" and config.get("form_kind") != "operating_plan":
        return False
    def normalize(value):
        return " ".join(str(value).casefold().split())
    return (normalize(config.get("subject") or "business") == normalize(item.subject)
            and normalize(config.get("period") or "current") == normalize(item.period))


def request_label(item: FileRequestItem) -> str:
    amount = f"{item.quantity} {item.unit if item.quantity != 1 else item.unit.rstrip('s')}"
    details = [value for value in (item.subject, item.period) if value not in {"business", "current"}]
    return (f"{item.name} · {amount}" + (" · " + " · ".join(details) if details else ""))[:180]


def batch_fingerprint(items: list[FileRequestItem]) -> str:
    return hashlib.sha256(json.dumps([item.model_dump() for item in items], sort_keys=True).encode()).hexdigest()


async def lock_profile(db: AsyncSession, profile: ApplicationProfile) -> None:
    from app.services.financial_workspace import lock_financial_profile

    await lock_financial_profile(db, profile)


async def request_states(db: AsyncSession, profile_id: UUID, request_id: UUID | None = None) -> list[ApplicationRequirementState]:
    states = list((await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile_id,
    ).order_by(ApplicationRequirementState.created_at, ApplicationRequirementState.requirement_key))).scalars().all())
    return [state for state in states if ((state.provenance or {}).get("source_kind") in REQUEST_SOURCE_KINDS or (state.provenance or {}).get("request_batches"))
            and not (state.provenance or {}).get("redirect_requirement_key")
            and (request_id is None or str(request_id) in batch_memberships(state))]


def batch_memberships(state: ApplicationRequirementState) -> dict:
    metadata = state.provenance or {}
    members = deepcopy(metadata.get("request_batches") or {})
    original = metadata.get("file_request_id")
    if original and original not in members:
        members[original] = {"fingerprint": metadata.get("request_fingerprint"), "item": metadata.get("request_item") or {},
                             "cancelled_at": metadata.get("cancelled_at"),
                             "created_at": state.created_at.isoformat() if state.created_at else None}
    return members


def request_identity(item: FileRequestItem) -> str:
    values = [item.document_type.casefold(), item.unit, " ".join(item.subject.casefold().split()), " ".join(item.period.casefold().split())]
    if item.document_type not in DOCUMENT_CLASSES:
        values.append(" ".join(item.name.casefold().split()))
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


async def apply_memberships(db, state, memberships: dict) -> None:
    metadata = deepcopy(state.provenance or {})
    active = [FileRequestItem(**member["item"]) for member in memberships.values() if not member.get("cancelled_at")]
    effective = active[-1].model_copy(update={"quantity": max(item.quantity for item in active)})
    config = item_config(effective, UUID(metadata["file_request_id"]))
    if effective.document_type == "debt_schedule":
        from app.services.requirement_lifecycle import normalize_subject, profile_subjects
        profile = await db.get(ApplicationProfile, state.profile_id)
        if profile:
            config = normalize_subject(config, await profile_subjects(db, profile))
    if metadata.get("source_context"):
        config["source_context"] = deepcopy(metadata["source_context"])
    label = request_label(effective)
    request_message = effective.instructions or f"Please provide {label}."
    definition = deepcopy(metadata["external_definition"])
    definition.update(collection_config=config, label=label, objective_text=effective.instructions,
                      ai_request_message_template=request_message,
                      requirement_kind="form" if _online_form(effective) else "document")
    metadata.update(request_batches=memberships, request_item=effective.model_dump(), collection_config=config,
                    external_definition=definition, request_instructions=effective.instructions,
                    objective_text=effective.instructions, request_message=request_message,
                    requirement_kind=definition["requirement_kind"])
    state.provenance = metadata
    state.label = label
    requested = await db.get(BucketRequestedDocument, state.requested_document_id)
    if requested:
        requested.name, requested.description = label, effective.instructions or None
        requested.requirement_source = {**(requested.requirement_source or {}), "collection_config": config}


async def create_batch(
    db: AsyncSession, *, profile: ApplicationProfile, request_id: UUID,
    items: list[FileRequestItem], user=None, canonical_keys: list[str] | None = None,
    source_kind: str = "staff_request", source_context: dict | None = None,
    source_contexts: list[dict] | None = None,
) -> list[ApplicationRequirementState]:
    # These options belong to trusted server workflows, not the public schema.
    if source_kind not in REQUEST_SOURCE_KINDS:
        raise ValueError("Unsupported file request source")
    if canonical_keys is not None and (
        len(canonical_keys) != len(items)
        or any(not key or len(key) > 200 for key in canonical_keys)
    ):
        raise ValueError("A valid canonical identity is required for each file request")
    if source_contexts is not None and len(source_contexts) != len(items):
        raise ValueError("A source context is required for each file request")
    if profile.primary_bucket_id is None:
        raise HTTPException(409, "This file has no document room")
    await lock_profile(db, profile)
    fingerprint = batch_fingerprint(items)
    existing = await request_states(db, profile.id, request_id)
    if existing:
        if any(batch_memberships(state).get(str(request_id), {}).get("fingerprint") != fingerprint for state in existing):
            raise HTTPException(409, "This request ID was already used for different items")
        return existing
    identities = canonical_keys or [request_identity(item) for item in items]
    if len(set(identities)) != len(identities):
        raise HTTPException(422, "Combine repeated document requests into one quantity")
    # Respect the same authorized hosted-collection boundary as program tasks.
    from app.services.arf_channel import profile_uses_arf, require_hosted_permission
    if await profile_uses_arf(db, profile):
        await require_hosted_permission(db, requested_documents=[item.document_type for item in items])
    states = []
    candidates = await request_states(db, profile.id)
    debt_states = list((await db.execute(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id))).scalars().all()) if any(item.document_type in {"debt_schedule", "operating_plan"} for item in items) else []
    namespace = uuid5(profile.id, str(request_id))
    for index, item in enumerate(items):
        context = source_contexts[index] if source_contexts is not None else source_context
        key = f"staff_request_{request_id.hex}_{index}"
        semantic_key = canonical_keys[index] if canonical_keys is not None else f"staff_file_{request_identity(item)[:40]}"
        if item.document_type == "debt_schedule":
            from app.services.requirement_lifecycle import (
                debt_identity,
                normalize_subject,
                profile_subjects,
                state_key,
            )
            semantic_key = debt_identity("debt_schedule", normalize_subject(item_config(item, request_id), await profile_subjects(db, profile)))
            candidates = list({state.id: state for state in [*candidates, *debt_states]}.values())
        if item.document_type == "operating_plan" and _online_form(item):
            candidates = list({state.id: state for state in [*candidates, *debt_states]}.values())
        previous = next((state for state in candidates
                         if not (state.provenance or {}).get("cancelled_at")
                         and not (state.provenance or {}).get("redirect_requirement_key")
                         and ((state.provenance or {}).get("canonical_key") == semantic_key
                              or item.document_type == "debt_schedule" and state_key(state) == semantic_key
                              or _same_operating_request(state, item))), None)
        membership = {"fingerprint": fingerprint, "item": item.model_dump(), "created_at": datetime.now(UTC).isoformat(), "cancelled_at": None}
        if previous is not None:
            if item.document_type in {"debt_schedule", "operating_plan"}:
                metadata = deepcopy(previous.provenance or {})
                metadata.setdefault("file_request_id", str(request_id))
                metadata.setdefault("source_kind", source_kind)
                metadata.setdefault("source_key", str(request_id))
                metadata.setdefault("catalog_approved", True)
                metadata.setdefault("external_definition", {"requirement_key": previous.requirement_key,
                    "label": previous.label, "category": previous.category, "required_level": previous.required_level,
                    "requirement_kind": "form" if _online_form(item) else "document", "visibility": ["borrower", "underwriter"],
                    "completion_mode": "requires_human_verify" if previous.verification_required else "ai_can_complete",
                    "can_underwriter_waive": True})
                previous.provenance = metadata
            memberships = batch_memberships(previous)
            memberships[str(request_id)] = membership
            await apply_memberships(db, previous, memberships)
            states.append(previous)
            continue
        config = item_config(item, request_id)
        if item.document_type == "debt_schedule":
            config = normalize_subject(config, await profile_subjects(db, profile))
        if context:
            config["source_context"] = deepcopy(context)
        label = request_label(item)
        custom = item.document_type not in DOCUMENT_CLASSES
        # Free-text reporting windows are preserved as client instructions.
        # Their exact meaning needs staff review before claiming fulfillment.
        requires_review = custom or item.document_type == "operating_plan" or item.period.strip().casefold() not in {"current", "any", "any period"}
        requirement_kind = "form" if _online_form(item) else "document"
        definition = {
            "requirement_key": key, "canonical_key": semantic_key,
            "label": label, "category": "Other", "required_level": "required",
            "requirement_kind": requirement_kind, "collection_config": config,
            "visibility": ["borrower", "underwriter"],
            "verification_required": requires_review,
            "completion_mode": "requires_human_verify" if requires_review else "ai_can_complete",
            "objective_text": item.instructions,
            "completion_criteria": "Provide the requested document type, subject, period and quantity.",
            "ai_request_message_template": item.instructions or f"Please provide {label}.",
            "can_underwriter_waive": True,
        }
        requested = BucketRequestedDocument(
            id=uuid5(namespace, f"document:{index}"), bucket_id=profile.primary_bucket_id,
            name=label, category="Other", description=item.instructions or None,
            required=True, allow_multiple_files=True, status="requested", is_custom=True,
            requirement_key=key,
            requirement_source={"kind": source_kind, "file_request_id": str(request_id), "collection_config": config},
        )
        db.add(requested)
        state = ApplicationRequirementState(
            id=uuid5(namespace, f"requirement:{index}"), profile_id=profile.id,
            requirement_key=key, label=label, category="Other", required_level="required",
            status="requested", requested_document_id=requested.id,
            verification_required=requires_review, source_program_keys=[], source_policy_keys=[],
            first_requested_at=datetime.now(UTC),
            provenance={
                "source_kind": source_kind, "source_key": str(request_id),
                "file_request_id": str(request_id), "request_fingerprint": fingerprint,
                "request_batches": {str(request_id): membership},
                "request_item": item.model_dump(), "request_instructions": item.instructions,
                "requested_by_user_id": str(user.id) if user is not None else None,
                **({"source_context": deepcopy(context)} if context else {}),
                "requirement_kind": requirement_kind, "canonical_key": semantic_key,
                "collection_config": config, "catalog_approved": True, "client_visible": True,
                "definition_id": key, "definition_version": "1", "external_definition": definition,
                "objective_text": item.instructions, "request_message": definition["ai_request_message_template"],
            },
        )
        db.add(state)
        states.append(state)
    await db.flush()
    # Include other states so syncing does not cancel unrelated borrower tasks.
    all_states = list((await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
    ))).scalars().all())
    tasks = await collection_tasks.sync_collection_tasks(db, profile, all_states)
    for task in tasks:
        if set(task.requirement_keys).intersection(state.requirement_key for state in states):
            task.next_attempt_at = datetime.now(UTC) + timedelta(hours=24)
    await profiles.log_profile_action(db, profile, user, "files.requested",
        f"Requested {len(states)} document item(s)", target_type="file_request", target_id=request_id,
        metadata={"requirement_keys": [state.requirement_key for state in states]})
    return states


async def deliver_batch(db: AsyncSession, *, profile: ApplicationProfile, request_id: UUID, user, retry_id: UUID | None = None) -> ApplicationRoomDelivery:
    await lock_profile(db, profile)
    states = await request_states(db, profile.id, request_id)
    if not states:
        raise HTTPException(404, "File request not found")
    states = [state for state in states if not (state.provenance or {}).get("cancelled_at")
              and not batch_memberships(state).get(str(request_id), {}).get("cancelled_at")]
    if not states:
        raise HTTPException(409, "This file request was cancelled")
    delivery_key = f"files:{profile.id}:{request_id}:" + (f"retry:{retry_id}" if retry_id else "initial")
    existing = (await db.execute(select(ApplicationRoomDelivery).where(
        ApplicationRoomDelivery.idempotency_key == delivery_key,
    ))).scalar_one_or_none()
    if existing:
        return existing
    if retry_id:
        deliveries = await batch_deliveries(db, profile.id, request_id)
        if not deliveries or deliveries[-1].status != "failed":
            if deliveries:
                return deliveries[-1]
            raise HTTPException(409, "There is no failed notification to retry")
    try:
        delivery = await missing_item_automation.send_requirements_email(
            db, profile=profile, requirement_keys=[state.requirement_key for state in states], user=user,
            initiation_source="staff_file_request", delivery_key=delivery_key,
            file_request_id=str(request_id),
        )
    except Exception as exc:
        # Creation was committed by the caller. A provider/preflight failure
        # records a retryable receipt and never removes the upload destinations.
        detail = str(exc.detail) if isinstance(exc, HTTPException) else "Email delivery failed; retry the notification"
        delivery = ApplicationRoomDelivery(
            id=uuid4(), profile_id=profile.id, bucket_id=profile.primary_bucket_id,
            requested_document_id=states[0].requested_document_id,
            action_kind="missing_item_email", channel="email", status="failed", detail=detail,
            initiation_source="staff_file_request", idempotency_key=delivery_key,
            created_by_user_id=user.id, attempt_number=1,
            provider_result={"file_request_id": str(request_id), "requirement_keys": [state.requirement_key for state in states]},
        )
        db.add(delivery)
    await collection_tasks.record_outreach(db, profile.id, [state.requirement_key for state in states], successful=delivery.status == "sent")
    profile.missing_item_email_last_sent_at = datetime.now(UTC)
    profile.missing_item_email_next_send_at = datetime.now(UTC) + timedelta(hours=24)
    await db.flush()
    return delivery


async def batch_deliveries(db: AsyncSession, profile_id: UUID, request_id: UUID) -> list[ApplicationRoomDelivery]:
    rows = list((await db.execute(select(ApplicationRoomDelivery).where(
        ApplicationRoomDelivery.profile_id == profile_id,
    ).order_by(ApplicationRoomDelivery.created_at, ApplicationRoomDelivery.id))).scalars().all())
    return [row for row in rows if (row.provider_result or {}).get("file_request_id") == str(request_id)]


async def cancel_batch(db: AsyncSession, *, profile: ApplicationProfile, request_id: UUID, user) -> None:
    await lock_profile(db, profile)
    states = await request_states(db, profile.id, request_id)
    if not states:
        raise HTTPException(404, "File request not found")
    for state in states:
        memberships = batch_memberships(state)
        memberships[str(request_id)] = {**memberships[str(request_id)], "cancelled_at": datetime.now(UTC).isoformat()}
        if any(not member.get("cancelled_at") for member in memberships.values()):
            await apply_memberships(db, state, memberships)
            continue
        from app.services.requirement_lifecycle import derived_required
        if derived_required(state):
            state.provenance = {**(state.provenance or {}), "request_batches": memberships}
            continue  # Cancelling this staff ask never cancels the policy's shared collection.
        state.status = "not_applicable"
        state.state_reason = "Staff cancelled this file request"
        state.provenance = {**(state.provenance or {}), "request_batches": memberships, "cancelled_at": datetime.now(UTC).isoformat(), "cancelled_by_user_id": str(user.id), "inactive_definition": True}
        requested = await db.get(BucketRequestedDocument, state.requested_document_id)
        if requested:
            requested.status = "not_applicable"
    keys = {state.requirement_key for state in states if (state.provenance or {}).get("cancelled_at")}
    tasks = list((await db.execute(select(ApplicationCollectionTask).where(ApplicationCollectionTask.profile_id == profile.id))).scalars().all())
    for task in tasks:
        if set(task.requirement_keys or []).intersection(keys):
            task.status, task.next_attempt_at = "cancelled", None
    await profiles.log_profile_action(db, profile, user, "files.request_cancelled", "Cancelled document request", target_type="file_request", target_id=request_id)
    await db.flush()


def delivery_read(row: ApplicationRoomDelivery) -> dict:
    return {"id": str(row.id), "status": row.status, "detail": row.detail,
            "channel": row.channel, "created_at": row.created_at,
            "recipient_email": row.recipient_email}


async def list_batches(db: AsyncSession, profile: ApplicationProfile, *, refresh: bool = True) -> dict:
    if refresh:
        from app.services.application_programs import get_program_readiness
        await get_program_readiness(db, profile)
    states = await request_states(db, profile.id)
    tasks = list((await db.execute(select(ApplicationCollectionTask).where(ApplicationCollectionTask.profile_id == profile.id))).scalars().all())
    task_by_key = {key: task for task in tasks for key in task.requirement_keys or []}
    deliveries = list((await db.execute(select(ApplicationRoomDelivery).where(ApplicationRoomDelivery.profile_id == profile.id).order_by(ApplicationRoomDelivery.created_at))).scalars().all())
    grouped = {}
    for state in states:
        metadata = state.provenance or {}
        task = task_by_key.get(state.requirement_key)
        for request_id, member in batch_memberships(state).items():
            batch = grouped.setdefault(request_id, {"request_id": request_id, "created_at": member.get("created_at") or state.created_at, "items": [], "deliveries": []})
            batch["items"].append({**metadata.get("request_item", {}), "label": state.label,
                "requirement_key": state.requirement_key, "requested_document_id": str(state.requested_document_id),
                "status": "cancelled" if member.get("cancelled_at") or metadata.get("cancelled_at") else state.status,
                "coverage": metadata.get("coverage") or {}, "verified_coverage": metadata.get("verified_coverage") or {},
                "next_reminder_at": task.next_attempt_at if task else None,
                "task_id": str(task.id) if task else None, "task_status": task.status if task else None})
    for delivery in deliveries:
        batch = grouped.get((delivery.provider_result or {}).get("file_request_id"))
        if batch is not None:
            batch["deliveries"].append(delivery_read(delivery))
    return {"requests": list(grouped.values())}


async def loan_todo_items(db: AsyncSession, profile: ApplicationProfile, *, want_pending: bool, want_completed: bool) -> list[dict]:
    """Canonical account To-Do projection; no copied loan Document rows."""
    from app.dealer_os.services.client_room import active_link
    from app.services.application_programs import get_program_readiness
    readiness = await get_program_readiness(db, profile)
    link = await active_link(db, profile.primary_bucket_id) if profile.primary_bucket_id else None
    return [
        {"id": f"file-request:{row.requirement_key}", "kind": "document", "title": row.label,
         "subtitle": row.state_reason if link else "Contact your team to reopen the secure upload room.", "status": row.status, "due_at": None,
         "deeplink": (f"/buckets/request/{link.token}?tab=todo&request={row.requested_document_id}"
                      if link else None)}
        for row in readiness.requirements
        if row.client_visible and (row.provenance.get("source_kind") in REQUEST_SOURCE_KINDS or row.provenance.get("request_batches"))
        and not row.provenance.get("cancelled_at")
        and ((row.status in {"verified", "waived", "not_applicable"}) and want_completed
             or row.status not in {"verified", "waived", "not_applicable"} and want_pending)
    ]
