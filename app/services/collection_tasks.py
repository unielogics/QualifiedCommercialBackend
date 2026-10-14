"""Catalog-backed collection actions shared across products and lenders.

Requirement state remains the readiness authority. Tasks consolidate work and
responses preserve an audit trail; neither an AI suggestion nor a reminder can
promote an unapproved requirement into borrower policy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationExtractedFact,
    ApplicationProfile,
    ApplicationRequirementResponse,
    ApplicationRequirementState,
)

KINDS = {"field", "document", "form", "consent", "human_review"}
ACTIONS = {
    "field": "answer_fields",
    "document": "upload_documents",
    "form": "complete_form",
    "consent": "sign_consent",
    "human_review": "internal_review",
}
COMPLETE = {"verified", "waived", "not_applicable"}
OPEN_TASKS = {"open", "requested"}


def timestamp() -> datetime:
    return datetime.now(UTC)


def requirement_kind(requirement: object) -> str:
    value = getattr(requirement, "requirement_kind", None) or "document"
    return value if value in KINDS else "document"


def collects_file(requirement: object) -> bool:
    kind = requirement_kind(requirement)
    return (
        kind == "document"
        or kind == "form"
        and not (getattr(requirement, "collection_config", None) or {}).get("fields")
    )


def canonical_identity(key: str, kind: str, config: dict) -> str:
    """Entity and period are part of identity; labels and filenames never are."""
    material = [
        kind,
        key,
        str(config.get("subject") or "business"),
        str(config.get("period") or "current"),
    ]
    # Different disclosures or form schemas must never share one acceptance.
    if kind in {"consent", "form"}:
        material.append(
            hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[
                :16
            ]
        )
    value = ":".join(material)
    return (
        value
        if len(value) <= 200
        else value[:130] + ":" + hashlib.sha256(value.encode()).hexdigest()
    )


def definition_metadata(requirement: object, *, version: int = 1) -> dict:
    return {
        "requirement_kind": requirement_kind(requirement),
        "canonical_key": getattr(requirement, "canonical_key", None)
        or getattr(requirement, "requirement_key", "unknown"),
        "collection_config": dict(getattr(requirement, "collection_config", None) or {}),
        "definition_version": str(version),
        "definition_id": str(getattr(requirement, "id", "")),
        "catalog_approved": True,
        "client_visible": "borrower" in (getattr(requirement, "visibility", None) or []),
        "completion_criteria": getattr(requirement, "completion_criteria", "") or "",
        "expiration_days": getattr(requirement, "expiration_days", None),
        "objective_text": getattr(requirement, "objective_text", "") or "",
        "request_message": getattr(requirement, "ai_request_message_template", None),
    }


async def latest_responses(
    db: AsyncSession, profile_id: uuid.UUID
) -> dict[uuid.UUID, ApplicationRequirementResponse]:
    rows = list(
        (
            await db.execute(
                select(ApplicationRequirementResponse)
                .where(
                    ApplicationRequirementResponse.profile_id == profile_id,
                )
                .order_by(
                    ApplicationRequirementResponse.created_at.desc(),
                    ApplicationRequirementResponse.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    result = {}
    for row in rows:
        result.setdefault(row.requirement_state_id, row)
    return result


def merged_task_config(
    members: list[ApplicationRequirementState],
) -> tuple[ApplicationRequirementState, dict]:
    """The collection request must cover the strictest shared lender checklist."""

    def count(row, key):
        metadata = row.provenance or {}
        raw = (metadata.get("collection_config") or {}).get(key)
        if raw is None:
            raw = (metadata.get("coverage") or {}).get(key)
        return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0

    representative = max(
        members,
        key=lambda row: (
            count(row, "required_months"),
            count(row, "required_years"),
            count(row, "minimum_count"),
        ),
    )
    config = dict((representative.provenance or {}).get("collection_config") or {})
    for key in ("required_months", "required_years", "minimum_count"):
        value = max(count(row, key) for row in members)
        if value:
            config[key] = value
    definitions = [(row.provenance or {}).get("collection_config") or {} for row in members]
    for key, combine in (("min", max), ("max", min), ("max_length", min)):
        values = [
            item[key]
            for item in definitions
            if isinstance(item.get(key), (float, int)) and not isinstance(item[key], bool)
        ]
        if values:
            config[key] = combine(values)
    choices = [item["choices"] for item in definitions if isinstance(item.get("choices"), list)]
    if choices:
        config["choices"] = [
            choice for choice in choices[0] if all(choice in other for other in choices[1:])
        ]
    expirations = [
        int((row.provenance or {})["expiration_days"])
        for row in members
        if (row.provenance or {}).get("expiration_days")
    ]
    if expirations:
        config["expiration_days"] = min(expirations)
    return representative, config


async def materialize_response_state(
    db: AsyncSession, state: ApplicationRequirementState, responses: dict | None = None
) -> None:
    metadata = dict(state.provenance or {})
    kind = metadata.get("requirement_kind", "document")
    if kind == "document" or state.status in {"waived", "not_applicable"}:
        return
    if kind == "human_review" and state.status == "verified" and state.verified_by_user_id:
        return
    response = (
        responses if responses is not None else await latest_responses(db, state.profile_id)
    ).get(state.id)
    if response is not None:
        current_version = str(metadata.get("definition_version") or "1")
        current_definition = metadata.get("definition_id")
        same_definition = (
            not current_definition or response.provenance.get("definition_id") == current_definition
        )
        same_disclosure = kind != "consent" or response.provenance.get(
            "disclosure_hash"
        ) == disclosure_hash(metadata.get("collection_config") or {})
        expired = bool(
            metadata.get("expiration_days")
            and response.created_at < timestamp() - timedelta(days=int(metadata["expiration_days"]))
        )
        if (
            response.source_version != current_version
            or not same_definition
            or not same_disclosure
            or expired
        ):
            state.status, state.state_reason = (
                "stale",
                "Requirements changed; please confirm the current information",
            )
        elif response.review_state == "accepted":
            state.status = "verified"
            state.verified_at = response.reviewed_at or response.created_at
            state.verified_by_user_id = response.reviewed_by_user_id
            state.state_reason = "Structured response accepted"
        elif response.review_state == "needs_more":
            state.status, state.state_reason = "requested", "Please clarify or update this answer"
        else:
            state.status = "received_unverified"
            state.state_reason = (
                "Response needs staff review"
                if response.review_state != "unavailable"
                else "Borrower cannot provide this item; staff follow-up required"
            )
        state.received_at = state.received_at or response.created_at
        metadata["response"] = {
            "id": str(response.id),
            "value": response.value,
            "review_state": response.review_state,
            "source": response.source,
        }
    elif kind == "human_review":
        state.status, state.state_reason = "missing", "Internal review required"
    else:
        state.status, state.state_reason = "missing", "Awaiting a structured response"
    complete = state.status == "verified"
    if not complete:
        state.verified_at, state.verified_by_user_id = None, None
    collected = (
        response is not None and response.review_state != "needs_more" and state.status != "stale"
    )
    metadata["coverage"] = {
        "current": int(collected),
        "required": 1,
        "unit": "responses",
        "complete": collected,
    }
    metadata["verified_coverage"] = {
        "current": int(complete),
        "required": 1,
        "unit": "responses",
        "complete": complete,
    }
    state.provenance = metadata


async def sync_collection_tasks(
    db: AsyncSession, profile: ApplicationProfile, states: list[ApplicationRequirementState]
) -> list[ApplicationCollectionTask]:
    existing = list(
        (
            await db.execute(
                select(ApplicationCollectionTask).where(
                    ApplicationCollectionTask.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_identity = {row.canonical_identity: row for row in existing}
    fact_rows = list(
        (
            await db.execute(
                select(ApplicationExtractedFact)
                .where(
                    ApplicationExtractedFact.profile_id == profile.id,
                    ApplicationExtractedFact.status.in_(["suggested", "accepted"]),
                )
                .order_by(ApplicationExtractedFact.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    prefills = {}
    for fact in fact_rows:
        prefills.setdefault(
            fact.field_key,
            {
                "value": (fact.value or {}).get("value"),
                "source": fact.extraction_method,
                "source_file_id": str(fact.source_file_id) if fact.source_file_id else None,
            },
        )
    grouped: dict[str, list[ApplicationRequirementState]] = {}
    for state in states:
        metadata = state.provenance or {}
        if state.status == "stale" and metadata.get("inactive_definition"):
            continue
        kind = metadata.get("requirement_kind", "document")
        identity = canonical_identity(
            metadata.get("canonical_key") or state.requirement_key,
            kind,
            metadata.get("collection_config") or {},
        )
        grouped.setdefault(identity, []).append(state)
    result = []
    for identity, members in grouped.items():
        first, merged_config = merged_task_config(members)
        metadata = first.provenance or {}
        kind = metadata.get("requirement_kind", "document")
        client_visible = all((row.provenance or {}).get("client_visible", False) for row in members)
        approved = all(
            (row.provenance or {}).get("catalog_approved", False)
            and ((row.provenance or {}).get("collection_config") or {}).get("automation_approved")
            is not False
            for row in members
        )
        source_programs = {key for row in members for key in (row.source_program_keys or [])}
        arf_permissions_blocked = False
        if (
            any(key.startswith("arf_") for key in source_programs)
            or getattr(profile, "funding_category", None) == "arf_external"
        ):
            from app.services.arf_channel import approval_valid, get_configuration

            arf_config = await get_configuration(db)
            requested_key = (
                (metadata.get("collection_config") or {}).get("field_key")
                or metadata.get("canonical_key")
                or first.requirement_key
            )
            allowed = (
                arf_config.permitted_documents
                if kind in {"document", "form"}
                else arf_config.permitted_fields
            )
            arf_permissions_blocked = (
                arf_config.intake_mode != "partner_hosted_authorized"
                or not approval_valid(arf_config, "hosted_intake")
                or requested_key not in (allowed or [])
            )
            approved = approved and not arf_permissions_blocked
        task = by_identity.get(identity)
        if task is None:
            task = ApplicationCollectionTask(
                profile_id=profile.id,
                canonical_identity=identity,
                label=first.label,
                action_type=ACTIONS.get(kind, "internal_review"),
                status="open",
                cadence_hours=24,
                max_attempts=3,
                attempts=0,
                channel="portal",
            )
            db.add(task)
        task.requirement_state_ids = [str(row.id) for row in members]
        task.requirement_keys = [row.requirement_key for row in members]
        task.label = first.label
        message_override = (task.config or {}).get("request_message_override")
        task.description = (
            message_override
            or metadata.get("request_message")
            or metadata.get("objective_text")
            or metadata.get("completion_criteria")
            or first.state_reason
        )
        task.config = {
            **merged_config,
            "requirement_kind": kind,
            "requested_document_ids": list(
                dict.fromkeys(
                    str(row.requested_document_id) for row in members if row.requested_document_id
                )
            ),
            "source_program_keys": sorted(
                {key for row in members for key in (row.source_program_keys or [])}
            ),
            "required": any(row.required_level == "required" for row in members),
        }
        if message_override:
            task.config = {**task.config, "request_message_override": message_override}
        field_key = task.config.get("field_key") or metadata.get("canonical_key")
        if kind == "field" and field_key in prefills:
            task.config = {**task.config, "prefill": prefills[field_key]}
        if metadata.get("response"):
            task.config = {**task.config, "response": metadata["response"]}
        task.automation_approved = approved and client_visible and kind != "human_review"
        if kind == "human_review" or not approved or not client_visible:
            task.assignee = "internal"
            if arf_permissions_blocked:
                task.state_reason = "Written ARF permission for this item must be confirmed before borrower collection"
        elif task.status not in {"unavailable", "escalated"} and not task.assigned_user_id:
            task.assignee = "borrower"
        if all(row.status in COMPLETE for row in members):
            task.status, task.completed_at, task.next_attempt_at = (
                "completed",
                task.completed_at or timestamp(),
                None,
            )
        elif task.status == "completed":
            task.status, task.completed_at, task.attempts = "open", None, 0
            task.next_attempt_at = timestamp()
        elif task.status not in {"paused", "unavailable", "escalated"}:
            if task.attempts >= task.max_attempts:
                task.status, task.assignee, task.next_attempt_at = "escalated", "internal", None
                task.state_reason = "Automatic outreach limit reached; staff follow-up required"
            elif all(row.status == "received_unverified" for row in members):
                task.status, task.next_attempt_at = "in_review", None
            else:
                task.status = "requested" if task.attempts else "open"
                task.next_attempt_at = task.next_attempt_at or timestamp()
        for member in members:
            member.provenance = {
                **(member.provenance or {}),
                "collection_status": task.status,
                "collection_assignee": task.assignee,
                "collection_attempts": task.attempts,
            }
        result.append(task)
    for task in existing:
        if task.canonical_identity not in grouped:
            task.status, task.next_attempt_at = "cancelled", None
    await db.flush()
    return result


def disclosure_hash(config: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: config.get(key) for key in ("disclosure_text", "disclosure_version", "scope")},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def validate_answer(kind: str, config: dict, value: Any) -> Any:
    if kind == "consent":
        if not config.get("disclosure_text") or not config.get("disclosure_version"):
            raise HTTPException(409, "The approved consent disclosure has not been configured")
        if (
            not isinstance(value, dict)
            or value.get("granted") is not True
            or not str(value.get("consenter_name") or "").strip()
        ):
            raise HTTPException(422, "Explicit consent and the signer's full name are required")
        return {"granted": True, "consenter_name": str(value["consenter_name"]).strip()[:160]}
    if kind == "form" and config.get("fields"):
        if not isinstance(value, dict):
            raise HTTPException(422, "Complete the structured form")
        return {
            field["key"]: validate_answer("field", field, value.get(field["key"]))
            for field in config["fields"]
        }
    if kind == "form":
        raise HTTPException(
            422, "Complete the approved form and upload its signed or completed copy"
        )
    if kind in {"document", "human_review"}:
        raise HTTPException(422, "This action requires an upload or staff verification")
    value_type = config.get("value_type", "text")
    if value is None or isinstance(value, str) and not value.strip():
        if config.get("required", True):
            raise HTTPException(422, "This answer is required")
        return None
    if value_type in {"number", "integer"}:
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if not math.isfinite(number) or value_type == "integer" and not number.is_integer():
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(422, "Enter a valid number") from None
        if (
            config.get("min") is not None
            and number < float(config["min"])
            or config.get("max") is not None
            and number > float(config["max"])
        ):
            raise HTTPException(422, "The number is outside the configured range")
        return int(number) if value_type == "integer" else number
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise HTTPException(422, "Select yes or no")
        return value
    if not isinstance(value, str):
        raise HTTPException(422, "Enter a text answer")
    value = value.strip()
    if len(value) > min(int(config.get("max_length", 10000)), 10000):
        raise HTTPException(422, "The answer is too long")
    if value_type == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(422, "Enter a valid email address")
    if value_type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, "Enter a date in YYYY-MM-DD format") from None
    if value_type == "enum" and value not in config.get("choices", []):
        raise HTTPException(422, "Select one of the available choices")
    return value


async def answer_task(
    db: AsyncSession,
    profile: ApplicationProfile,
    task: ApplicationCollectionTask,
    *,
    value: Any = None,
    unavailable_reason: str | None = None,
    user_id: uuid.UUID | None = None,
    source: str = "borrower_room",
    provenance: dict | None = None,
) -> list[ApplicationRequirementResponse]:
    if task.profile_id != profile.id or task.status in {"cancelled", "completed"}:
        raise HTTPException(409, "This collection action is no longer open")
    if source == "borrower_room" and (
        task.status not in {"open", "requested", "escalated"}
        or task.assignee != "borrower"
        and task.status != "escalated"
        or not task.automation_approved
    ):
        raise HTTPException(404, "Collection action not found")
    from app.services.arf_channel import profile_uses_arf, require_hosted_permission

    if await profile_uses_arf(db, profile):
        requested = (task.config or {}).get("field_key")
        await require_hosted_permission(db, requested_fields=[requested] if requested else [])
    states = list(
        (
            await db.execute(
                select(ApplicationRequirementState).where(
                    ApplicationRequirementState.profile_id == profile.id,
                    ApplicationRequirementState.id.in_(
                        [uuid.UUID(key) for key in task.requirement_state_ids]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not states:
        raise HTTPException(404, "Requirement no longer exists")
    unavailable = bool(unavailable_reason and unavailable_reason.strip())
    responses = []
    for state in states:
        metadata = state.provenance or {}
        config = metadata.get("collection_config") or {}
        kind = metadata.get("requirement_kind", "document")
        validated = None if unavailable else validate_answer(kind, config, value)
        previous = (
            await db.execute(
                select(ApplicationRequirementResponse)
                .where(
                    ApplicationRequirementResponse.requirement_state_id == state.id,
                    ApplicationRequirementResponse.review_state == "accepted",
                )
                .order_by(ApplicationRequirementResponse.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        conflict = bool(
            previous and previous.value.get("answer") != validated and kind != "consent"
        )
        if kind == "field" and not unavailable:
            field_key = (
                config.get("field_key") or metadata.get("canonical_key") or state.requirement_key
            )
            accepted_facts = list(
                (
                    await db.execute(
                        select(ApplicationExtractedFact).where(
                            ApplicationExtractedFact.profile_id == profile.id,
                            ApplicationExtractedFact.field_key == field_key,
                            ApplicationExtractedFact.status == "accepted",
                        )
                    )
                )
                .scalars()
                .all()
            )
            conflict = conflict or any(
                (fact.value or {}).get("value") != validated for fact in accepted_facts
            )
        requires_review = state.verification_required or conflict or unavailable
        response = ApplicationRequirementResponse(
            profile_id=profile.id,
            requirement_state_id=state.id,
            value={"answer": validated}
            if not unavailable
            else {"unavailable_reason": unavailable_reason.strip()[:2000]},
            source=source,
            source_version=str(metadata.get("definition_version") or "1"),
            review_state="unavailable"
            if unavailable
            else "conflict"
            if conflict
            else "submitted"
            if requires_review
            else "accepted",
            submitted_by_user_id=user_id,
            reviewed_by_user_id=user_id if source == "staff" and not requires_review else None,
            reviewed_at=timestamp() if not requires_review else None,
            provenance={
                **(provenance or {}),
                "definition_id": metadata.get("definition_id"),
                **(
                    {
                        "disclosure_text": config.get("disclosure_text"),
                        "disclosure_version": config.get("disclosure_version"),
                        "disclosure_hash": disclosure_hash(config),
                        "scope": config.get("scope"),
                    }
                    if kind == "consent"
                    else {}
                ),
            },
        )
        db.add(response)
        await db.flush()
        responses.append(response)
        if not requires_review and kind == "field":
            await persist_response_fact(db, profile, state, response, reviewer_id=user_id)
        await materialize_response_state(db, state, {state.id: response})
    task.status = (
        "unavailable"
        if unavailable
        else "in_review"
        if any(row.review_state != "accepted" for row in responses)
        else "completed"
    )
    task.assignee = "internal" if task.status in {"unavailable", "in_review"} else task.assignee
    task.state_reason = (
        "Response conflicts with accepted information; staff review required"
        if any(row.review_state == "conflict" for row in responses)
        else unavailable_reason
    )
    task.next_attempt_at = None
    task.completed_at = timestamp() if task.status == "completed" else None
    await db.flush()
    return responses


async def persist_response_fact(
    db: AsyncSession,
    profile: ApplicationProfile,
    state: ApplicationRequirementState,
    response: ApplicationRequirementResponse,
    *,
    reviewer_id: uuid.UUID | None,
) -> None:
    metadata = state.provenance or {}
    if metadata.get("requirement_kind") != "field":
        return
    config = metadata.get("collection_config") or {}
    field_key = str(
        config.get("field_key") or metadata.get("canonical_key") or state.requirement_key
    )
    if len(field_key) > 64:
        raise HTTPException(409, "The configured field key exceeds the canonical field limit")
    value = response.value.get("answer")
    prior = list(
        (
            await db.execute(
                select(ApplicationExtractedFact).where(
                    ApplicationExtractedFact.profile_id == profile.id,
                    ApplicationExtractedFact.field_key == field_key,
                    ApplicationExtractedFact.status == "accepted",
                )
            )
        )
        .scalars()
        .all()
    )
    for fact in prior:
        if (fact.value or {}).get("value") == value and fact.reviewed_by_user_id == reviewer_id:
            return
        if reviewer_id is not None:
            fact.status = "superseded"
    db.add(
        ApplicationExtractedFact(
            profile_id=profile.id,
            field_key=field_key,
            value={"value": value},
            normalized_value=str(value),
            extraction_method="collection_response",
            status="accepted",
            reviewed_at=timestamp(),
            reviewed_by_user_id=reviewer_id,
        )
    )


async def materialize_external_requirements(
    db: AsyncSession,
    profile: ApplicationProfile,
    requirements: list[dict],
    source_key: str,
    approved: bool,
) -> list[ApplicationCollectionTask]:
    """Register a versioned lender checklist; unapproved proposals stay internal.

    Caller must persist source_key/version and enforce offering authorization.
    The same shared requirement key can be referenced by multiple offerings.
    """
    states = list(
        (
            await db.execute(
                select(ApplicationRequirementState).where(
                    ApplicationRequirementState.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_key = {row.requirement_key: row for row in states}
    responses = await latest_responses(db, profile.id)
    for definition in requirements:
        key = str(definition["requirement_key"])
        if not re.fullmatch(r"[a-z0-9_]{2,120}", key):
            raise HTTPException(422, "Invalid lender requirement key")
        kind = definition.get("requirement_kind", "document")
        if kind not in KINDS:
            raise HTTPException(422, "Invalid lender requirement kind")
        state = by_key.get(key)
        metadata = dict(state.provenance or {}) if state else {}
        if state is None:
            state = ApplicationRequirementState(
                profile_id=profile.id,
                requirement_key=key,
                label=definition.get("label", key),
                category=definition.get("category", "financials"),
                required_level=definition.get("required_level", "required"),
                status="missing",
                source_program_keys=[],
                source_policy_keys=[],
                verification_required=bool(definition.get("verification_required", False)),
            )
            db.add(state)
            states.append(state)
            by_key[key] = state
            await db.flush()
        # Program-owned definitions keep their approved source and validation.
        if metadata and metadata.get("source_kind") != "lender":
            continue
        metadata.update(
            {
                "source_kind": "lender",
                "source_key": source_key,
                "catalog_approved": approved,
                "requirement_kind": kind,
                "canonical_key": definition.get("canonical_key") or key,
                "collection_config": definition.get("collection_config") or {},
                "definition_version": str(definition.get("version") or "1"),
                "definition_id": f"{source_key}:{key}",
                "client_visible": "borrower"
                in definition.get("visibility", ["borrower", "underwriter"]),
                "objective_text": definition.get("objective_text") or definition.get("description"),
                "completion_criteria": definition.get("completion_criteria", ""),
                "external_definition": definition,
            }
        )
        state.provenance = metadata
        await materialize_response_state(db, state, responses)
    return await sync_collection_tasks(db, profile, states)


async def outreach_tasks(
    db: AsyncSession, profile_id: uuid.UUID, *, requirement_keys: list[str] | None = None
) -> list[ApplicationCollectionTask]:
    rows = list(
        (
            await db.execute(
                select(ApplicationCollectionTask).where(
                    ApplicationCollectionTask.profile_id == profile_id,
                    ApplicationCollectionTask.assignee == "borrower",
                    ApplicationCollectionTask.automation_approved.is_(True),
                    ApplicationCollectionTask.status.in_(OPEN_TASKS),
                    ApplicationCollectionTask.attempts < ApplicationCollectionTask.max_attempts,
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        row
        for row in rows
        if requirement_keys is None or set(row.requirement_keys).intersection(requirement_keys)
    ]


async def record_outreach(
    db: AsyncSession, profile_id: uuid.UUID, requirement_keys: list[str], *, successful: bool
) -> None:
    for task in await outreach_tasks(db, profile_id, requirement_keys=requirement_keys):
        task.attempts += 1
        task.last_requested_at = timestamp()
        task.next_attempt_at = timestamp() + timedelta(hours=max(task.cadence_hours, 24))
        task.status = "requested"
        if task.attempts >= task.max_attempts:
            task.status, task.assignee, task.next_attempt_at = "escalated", "internal", None
            task.state_reason = "Automatic outreach limit reached; staff follow-up required"
        elif not successful:
            task.state_reason = "Delivery failed; the next scheduled attempt will retry"
    await db.flush()


def task_read(task: ApplicationCollectionTask, *, public: bool = False) -> dict:
    config = dict(task.config or {})
    if public:
        config = {
            key: value
            for key, value in config.items()
            if key
            in {
                "requirement_kind",
                "value_type",
                "choices",
                "min",
                "max",
                "max_length",
                "required",
                "fields",
                "form_url",
                "form_kind",
                "disclosure_text",
                "disclosure_version",
                "requested_document_ids",
                "prefill",
            }
        }
    return {
        "id": str(task.id),
        "label": task.label,
        "description": task.description,
        "action_type": task.action_type,
        "status": task.status,
        "assignee": task.assignee,
        "config": config,
        "state_reason": task.state_reason,
        **(
            {}
            if public
            else {
                "requirement_keys": task.requirement_keys,
                "automation_approved": task.automation_approved,
                "attempts": task.attempts,
                "max_attempts": task.max_attempts,
                "next_attempt_at": task.next_attempt_at,
                "assigned_user_id": str(task.assigned_user_id) if task.assigned_user_id else None,
            }
        ),
    }
