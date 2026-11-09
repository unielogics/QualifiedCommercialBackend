"""Canonical checklist identities and recoverable staff removal.

Only known schedule definitions share an identity. A label resembling another
label is never sufficient to combine requirements or their evidence.
"""

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationProfile,
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import BucketRequestedDocument

DEBT_KEYS = {"business_debt_schedule", "refinance_debt_schedule", "debt_schedule"}
CONTEXT_KEYS = ("subject", "period", "business_id", "entity_id", "owner_id", "person_id", "account_id", "account_key",
                "period_start", "period_end", "as_of", "as_of_date", "reporting_period", "reporting_date")
STAFF_SOURCES = {"staff_request", "financial_form_request"}


def _text(value):
    return " ".join(str(value or "").casefold().split())


def legal_name(value):
    return re.sub(r"[^a-z0-9]", "", _text(value))


async def profile_subjects(db, profile):
    """Only explicit intake/dealer anchors establish the file's own business."""
    names = set()
    if getattr(profile, "dealer_id", None):
        from app.dealer_os.models import DealerBusiness
        dealer = await db.get(DealerBusiness, profile.dealer_id)
        if dealer:
            names.update(legal_name(value) for value in (dealer.legal_name, dealer.name) if value)
    if getattr(profile, "intake_id", None):
        from app.models.public_underwriting_intake import PublicUnderwritingIntake
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake and intake.business_name:
            names.add(legal_name(intake.business_name))
    return names


def normalize_subject(config, subjects=None):
    config = deepcopy(config or {})
    if (legal_name(config.get("subject")) in (subjects or set())
            and not any(config.get(key) for key in ("business_id", "entity_id", "owner_id", "person_id"))):
        config["subject_label"] = config["subject"]
        config["subject"] = "business"
    return config


def debt_identity(key, config=None, canonical_key=None):
    config = config or {}
    if (key not in DEBT_KEYS and canonical_key not in DEBT_KEYS
            and config.get("document_type") != "debt_schedule" and config.get("form_kind") != "debt_schedule"):
        return None
    context = {name: _text(config[name]) for name in CONTEXT_KEYS if config.get(name) not in (None, "")}
    context.setdefault("subject", "business")
    context.setdefault("period", "current")
    if config.get("unit") not in (None, "", "files"):
        context["unit"] = _text(config["unit"])
    quantity = config.get("quantity", config.get("minimum_count", 1))
    if str(quantity) not in {"", "1", "None"}:
        context["quantity"] = str(quantity)
    if context == {"subject": "business", "period": "current"}:
        return "business_debt_schedule"
    digest = sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:24]
    return f"business_debt_schedule_{digest}"


def definition_key(requirement):
    return debt_identity(requirement.requirement_key, getattr(requirement, "collection_config", None),
                         getattr(requirement, "canonical_key", None)) or requirement.requirement_key


def normalize_definition(requirement, subjects=None):
    config = normalize_subject(getattr(requirement, "collection_config", None), subjects)
    canonical = debt_identity(requirement.requirement_key, config, getattr(requirement, "canonical_key", None))
    key = definition_key(requirement)
    if debt_identity(requirement.requirement_key, getattr(requirement, "collection_config", None), getattr(requirement, "canonical_key", None)) is None:
        return requirement
    from sqlalchemy.orm import class_mapper

    from app.models.ai_playbook import AICollectionRequirement

    if isinstance(requirement, AICollectionRequirement):
        values = {column.key: getattr(requirement, column.key) for column in class_mapper(AICollectionRequirement).column_attrs}
    else:
        values = vars(requirement).copy()
    return SimpleNamespace(**{**values, "requirement_key": canonical or key, "canonical_key": "business_debt_schedule", "collection_config": config,
                              "label": "Business debt schedule" if canonical == "business_debt_schedule" else requirement.label,
                              "original_requirement_key": requirement.requirement_key})


def state_key(state):
    metadata = state.provenance or {}
    return debt_identity(state.requirement_key, metadata.get("collection_config"), metadata.get("canonical_key")) or state.requirement_key


