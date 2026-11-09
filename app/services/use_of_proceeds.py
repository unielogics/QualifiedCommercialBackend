"""Bind the proceeds checklist to the shared, revisioned budget declaration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException

from app.services import requirement_lifecycle, use_of_funds


def is_requirement(requirement) -> bool:
    return (getattr(requirement, "canonical_key", None) or getattr(requirement, "requirement_key", None)) == "use_of_proceeds"


def file_is_bound(state, file) -> bool:
    """Supporting evidence needs an explicit destination or recorded link."""
    metadata = state.provenance or {}
    linked = {str(value) for value in metadata.get("linked_file_ids", [])}
    return (
        bool(getattr(state, "requested_document_id", None) and getattr(file, "requested_document_id", None) == state.requested_document_id)
        or file.id == getattr(state, "evidence_file_id", None)
        or str(file.id) in linked
    )


def snapshot(profile, state, budget) -> dict:
    metadata = state.provenance or {}
    material = {
        "source": "use_of_funds", "profile_id": str(profile.id),
        "requirement_key": state.requirement_key,
        "definition_id": metadata.get("definition_id"),
        "definition_version": metadata.get("definition_version"),
        "review_checks": metadata.get("review_checks") or [],
        "requires_human": metadata.get("requires_human"),
        "budget_revision": budget.revision,
        "funding_request_revision": budget.funding_request_revision,
        "requested_amount": budget.requested_amount,
        "requested_amount_source": budget.requested_amount_source,
        "items": [item.model_dump(mode="json") for item in budget.items],
    }
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    receipt = metadata.get("collection_completion_receipt") or {}
    current = bool(budget.complete and receipt.get("source") == "use_of_funds" and receipt.get("fingerprint") == fingerprint)
    excluded = requirement_lifecycle.is_removed(state) or state.status in {"waived", "not_applicable"}
    return {
        "source": "use_of_funds", "fingerprint": fingerprint,
        "complete": current and not excluded,
        "can_complete": bool(budget.complete and not current and not excluded),
        "eligible_file_ids": [], "suggested_file_ids": [],
        "completed_at": receipt.get("at") if current else None,
        "reason": (
            "The current itemized budget has a staff collection receipt; supporting costs and program conditions remain separate."
            if current else
            "The complete allocation is saved. Staff may mark collection complete after reviewing it."
            if budget.complete else
            "Enter itemized business uses that add up to the current requested amount before completing collection."
        ),
        "summary": {
            "allocated_amount": budget.total, "requested_amount": budget.requested_amount,
            "item_count": len(budget.items), "complete": budget.complete,
            "revision": budget.revision, "funding_request_revision": budget.funding_request_revision,
        },
    }


async def materialize(db, profile, requirement, state, requested, files, *, qualification=None) -> bool:
    if not is_requirement(requirement):
        return False
    budget = await use_of_funds.read_budget(db, profile)
    # Existing, explicitly linked proceeds documents keep their evidence path
    # until somebody deliberately starts the shared structured budget.
    if not budget.items and budget.revision == 0 and any(file_is_bound(state, file) for file in files):
        state.provenance = {key: value for key, value in (state.provenance or {}).items() if key != "use_of_funds_collection"}
        return False
    previous = (state.provenance or {}).get("use_of_funds_collection") or {}
    collection = snapshot(profile, state, budget)
    checks = (state.provenance or {}).get("review_checks") or []
    program_review = (state.provenance or {}).get("use_of_funds_program_review") or {}
    if qualification and qualification.get("status") == "met" and checks and program_review.get("fingerprint") != collection["fingerprint"]:
        qualification = {
            **qualification, "status": "pending",
            "blocking": any(check.get("severity", "block") == "block" for check in checks),
            "reasons": ["Review and sign off the supporting evidence against the current allocation; the budget or funding request changed."],
        }
    if not requirement_lifecycle.is_removed(state) and state.status not in {"waived", "not_applicable"}:
        failed_current = state.status == "failed" and previous.get("fingerprint") == collection["fingerprint"]
        if collection["complete"] and not failed_current:
            receipt = state.provenance["collection_completion_receipt"]
            from uuid import UUID

            state.status = "verified"
            state.verified_at = datetime.fromisoformat(receipt["at"])
            state.verified_by_user_id = UUID(receipt["actor_id"])
            state.state_reason = "Staff completed collection of the current itemized use-of-funds budget"
        elif not failed_current:
            state.status = "received_unverified" if budget.items else "requested" if requested else "missing"
            state.verified_at = None
            state.verified_by_user_id = None
            state.state_reason = collection["reason"]
        if budget.items:
            state.received_at = state.received_at or budget.updated_at or datetime.now(UTC)
        if requested and not requested.requires_signature:
            requested.status = "uploaded" if budget.complete else "requested"
    from app.services.requirement_collection_completion import qualification_review

    state.provenance = {
        **(state.provenance or {}), "source": "use_of_funds",
        "use_of_funds_collection": collection, "collection_completion": collection,
        "coverage": {"current": int(budget.complete), "required": 1, "unit": "budget", "complete": budget.complete},
        "verified_coverage": {"current": int(collection["complete"]), "required": 1, "unit": "budget", "complete": collection["complete"]},
        "qualification_review": qualification if qualification is not None else qualification_review(state),
    }
    return True


async def complete_collection(db, profile, state, payload, actor, *, request_stamp):
    if requirement_lifecycle.is_removed(state) or state.status in {"waived", "not_applicable"}:
        raise HTTPException(409, "Restore this requirement before recording new budget collection")
    if payload.file_ids:
        raise HTTPException(422, "Review the saved budget without selecting unrelated source files")
    budget = await use_of_funds.read_budget(db, profile)
    current = snapshot(profile, state, budget)
    if current["fingerprint"] != payload.expected_fingerprint:
        raise HTTPException(409, "The budget or funding request changed. Reload and review the current allocation.")
    if not budget.complete:
        raise HTTPException(422, current["reason"])
    timestamp = datetime.now(UTC)
    previous = (state.provenance or {}).get("collection_completion_receipt")
    history = [*((state.provenance or {}).get("collection_completion_history") or []), *([previous] if previous else [])]
    receipt = {
        "source": "use_of_funds", "at": timestamp.isoformat(), "actor_id": str(actor.id),
        "reason": payload.reason.strip(), "fingerprint": current["fingerprint"], "file_ids": [],
        "idempotency_key": payload.idempotency_key, "request_stamp": request_stamp,
        "qualification_approved": False, "budget": current["summary"],
    }
    state.provenance = {**(state.provenance or {}), "collection_completion_history": history,
                        "collection_completion_receipt": receipt}
    from app.services import application_profiles, file_events

    await application_profiles.log_profile_action(db, profile, actor, "requirement.collection_completed",
        "Marked itemized use-of-funds collection complete", target_type="application_requirement", target_id=state.id,
        metadata={"requirement_key": state.requirement_key, **receipt})
    await file_events.emit(db, profile=profile, kind="requirement.collection_completed", visibility=file_events.VISIBILITY_CLIENT,
        title=f"Application item completed: {state.label}", actor=actor, target_type="application_requirement", target_id=state.id,
        meta={"requirement_key": state.requirement_key, "collection_complete": True})
    await db.flush()
