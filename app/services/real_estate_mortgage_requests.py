"""Loan-specific statement collection from explicitly saved real-estate forms.

The saved schedule declares which loans need evidence. It does not certify a
statement, derive business debt, or send email. Existing request destinations,
evidence decisions, collection tasks and configured reminders remain authoritative.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationCollectionTask, ApplicationRequirementState
from app.models.bucket import BucketRequestedDocument
from app.schemas.file_request import FileRequestItem
from app.services import collection_tasks, file_requests, real_estate_schema

SOURCE_KIND = "financial_form_request"
FORM_KIND = "real_estate_schedule"


def _scope(family_id: UUID, property_id: str, mortgage_id: str | None) -> dict:
    return {"kind": FORM_KIND, "family_id": str(family_id), "property_row_id": property_id,
            "mortgage_row_id": mortgage_id}


def _identity(context: dict) -> tuple[str, str | None]:
    return context["property_row_id"], context.get("mortgage_row_id")


def _canonical(context: dict) -> str:
    return f"real_estate_mortgage:{context['family_id']}:{context['property_row_id']}:{context.get('mortgage_row_id') or 'unspecified'}"


def statement_requests(body: dict, family_id: UUID) -> list[tuple[dict, FileRequestItem]]:
    """Use stable row identities, never lender/address equality, to separate loans."""
    mortgages_by_property: dict[str, list[dict]] = {}
    for row in body.get("mortgages", []):
        # Worksheets retain blank/unfinished lines in their working drafts.
        # Publication separately rejects a meaningful loan with no property;
        # this collector must never invent an association or crash on padding.
        if row.get("property_row_id"):
            mortgages_by_property.setdefault(str(row["property_row_id"]), []).append(row)
    result = []
    for property_row in body.get("properties", []):
        property_id = str(property_row["id"])
        loans = [row for row in mortgages_by_property.get(property_id, [])
                 if real_estate_schema.mortgage_requires_statement(property_row, row)]
        # An active property without identified loan detail still needs evidence.
        if not loans and real_estate_schema.mortgage_requires_statement(property_row):
            loans = [None]
        loans.sort(key=lambda row: str(row["id"]) if row else "")
        for ordinal, loan in enumerate(loans, 1):
            context = _scope(family_id, property_id, str(loan["id"]) if loan else None)
            from app.services.real_estate_evidence import identity_fingerprint

            context["identity_hash"] = identity_fingerprint(property_row, loan)
            address = ", ".join(str(property_row.get(key) or "").strip()
                                for key in ("address", "city", "state", "postal_code")
                                if str(property_row.get(key) or "").strip()) or "Property address not provided"
            lender = str((loan or {}).get("lender") or "Lender not provided").strip()
            reference = str((loan or {}).get("loan_reference") or "").strip()
            name = f"Latest mortgage statement — {address[:60]} — {lender[:40]}"
            if reference:
                name += f" — Loan {reference[:20]}"
            elif len(loans) > 1:
                name += f" — Mortgage {ordinal}"
            instructions = f"Upload the latest mortgage statement for {address}. Lender: {lender}."
            if reference:
                instructions += f" Loan reference: {reference}."
            if loan is None:
                instructions += " Mortgage details are not yet identified; provide the statement for this property's active mortgage."
            instructions += " Use this upload destination for this mortgage only. The statement must identify the property, lender and current loan balance."
            result.append((context, FileRequestItem(document_type="mortgage_statement", name=name[:180],
                quantity=1, unit="files", period="latest", instructions=instructions[:2000])))
    return result


async def _refresh_definition(db: AsyncSession, state: ApplicationRequirementState, item: FileRequestItem,
                              context: dict, version) -> None:
    metadata = deepcopy(state.provenance or {})
    context = {**context, "version_id": str(version.id), "version_number": version.version_number}
    metadata["source_context"] = context
    metadata["last_schedule_version_id"] = str(version.id)
    metadata["publication_actor_name"] = getattr(version, "actor_name", None)
    metadata["publication_source"] = version.source
    memberships = file_requests.batch_memberships(state)
    for member in memberships.values():
        member["item"] = item.model_dump()
    state.provenance = metadata
    if not metadata.get("cancelled_at"):
        await file_requests.apply_memberships(db, state, memberships)
    else:
        # Staff cancellation is retained, including its original upload history.
        state.provenance = {**metadata, "request_batches": memberships}


async def _adopt_placeholder(db: AsyncSession, state: ApplicationRequirementState, context: dict) -> None:
    metadata = deepcopy(state.provenance or {})
    old_context = metadata.get("source_context") or {}
    config = {**(metadata.get("collection_config") or {}), "source_context": context}
    canonical = _canonical(context)
    metadata.update(canonical_key=canonical, source_context=context, collection_config=config,
                    previous_property_scope=old_context)
    metadata["external_definition"] = {**(metadata.get("external_definition") or {}),
                                       "canonical_key": canonical, "collection_config": config}
    state.provenance = metadata
    # Retain the task's identity/history as well as its upload destination.
    tasks = list((await db.execute(select(ApplicationCollectionTask).where(
        ApplicationCollectionTask.profile_id == state.profile_id))).scalars().all())
    for task in tasks:
        if state.requirement_key in (task.requirement_keys or []):
            task.canonical_identity = collection_tasks.canonical_identity(canonical, "document", config)


async def _retire(db: AsyncSession, state: ApplicationRequirementState, version) -> None:
    metadata = deepcopy(state.provenance or {})
    if metadata.get("cancelled_at"):
        return
    stamp = datetime.now(UTC).isoformat()
    memberships = file_requests.batch_memberships(state)
    for member in memberships.values():
        member["cancelled_at"] = stamp
    state.provenance = {**metadata, "request_batches": memberships, "cancelled_at": stamp,
        "cancelled_by_source": FORM_KIND, "inactive_definition": True,
        "schedule_retirement": {"version_id": str(version.id), "previous_status": state.status}}
    state.status = "not_applicable"
    state.state_reason = "This mortgage is no longer active in the saved real-estate schedule"
    requested = await db.get(BucketRequestedDocument, state.requested_document_id)
    if requested:
        requested.status = "not_applicable"


async def _restore(db: AsyncSession, state: ApplicationRequirementState) -> None:
    metadata = deepcopy(state.provenance or {})
    if metadata.get("cancelled_by_source") != FORM_KIND:
        return
    retirement = metadata.pop("schedule_retirement", {})
    metadata.pop("cancelled_at", None)
    metadata.pop("cancelled_by_source", None)
    metadata["inactive_definition"] = False
    memberships = file_requests.batch_memberships(state)
    for member in memberships.values():
        member["cancelled_at"] = None
    metadata["request_batches"] = memberships
    state.provenance = metadata
    state.status = retirement.get("previous_status") or "requested"
    state.state_reason = "This mortgage is active in the saved real-estate schedule"
    requested = await db.get(BucketRequestedDocument, state.requested_document_id)
    if requested:
        requested.status = "uploaded" if state.status in {"verified", "received_unverified"} else "requested"


async def sync_published_mortgage_requests(
    db: AsyncSession, *, profile, family, version, actor: Any = None,
) -> list[ApplicationRequirementState]:
    """Called inside the explicit Save transaction, before readiness refresh.

    Collection policy failures propagate and roll back publication atomically.
    There is deliberately no delivery call: normal configured reminders run only
    after the persisted upload destinations and tasks are committed.
    """
    if family.kind != FORM_KIND:
        return []
    if family.profile_id != profile.id or version.family_id != family.id:
        raise HTTPException(409, "Mortgage requests must belong to this saved real-estate schedule")
    if (family.current_version_id != version.id or version.status not in {"published", "filed"}
            or version.source != "form"):
        return []
    await file_requests.lock_profile(db, profile)
    requested_items = statement_requests(version.body, family.id)
    existing = [state for state in await file_requests.request_states(db, profile.id)
                if (state.provenance or {}).get("source_kind") == SOURCE_KIND
                and ((state.provenance or {}).get("source_context") or {}).get("kind") == FORM_KIND
                and ((state.provenance or {}).get("source_context") or {}).get("family_id") == str(family.id)]
    by_identity = {_identity(state.provenance["source_context"]): state for state in existing}
    active_by_property: dict[str, list[tuple[dict, FileRequestItem]]] = {}
    for context, item in requested_items:
        active_by_property.setdefault(context["property_row_id"], []).append((context, item))
    result, new_items, new_contexts = [], [], []
    active_ids = set()
    for context, item in requested_items:
        identity = _identity(context)
        state = by_identity.get(identity)
        placeholder = by_identity.get((identity[0], None))
        adopted = False
        if (state is None and identity[1] is not None and placeholder is not None
                and len(active_by_property[identity[0]]) == 1):
            state = placeholder
            await _adopt_placeholder(db, state, context)
            adopted = True
        if state is not None:
            old_hash = ((state.provenance or {}).get("source_context") or {}).get("identity_hash")
            if old_hash and old_hash != context["identity_hash"] and not adopted:
                from app.services.real_estate_evidence import invalidate_changed_identity

                await invalidate_changed_identity(db, profile=profile, state=state, identity_hash=context["identity_hash"])
            await _restore(db, state)
            await _refresh_definition(db, state, item, context, version)
            active_ids.add(state.id)
            result.append(state)
        else:
            new_items.append(item)
            new_contexts.append({**context, "version_id": str(version.id), "version_number": version.version_number})
    for state in existing:
        if state.id not in active_ids:
            await _retire(db, state, version)
    if new_items:
        states = await file_requests.create_batch(db, profile=profile,
            request_id=uuid5(family.id, f"mortgage-statements:{version.id}"), items=new_items, user=actor,
            canonical_keys=[_canonical(context) for context in new_contexts], source_kind=SOURCE_KIND,
            source_contexts=new_contexts)
        for state in states:
            state.provenance = {**(state.provenance or {}), "publication_actor_name": getattr(version, "actor_name", None),
                                "publication_source": version.source, "last_schedule_version_id": str(version.id)}
        result.extend(states)
    from app.services.real_estate_evidence import sync_published_evidence

    await sync_published_evidence(db, profile=profile, family=family, version=version, states=result)
    all_states = list((await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id))).scalars().all())
    await collection_tasks.sync_collection_tasks(db, profile, all_states)
    await db.flush()
    return result