def derived_required(state):
    return bool(state.source_program_keys or [key for key in state.source_policy_keys or [] if key != "staff_added"]
                or (state.provenance or {}).get("source_kind") == "lender")


def removable(state):
    metadata = state.provenance or {}
    # Removal is a file-level collection decision, not a change to a published
    # program condition. The source definition and all linked evidence remain.
    return not metadata.get("redirect_requirement_key")


def lifecycle_fields(state):
    removed = (state.provenance or {}).get("staff_removal") or {}
    return {"can_remove": removable(state), "derived_required": derived_required(state),
            "removal_reason": removed.get("reason") if removed.get("removed_at") else None}


def is_removed(state):
    return bool(((state.provenance or {}).get("staff_removal") or {}).get("removed_at"))


def resolve_requirement_key(rows, key):
    candidates = [row for row in rows if not (getattr(row, "provenance", None) or {}).get("redirect_requirement_key")]
    if any(row.requirement_key == key for row in candidates):
        return key
    matches = {row.requirement_key for row in candidates if key in (getattr(row, "provenance", None) or {}).get("requirement_aliases", [])}
    if len(matches) > 1:
        raise HTTPException(409, {"code": "requirement_context_ambiguous", "message": "Choose the specific business, person, account or reporting period before changing this requirement."})
    return next(iter(matches), None)


def reactivate_for_derived(state):
    """Removing a staff ask never supplies a waiver for a future policy rule."""
    metadata = deepcopy(state.provenance or {})
    removal = metadata.get("staff_removal")
    if removal and removal.get("collection_suppression"):
        # Explicit current staff removal applies to this file until restored.
        # A changed program may still surface its qualification condition.
        return
    if not removal or not derived_required(state):
        return
    metadata["staff_removal_history"] = [*metadata.get("staff_removal_history", []), {
        **removal, "reactivated_at": datetime.now(UTC).isoformat(), "reactivated_by_derived_requirement": True,
    }]
    metadata.pop("staff_removal", None)
    metadata.pop("cancelled_at", None)
    metadata["inactive_definition"] = False
    # The cancelled staff batch remains cancelled even though the independent
    # program/policy now collects this same document.
    metadata["request_batches"] = {key: {**member, "cancelled_at": member.get("cancelled_at") or removal["removed_at"]}
                                   for key, member in (metadata.get("request_batches") or {}).items()}
    prior = removal.get("previous_status")
    explicit_disposition = (prior in {"waived", "not_applicable"} and not removal.get("previous_cancelled_at")
                            and state.verified_by_user_id is not None and state.verified_at is not None)
    state.status = prior if explicit_disposition else "requested"
    state.state_reason = "A current program or evidence policy requires this item; prior staff removal is retained in history"
    state.provenance = metadata


