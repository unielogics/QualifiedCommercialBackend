"""Staff collection workbench and token/PIN scoped borrower answers."""
# ruff: noqa: B008

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationRequirementResponse,
    ApplicationRequirementState,
)
from app.routers.application_profiles import _public_application_room, _require_underwriting_actor
from app.services import application_profiles as profiles
from app.services import application_programs, collection_tasks, missing_item_automation

router = APIRouter(tags=["Application collection"])


class TaskAnswer(BaseModel):
    value: Any = None
    unavailable_reason: str | None = Field(default=None, min_length=2, max_length=2000)


class RoomTaskAnswer(TaskAnswer):
    passcode: str = Field(min_length=4, max_length=32)


class RoomTaskAccess(BaseModel):
    passcode: str = Field(min_length=4, max_length=32)


class TaskPatch(BaseModel):
    status: Literal["open", "paused", "completed"] | None = None
    assignee: Literal["borrower", "internal"] | None = None
    assigned_user_id: UUID | None = None
    description: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=2000)


class ResponseReview(BaseModel):
    disposition: Literal["accepted", "needs_more"]
    reason: str = Field(min_length=3, max_length=2000)


async def _task(db: AsyncSession, profile_id: UUID, task_id: UUID) -> ApplicationCollectionTask:
    row = await db.get(ApplicationCollectionTask, task_id)
    if not row or row.profile_id != profile_id:
        raise HTTPException(404, "Collection action not found")
    return row