async def reconcile(db, profile, states):
    """Retire duplicate identities without deleting evidence or past decisions."""
    subjects = await profile_subjects(db, profile)
    grouped = {}
    for state in states:
        if debt_identity(state.requirement_key, (state.provenance or {}).get("collection_config"), (state.provenance or {}).get("canonical_key")):
            state.provenance = {**(state.provenance or {}), "collection_config": normalize_subject((state.provenance or {}).get("collection_config"), subjects)}
            grouped.setdefault(state_key(state), []).append(state)
    for key, members in grouped.items():
        canonical = next((state for state in members if state.requirement_key == key), None)
        if canonical is None:
            # Keep a stable state ID, including its immutable decision links.
            canonical = members[0]
            former = canonical.requirement_key
            canonical.requirement_key = key
            canonical.provenance = {**(canonical.provenance or {}), "requirement_aliases": sorted({former, *((canonical.provenance or {}).get("requirement_aliases") or [])})}
        metadata = deepcopy(canonical.provenance or {})
        aliases = set(metadata.get("requirement_aliases") or [])
        requested_ids = set(metadata.get("merged_requested_document_ids") or [])
        if canonical.requested_document_id:
            requested_ids.add(str(canonical.requested_document_id))
        for other in members:
            if other is canonical or (other.provenance or {}).get("redirect_requirement_key") == key:
                continue
            aliases.add(other.requirement_key)
            aliases.update((other.provenance or {}).get("requirement_aliases") or [])
            canonical.source_program_keys = sorted(set(canonical.source_program_keys or []) | set(other.source_program_keys or []))
            canonical.source_policy_keys = sorted(set(canonical.source_policy_keys or []) | set(other.source_policy_keys or []))
            for name in ("staff_added", "source_kind", "external_definition", "file_request_id", "request_item", "request_fingerprint", "source_key"):
                if name not in metadata and (other.provenance or {}).get(name) is not None:
                    metadata[name] = deepcopy(other.provenance[name])
            from app.services.file_requests import batch_memberships
            metadata["request_batches"] = {**metadata.get("request_batches", {}), **batch_memberships(other)}
            if canonical.requested_document_id is None:
                canonical.requested_document_id = other.requested_document_id
            if other.requested_document_id:
                requested_ids.add(str(other.requested_document_id))
            links = list((await db.execute(select(ApplicationRequirementEvidence).where(
                ApplicationRequirementEvidence.requirement_state_id.in_([canonical.id, other.id]),
                ApplicationRequirementEvidence.removed_at.is_(None),
            ))).scalars().all())
            from app.services.application_programs import _latest_evidence_decisions
            decisions = await _latest_evidence_decisions(db, links)
            canonical_files = {link.file_id: link for link in links if link.requirement_state_id == canonical.id}
            for link in links:
                if link.requirement_state_id != other.id:
                    continue
                prior = canonical_files.get(link.file_id)
                if prior is not None and preferred_link([prior, link], decisions) is link:
                    prior.removed_at = datetime.now(UTC)
                    prior.reason = "Superseded duplicate association during canonical requirement reconciliation"
                    await db.flush()  # Free the partial unique index before moving the surviving link.
                    canonical_files.pop(link.file_id)
                if link.file_id not in canonical_files:
                    link.provenance = {**(link.provenance or {}), "merged_from_requirement_state_id": str(other.id), "merged_from_requirement_key": other.requirement_key}
                    link.requirement_state_id = canonical.id
                    canonical_files[link.file_id] = link
                # A second link to the same file remains on the historical
                # state; its immutable decisions are never discarded.
            other.provenance = {**(other.provenance or {}), "redirect_requirement_key": key,
                                "inactive_definition": True, "merged_at": datetime.now(UTC).isoformat()}
            other.status = "stale"
            other.state_reason = "Shared with the canonical business debt schedule; evidence and history retained"
        metadata.update(requirement_aliases=sorted(aliases), merged_requested_document_ids=sorted(requested_ids))
        canonical.provenance = metadata
        if canonical.requested_document_id:
            requested = await db.get(BucketRequestedDocument, canonical.requested_document_id)
            if requested:
                requested.requirement_key = key
        for requested_id in requested_ids:
            from uuid import UUID
            requested = await db.get(BucketRequestedDocument, UUID(requested_id))
            if requested and requested.id != canonical.requested_document_id:
                requested.status = "closed"
                requested.closed_at = requested.closed_at or datetime.now(UTC)
                requested.closed_reason = "requirement_merged"
                requested.requirement_source = {**(requested.requirement_source or {}), "redirect_requirement_key": key,
                                                "redirect_requested_document_id": str(canonical.requested_document_id)}
        if getattr(profile, "missing_item_email_requirement_key", None) in aliases:
            profile.missing_item_email_requirement_key = key
    await db.flush()
    return states


def preferred_link(links, decisions):
    """A valid staff override survives AI branches; newest staff decision wins.

    The normal evidence reconciliation still validates content and policy before
    retaining that decision. No decision is copied, rewritten, or deleted.
    """
    def rank(link):
        decision = decisions.get(link.id)
        actor = getattr(decision, "actor_kind", None)
        staff = actor == "staff" or decision is None and link.verified_by_user_id is not None
        at = getattr(decision, "created_at", None) or link.verified_at or link.linked_at
        return bool(staff), str(at or ""), str(link.id)
    return max(links, key=rank)


async def reconcile_profile(db, profile):
    """Idempotent maintenance: existing identities only, no outreach or AI.

    The caller owns commit. This deliberately avoids readiness's automatic
    selection and underwriting advancement; the next ordinary refresh derives
    current coverage from the retained evidence as usual.
    """
    from app.services import collection_tasks
    from app.services.financial_workspace import lock_financial_profile

    await lock_financial_profile(db, profile)
    states = list((await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
    ).execution_options(populate_existing=True))).scalars().all())
    await reconcile(db, profile, states)
    active = [row for row in states if not (row.provenance or {}).get("redirect_requirement_key")
              and not (row.provenance or {}).get("inactive_definition") and not is_removed(row)]
    tasks = await collection_tasks.sync_collection_tasks(db, profile, active)
    return {
        "active_debt_schedule_requirements": sum(debt_identity(row.requirement_key, (row.provenance or {}).get("collection_config"), (row.provenance or {}).get("canonical_key")) is not None for row in active),
        "retired_duplicate_requirements": sum(bool((row.provenance or {}).get("redirect_requirement_key")) for row in states),
        "active_debt_schedule_tasks": sum(row.canonical_identity.startswith("debt_schedule:") for row in tasks),
    }


async def change(db, profile, requirement_key, *, action, reason, actor):
    from app.services import application_programs, requirement_library
    from app.services.financial_workspace import lock_financial_profile

    await lock_financial_profile(db, profile)
    if isinstance(profile, ApplicationProfile):
        # A profile loaded before waiting for the lock may have stale staff
        # requests or reminder fields. Do not overwrite another staff edit.
        profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
                                    .execution_options(populate_existing=True))).scalar_one_or_none()
        if profile is None:
            raise HTTPException(404, "Application profile not found")
    await application_programs.get_program_readiness(db, profile)
    states = list((await db.execute(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id)
                                  .execution_options(populate_existing=True))).scalars().all())
    resolved = resolve_requirement_key(states, requirement_key)
    state = next((row for row in states if row.requirement_key == resolved), None)
    if state is None:
        raise HTTPException(404, "Requirement not found")
    metadata = deepcopy(state.provenance or {})
    previous = metadata.get("staff_removal") or {}
    if action == "remove":
        if not removable(state) and not previous.get("removed_at"):
            raise HTTPException(409, "Choose the current canonical checklist item before removing it")
        if previous.get("removed_at"):
            return
        if not isinstance(reason, str) or len(reason.strip()) < 8 or len(reason) > 2000:
            raise HTTPException(422, "A removal reason of at least eight characters is required")
        reason = reason.strip()
        aliases = {state.requirement_key, *(metadata.get("requirement_aliases") or [])}
        entries = [entry for entry in requirement_library.clean_entries(profile.extra_requirements) if entry["key"] in aliases]
        profile.extra_requirements = [entry for entry in requirement_library.clean_entries(profile.extra_requirements) if entry["key"] not in aliases]
        metadata["staff_removal"] = {"removed_at": datetime.now(UTC).isoformat(), "actor_id": str(actor.id), "reason": reason,
                                     "entries": entries, "previous_status": state.status, "previous_cancelled_at": metadata.get("cancelled_at"),
                                     "collection_suppression": True,
                                     "source_program_keys": list(state.source_program_keys or []),
                                     "source_policy_keys": list(state.source_policy_keys or []),
                                     "qualification_effect": "No published program condition is waived or satisfied by removing this collection item."}
        metadata.update(cancelled_at=metadata["staff_removal"]["removed_at"], inactive_definition=True)
        state.status, state.state_reason = "not_applicable", "Removed by staff: " + reason
    elif action == "restore":
        if not previous.get("removed_at"):
            return
        current = requirement_library.clean_entries(profile.extra_requirements)
        present = {entry["key"] for entry in current}
        profile.extra_requirements = [*current, *(entry for entry in previous.get("entries", []) if entry["key"] not in present)]
        metadata["staff_removal_history"] = [*metadata.get("staff_removal_history", []), {**previous, "restored_at": datetime.now(UTC).isoformat(), "restored_by_user_id": str(actor.id)}]
        metadata.pop("staff_removal", None)
        if previous.get("previous_cancelled_at"):
            metadata["cancelled_at"] = previous["previous_cancelled_at"]
        else:
            metadata.pop("cancelled_at", None)
        metadata["inactive_definition"] = bool(previous.get("previous_cancelled_at"))
        state.status, state.state_reason = previous.get("previous_status", "requested"), "Restored by staff"
    else:
        raise HTTPException(422, "Unknown checklist lifecycle action")
    state.provenance = metadata
    slot_snapshots = metadata["staff_removal"].setdefault("slot_snapshots", {}) if action == "remove" else previous.get("slot_snapshots", {})
    for slot_id in {state.requested_document_id, *(metadata.get("merged_requested_document_ids") or [])} - {None}:
        from uuid import UUID
        slot = await db.get(BucketRequestedDocument, UUID(str(slot_id)))
        if slot and (action == "remove" or slot.id == state.requested_document_id):
            if action == "remove":
                slot_snapshots[str(slot.id)] = slot.status
                metadata["staff_removal"].setdefault("slot_details", {})[str(slot.id)] = {
                    "closed_at": slot.closed_at.isoformat() if slot.closed_at else None,
                    "closed_reason": slot.closed_reason,
                }
                slot.status = "not_applicable"
                slot.closed_at = slot.closed_at or datetime.now(UTC)
                slot.closed_reason = "staff_removed_requirement"
            elif str(slot.id) in slot_snapshots:
                slot.status = slot_snapshots[str(slot.id)]
                details = previous.get("slot_details", {}).get(str(slot.id))
                if details is not None:
                    slot.closed_at = datetime.fromisoformat(details["closed_at"]) if details.get("closed_at") else None
                    slot.closed_reason = details.get("closed_reason")
    tasks = list((await db.execute(select(ApplicationCollectionTask).where(ApplicationCollectionTask.profile_id == profile.id))).scalars().all())
    for task in tasks:
        if state.requirement_key in (task.requirement_keys or []):
            others = [row for row in states if row is not state and row.requirement_key in (task.requirement_keys or [])
                      and not is_removed(row) and not (row.provenance or {}).get("redirect_requirement_key")
                      and not (row.provenance or {}).get("inactive_definition")]
            if others:
                if all(row.status in {"verified", "waived", "not_applicable"} for row in others):
                    task.status, task.next_attempt_at = "completed", None
                    task.completed_at = task.completed_at or datetime.now(UTC)
                continue
            if action == "remove":
                metadata["staff_removal"].setdefault("task_snapshots", {})[str(task.id)] = {
                    "status": task.status, "next_attempt_at": task.next_attempt_at.isoformat() if task.next_attempt_at else None,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None}
                task.status, task.next_attempt_at = "cancelled", None
            else:
                snapshot = previous.get("task_snapshots", {}).get(str(task.id))
                if snapshot:
                    task.status = snapshot["status"]
                    task.next_attempt_at = datetime.fromisoformat(snapshot["next_attempt_at"]) if snapshot.get("next_attempt_at") else None
                    task.completed_at = datetime.fromisoformat(snapshot["completed_at"]) if snapshot.get("completed_at") else None
                    if state.status in {"verified", "waived", "not_applicable"} or metadata.get("cancelled_at"):
                        task.status = "cancelled" if metadata.get("cancelled_at") else "completed"
                        task.next_attempt_at = None
    if action == "remove" and profile.missing_item_email_requirement_key == state.requirement_key:
        metadata["staff_removal"]["next_email_at"] = profile.missing_item_email_next_send_at.isoformat() if profile.missing_item_email_next_send_at else None
        profile.missing_item_email_next_send_at = None
    elif action == "restore" and profile.missing_item_email_requirement_key == state.requirement_key:
        value = previous.get("next_email_at")
        profile.missing_item_email_next_send_at = datetime.fromisoformat(value) if value and state.status not in {"verified", "waived", "not_applicable"} and not metadata.get("cancelled_at") else None
    state.provenance = metadata
    await db.flush()