async def _rows(db: AsyncSession, profile, *, public: bool = False) -> list[dict]:
    await application_programs.get_program_readiness(db, profile)
    rows = list(
        (
            await db.execute(
                select(ApplicationCollectionTask)
                .where(
                    ApplicationCollectionTask.profile_id == profile.id,
                    ApplicationCollectionTask.status != "cancelled",
                )
                .order_by(ApplicationCollectionTask.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [
        collection_tasks.task_read(row, public=public)
        for row in rows
        if not public
        or row.automation_approved
        and row.action_type != "internal_review"
        and (
            row.assignee == "borrower"
            or row.status in {"in_review", "unavailable", "escalated", "completed"}
        )
        and row.status != "paused"
    ]


async def deliver_due_profile(profile_id: UUID) -> None:
    """Fresh transaction for after-commit immediate initial delivery."""
    from app.db import SessionLocal

    async with SessionLocal() as db:
        profile = await db.get(collection_tasks.ApplicationProfile, profile_id)
        if profile:
            await missing_item_automation.run_profile_collection_outreach(db, profile)
            await db.commit()


@router.get("/application-profiles/{profile_id}/collection-tasks")
async def list_collection_tasks(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await _rows(db, profile)
    await db.commit()
    return {"tasks": rows}


@router.post("/application-profiles/{profile_id}/collection-tasks/collect-missing")
async def collect_missing(
    profile_id: UUID,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    rows = await _rows(db, profile)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "collection.requested",
        "Consolidated configured missing items into collection tasks",
    )
    await db.commit()
    background_tasks.add_task(deliver_due_profile, profile.id)
    return {"tasks": rows, "outreach": "scheduled"}


@router.patch("/application-profiles/{profile_id}/collection-tasks/{task_id}")
async def update_collection_task(
    profile_id: UUID,
    task_id: UUID,
    payload: TaskPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    task = await _task(db, profile.id, task_id)
    if payload.status == "completed" and task.action_type != "internal_review":
        raise HTTPException(422, "Complete the requirement response or verify its evidence")
    if payload.assignee == "borrower" and not task.automation_approved:
        raise HTTPException(
            409, "Unapproved or internal requirements cannot be assigned to the borrower"
        )
    if "assigned_user_id" in payload.model_fields_set and payload.assigned_user_id not in {
        None,
        user.id,
    }:
        raise HTTPException(422, "Use Take over to assign this task to yourself")
    for key in ("status", "assignee", "description", "assigned_user_id"):
        if key in payload.model_fields_set:
            setattr(task, key, getattr(payload, key))
    if payload.assignee == "internal":
        task.assigned_user_id = user.id
    if "description" in payload.model_fields_set:
        task.config = {**(task.config or {}), "request_message_override": payload.description}
    if payload.reason:
        task.state_reason = payload.reason
    if task.status == "open":
        task.attempts, task.next_attempt_at = 0, collection_tasks.timestamp()
    if task.status == "completed":
        task.completed_at = collection_tasks.timestamp()
        states = list(
            (
                await db.execute(
                    select(ApplicationRequirementState).where(
                        ApplicationRequirementState.profile_id == profile.id,
                        ApplicationRequirementState.id.in_(
                            [UUID(key) for key in task.requirement_state_ids]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for state in states:
            state.status, state.verified_at, state.verified_by_user_id = (
                "verified",
                collection_tasks.timestamp(),
                user.id,
            )
            state.state_reason = payload.reason or "Internal review completed"
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "collection.updated",
        payload.reason or f"Collection action {task.status}",
        target_type="collection_task",
        target_id=task.id,
    )
    await db.commit()
    return collection_tasks.task_read(task)


@router.post("/application-profiles/{profile_id}/collection-tasks/{task_id}/answer")
async def answer_staff_collection_task(
    profile_id: UUID,
    task_id: UUID,
    payload: TaskAnswer,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    task = await _task(db, profile.id, task_id)
    await collection_tasks.answer_task(
        db,
        profile,
        task,
        value=payload.value,
        unavailable_reason=payload.unavailable_reason,
        source="staff",
        user_id=user.id,
    )
    rows = await _rows(db, profile)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "collection.answered",
        f"Answered {task.label}",
        target_type="collection_task",
        target_id=task.id,
    )
    await db.commit()
    return {"tasks": rows}


@router.post("/application-profiles/{profile_id}/requirement-responses/{response_id}/review")
async def review_collection_response(
    profile_id: UUID,
    response_id: UUID,
    payload: ResponseReview,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _require_underwriting_actor(user)
    profile = await profiles.load_profile(db, profile_id, user)
    response = await db.get(ApplicationRequirementResponse, response_id)
    if not response or response.profile_id != profile.id:
        raise HTTPException(404, "Response not found")
    state = await db.get(ApplicationRequirementState, response.requirement_state_id)
    if (
        not state
        or (await collection_tasks.latest_responses(db, profile.id)).get(state.id).id != response.id
    ):
        raise HTTPException(409, "Review the latest response")
    if payload.disposition == "accepted":
        metadata = state.provenance or {}
        if response.source_version != str(
            metadata.get("definition_version") or "1"
        ) or response.provenance.get("definition_id") != metadata.get("definition_id"):
            raise HTTPException(409, "The requirement changed; request a current response")
        if metadata.get("requirement_kind") == "consent" and response.provenance.get(
            "disclosure_hash"
        ) != collection_tasks.disclosure_hash(metadata.get("collection_config") or {}):
            raise HTTPException(409, "The consent disclosure changed; request fresh consent")
        collection_tasks.validate_answer(
            (state.provenance or {}).get("requirement_kind", "document"),
            (state.provenance or {}).get("collection_config") or {},
            response.value.get("answer"),
        )
    response.review_state, response.reviewed_by_user_id, response.reviewed_at = (
        payload.disposition,
        user.id,
        collection_tasks.timestamp(),
    )
    response.provenance = {**(response.provenance or {}), "review_reason": payload.reason}
    if payload.disposition == "accepted":
        await collection_tasks.persist_response_fact(
            db, profile, state, response, reviewer_id=user.id
        )
    await collection_tasks.materialize_response_state(db, state, {state.id: response})
    rows = await _rows(db, profile)
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "collection.response_reviewed",
        payload.reason,
        target_type="requirement_response",
        target_id=response.id,
    )
    await db.commit()
    return {"tasks": rows}


@router.post("/application-rooms/{token}/collection-tasks/{task_id}/form-link")
async def open_public_collection_form(
    token: str,
    task_id: UUID,
    payload: RoomTaskAccess,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.config import get_settings
    from app.services import financial_statements, pfs_schema
    from app.services.arf_channel import profile_uses_arf, require_hosted_permission

    _link, profile = await _public_application_room(
        db, token, payload.passcode, request, allow_dealer=True
    )
    task = await _task(db, profile.id, task_id)
    if (
        not task.automation_approved
        or task.action_type != "complete_form"
        or task.status not in {"open", "requested", "escalated"}
    ):
        raise HTTPException(404, "Form collection action not found")
    if task.assignee != "borrower" and task.status != "escalated":
        raise HTTPException(404, "Form collection action not found")
    kind = (task.config or {}).get("form_kind")
    if kind not in {"pfs", "debt_schedule", "p_and_l", "balance_sheet"}:
        raise HTTPException(409, "Use the approved lender form and upload its completed copy")
    if await profile_uses_arf(db, profile):
        await require_hosted_permission(db, requested_documents=task.requirement_keys)
    statement_id = None
    if kind == "pfs":
        statement = await financial_statements.latest_for_profile(db, profile.id)
        if statement is None:
            statement = await financial_statements.save_statement(
                db, profile, body=pfs_schema.empty_body()
            )
        statement_id = statement.id
    link, form_token = await financial_statements.mint_link(
        db, profile, kind=kind, statement_id=statement_id, ttl_days=1
    )
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "collection.form_opened",
        f"Opened the requested {kind} form",
        target_type="collection_task",
        target_id=task.id,
    )
    await db.commit()
    return {
        "url": f"{get_settings().frontend_app_url.rstrip('/')}/forms/{kind}/{form_token}",
        "expires_at": link.expires_at,
    }


@router.post("/application-rooms/{token}/collection-tasks")
async def public_collection_tasks(
    token: str, payload: RoomTaskAccess, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _link, profile = await _public_application_room(
        db, token, payload.passcode, request, allow_dealer=True
    )
    rows = await _rows(db, profile, public=True)
    await db.commit()
    return {"tasks": rows}


@router.post("/application-rooms/{token}/collection-tasks/{task_id}/answer")
async def answer_public_collection_task(
    token: str,
    task_id: UUID,
    payload: RoomTaskAnswer,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _link, profile = await _public_application_room(
        db, token, payload.passcode, request, allow_dealer=True
    )
    task = await _task(db, profile.id, task_id)
    await collection_tasks.answer_task(
        db,
        profile,
        task,
        value=payload.value,
        unavailable_reason=payload.unavailable_reason,
        provenance={
            "ip_address": request.client.host if request.client else None,
            "user_agent": (request.headers.get("user-agent") or "")[:400],
        },
    )
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "collection.borrower_answered",
        f"Secure room response: {task.label}",
        target_type="collection_task",
        target_id=task.id,
    )
    rows = await _rows(db, profile, public=True)
    await db.commit()
    return {"tasks": rows}
