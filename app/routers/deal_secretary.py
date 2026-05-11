"""AI Deal Secretary router — assign/unassign tasks, edit assignments,
set file-level outreach mode, buffer wizard intent, run bootstrap repair.

Endpoint surface (all mounted at /api/v1 by main.py):

  GET    /loans/{loan_id}/deal-secretary
         The two-column view. Joins ClientRequirementStatus ⋈
         AICollectionRequirement ⋈ AITaskAssignment in one round trip.

  POST   /loans/{loan_id}/deal-secretary/assign
         Move a requirement to the AI column. Creates an
         AITaskAssignment with catalog-default channels/cadence/
         objective/completion_mode unless overridden in the payload.

  POST   /loans/{loan_id}/deal-secretary/unassign
         Move a requirement back to the human column. Soft-deletes
         the AITaskAssignment.

  PATCH  /loans/{loan_id}/deal-secretary/assignments/{assignment_id}
         Edit instructions/channels/cadence/approval_mode/objective/
         completion criteria/link/complete_file_by on an existing
         AITaskAssignment.

  PATCH  /loans/{loan_id}/deal-secretary/file-settings
         Set the file-level OutreachMode kill switch + supporting
         config (complete_file_by, sms_consent, email_opt_out).

  POST   /loans/{loan_id}/deal-secretary/bootstrap
         Idempotent repair endpoint. Reruns
         bootstrap_requirement_status_rows() on an existing loan.

  POST   /clients/{client_id}/deal-secretary/wizard-intent
         Buffer pre-loan wizard intent on the Client-stage ClientAIPlan
         (loan_id IS NULL). Materializes into real AITaskAssignment
         rows when the loan is later created from the prequal.

Permissioning:
  • SUPER_ADMIN / LOAN_EXEC may do anything on any loan.
  • BROKER may operate on loans they own (broker_id matches).
  • CLIENT is denied.

Funding-locked items (can_agent_override=false on the catalog row)
are server-enforced: an agent trying to /assign a funding-locked
requirement gets a 403. Underwriters can.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.deps import CurrentUser, get_db
from app.enums import Role, TaskOwnerType
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.ai_task_assignment import AITaskAssignment
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.loan import Loan
from app.schemas.deal_secretary import (
    AnyOk,
    AssignRequest,
    AssignmentUpdateRequest,
    BootstrapResponse,
    DealSecretaryView,
    FileSettings,
    FileSettingsUpdate,
    OutreachModeLiteral,
    TaskRow,
    UnassignRequest,
    WizardIntentRequest,
    WizardIntentResponse,
)
from app.services.ai.deal_secretary import (
    bootstrap_requirement_status_rows,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["deal-secretary"])


# ── Pipeline summary (Phase 3) ─────────────────────────────────────


from pydantic import BaseModel


class PipelineSummaryItem(BaseModel):
    loan_id: UUID
    outreach_mode: str
    ai_task_count: int
    ai_completed_count: int
    blocked_count: int
    next_outreach_at: datetime | None
    current_blocker: str | None
    state: str  # setup | active_work | waiting_borrower | blocked


@router.get(
    "/pipeline/deal-secretary-summary",
    response_model=list[PipelineSummaryItem],
)
async def pipeline_summary(
    loan_ids: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PipelineSummaryItem]:
    """Per-loan summary for pipeline badges. Single round-trip for the
    whole visible page — no N+1.

    `loan_ids` is a comma-separated UUID list (query param)."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Borrowers cannot see this surface")

    ids: list[UUID] = []
    for raw in loan_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.append(UUID(raw))
        except ValueError:
            continue
    if not ids:
        return []

    # Broker scope: filter to their own loans.
    loan_rows = (
        await db.execute(
            select(Loan).where(Loan.id.in_(ids))
        )
    ).scalars().all()
    if user.role == Role.BROKER:
        if user.broker is None:
            return []
        loan_rows = [l for l in loan_rows if l.broker_id == user.broker.id]
    visible_ids = {l.id for l in loan_rows}

    if not visible_ids:
        return []

    # Pull CRS + ClientAIPlan for all loans in one query each.
    crs_rows = (
        await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.loan_id.in_(visible_ids),
            )
        )
    ).scalars().all()
    plan_rows = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.loan_id.in_(visible_ids),
            )
        )
    ).scalars().all()
    plans_by_loan = {p.loan_id: p for p in plan_rows}

    # Pull live assignments (un-deleted).
    assignment_ids = [r.ai_assignment_id for r in crs_rows if r.ai_assignment_id]
    assignments_by_id: dict[UUID, AITaskAssignment] = {}
    if assignment_ids:
        assignment_rows = (
            await db.execute(
                select(AITaskAssignment).where(
                    AITaskAssignment.id.in_(assignment_ids),
                    AITaskAssignment.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        assignments_by_id = {a.id: a for a in assignment_rows}

    # Aggregate per loan.
    result: list[PipelineSummaryItem] = []
    for loan in loan_rows:
        plan = plans_by_loan.get(loan.id)
        settings = dict(plan.ai_secretary_settings or {}) if plan else {}
        outreach_mode = settings.get("outreach_mode", "draft_first")

        ai_rows = [r for r in crs_rows if r.loan_id == loan.id and r.owner_type == TaskOwnerType.AI.value]
        ai_count = len(ai_rows)
        completed = len([r for r in ai_rows if r.status in ("verified", "waived", "not_applicable")])
        # Blocked = attempts maxed out OR status conflicted/stalled.
        blocked = 0
        blocker_label: str | None = None
        soonest: datetime | None = None
        catalog_labels = await _labels_for_keys(db, [r.requirement_key for r in ai_rows])
        for r in ai_rows:
            a = assignments_by_id.get(r.ai_assignment_id) if r.ai_assignment_id else None
            if r.status in ("conflicted", "stalled"):
                blocked += 1
                if blocker_label is None:
                    blocker_label = catalog_labels.get(r.requirement_key, r.requirement_key)
            elif a is not None:
                max_attempts = int((a.cadence or {}).get("max_attempts", 3))
                if a.attempts_made >= max_attempts:
                    blocked += 1
                    if blocker_label is None:
                        blocker_label = catalog_labels.get(r.requirement_key, r.requirement_key)
                if a.next_run_at is not None:
                    if soonest is None or a.next_run_at < soonest:
                        soonest = a.next_run_at

        if blocked > 0:
            state = "blocked"
        elif ai_count > 0 and outreach_mode != "off":
            # Are any waiting on borrower?
            waiting = any(r.status in ("asked", "waiting_on_borrower") for r in ai_rows)
            state = "waiting_borrower" if waiting else "active_work"
        else:
            state = "setup"

        result.append(PipelineSummaryItem(
            loan_id=loan.id,
            outreach_mode=outreach_mode,
            ai_task_count=ai_count,
            ai_completed_count=completed,
            blocked_count=blocked,
            next_outreach_at=soonest,
            current_blocker=blocker_label,
            state=state,
        ))
    return result


async def _labels_for_keys(db: AsyncSession, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = (
        await db.execute(
            select(AICollectionRequirement.requirement_key, AICollectionRequirement.label)
            .where(AICollectionRequirement.requirement_key.in_(keys))
        )
    ).all()
    out: dict[str, str] = {}
    for k, lbl in rows:
        if k not in out:
            out[k] = lbl
    return out


# ── helpers ─────────────────────────────────────────────────────────


async def _resolve_loan_and_gate(
    loan_id: UUID, user, db: AsyncSession
) -> Loan:
    """Load the loan + enforce role-based access. Borrowers cannot use
    the workbench at all (the assignment surface is operator-only).
    Brokers must own the loan."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Borrowers cannot manage the AI Workbench")
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    if user.role == Role.BROKER:
        if user.broker is None or loan.broker_id != user.broker.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Loan belongs to another agent")
    return loan


def _is_operator(user) -> bool:
    return user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file_settings_from_jsonb(raw: dict[str, Any] | None) -> FileSettings:
    """Tolerant deserializer — ai_secretary_settings JSONB might have
    extra/unknown keys from older writes; pydantic ignores them when
    we pass a dict in."""
    raw = dict(raw or {})
    raw.pop("pending_assignments", None)  # not part of FileSettings shape
    return FileSettings(**{k: v for k, v in raw.items() if v is not None})


# ── GET combined view ──────────────────────────────────────────────


@router.get(
    "/loans/{loan_id}/deal-secretary",
    response_model=DealSecretaryView,
)
async def get_deal_secretary(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealSecretaryView:
    """Two-column view + file settings + funding-locked count."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)

    # CRS ⋈ assignment.  Load the catalog row separately (one
    # roundtrip per requirement_key seen — fine for the ~10-15
    # rows typically on a deal).
    crs_rows = (
        await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.client_id == loan.client_id,
                ClientRequirementStatus.loan_id == loan.id,
            )
        )
    ).scalars().all()

    assignment_ids = [r.ai_assignment_id for r in crs_rows if r.ai_assignment_id]
    assignments_by_id: dict[UUID, AITaskAssignment] = {}
    if assignment_ids:
        assignments = (
            await db.execute(
                select(AITaskAssignment).where(
                    AITaskAssignment.id.in_(assignment_ids),
                    AITaskAssignment.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        assignments_by_id = {a.id: a for a in assignments}

    # Catalog lookup. We resolve via requirement_key on whatever
    # AICollectionRequirement row exists for the platform/funding
    # playbook — multiple playbooks may have the same key, so pick
    # the highest-priority row (funding > platform > agent).
    keys = list({r.requirement_key for r in crs_rows})
    catalog_by_key: dict[str, AICollectionRequirement] = {}
    if keys:
        cat_rows = (
            await db.execute(
                select(AICollectionRequirement)
                .options(selectinload(AICollectionRequirement.playbook))
                .where(AICollectionRequirement.requirement_key.in_(keys))
            )
        ).scalars().all()
        priority = {"funding": 3, "platform": 2, "agent": 1}
        for cat in cat_rows:
            existing = catalog_by_key.get(cat.requirement_key)
            owner = cat.playbook.owner_type if cat.playbook else "platform"
            if existing is None:
                catalog_by_key[cat.requirement_key] = cat
                continue
            existing_owner = existing.playbook.owner_type if existing.playbook else "platform"
            if priority.get(owner, 0) > priority.get(existing_owner, 0):
                catalog_by_key[cat.requirement_key] = cat

    left: list[TaskRow] = []
    right: list[TaskRow] = []
    funding_locked = 0

    for crs in crs_rows:
        cat = catalog_by_key.get(crs.requirement_key)
        assignment = (
            assignments_by_id.get(crs.ai_assignment_id)
            if crs.ai_assignment_id else None
        )
        row = _build_task_row(crs, cat, assignment)
        if row.owner_type == TaskOwnerType.AI.value:
            right.append(row)
        else:
            left.append(row)
        if row.owner_type == TaskOwnerType.FUNDING_LOCKED.value:
            funding_locked += 1

    # Stable order: display_order first, then label.
    left.sort(key=lambda r: (r.display_order, r.label))
    right.sort(key=lambda r: (r.display_order, r.label))

    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    file_settings = _file_settings_from_jsonb(
        plan.ai_secretary_settings if plan else None
    )

    return DealSecretaryView(
        loan_id=loan.id,
        client_id=loan.client_id,
        left=left,
        right=right,
        file_settings=file_settings,
        funding_locked_count=funding_locked,
    )


def _build_task_row(
    crs: ClientRequirementStatus,
    cat: AICollectionRequirement | None,
    assignment: AITaskAssignment | None,
) -> TaskRow:
    """Combine the three rows into the TaskRow shape the picker
    renders. Catalog defaults supply objective/completion/link when
    the assignment doesn't override."""
    # Effective link prefers assignment override → catalog default.
    link_url = (assignment.link_url if assignment else None) or (cat.link_url if cat else None)
    link_label = (assignment.link_label if assignment else None) or (cat.link_label if cat else None)
    link_kind = (assignment.link_kind if assignment else None) or (cat.link_kind if cat else None)

    objective = (
        (assignment.objective_text if assignment else None)
        or (cat.objective_text if cat else "")
        or ""
    )
    completion_criteria = (
        (assignment.completion_criteria if assignment else None)
        or (cat.completion_criteria if cat else "")
        or ""
    )
    completion_mode = (
        (assignment.completion_mode if assignment else None)
        or (cat.completion_mode if cat else "ai_can_complete")
        or "ai_can_complete"
    )

    return TaskRow(
        client_requirement_status_id=crs.id,
        requirement_key=crs.requirement_key,
        label=cat.label if cat else crs.requirement_key,
        category=cat.category if cat else "borrower_info",  # type: ignore[arg-type]
        required_level=cat.required_level if cat else "required",  # type: ignore[arg-type]
        status=crs.status,
        owner_type=crs.owner_type,  # type: ignore[arg-type]
        source=crs.source,
        objective_text=objective,
        completion_criteria=completion_criteria,
        completion_mode=completion_mode,  # type: ignore[arg-type]
        visibility=list(cat.visibility) if cat and cat.visibility else [],
        can_agent_override=cat.can_agent_override if cat else False,
        can_underwriter_waive=cat.can_underwriter_waive if cat else True,
        blocks_stage=cat.blocks_stage if cat else None,
        display_order=cat.display_order if cat else 0,
        link_url=link_url,
        link_label=link_label,
        link_kind=link_kind,  # type: ignore[arg-type]
        due_at=crs.due_at,
        last_requested_at=crs.last_requested_at,
        last_response_at=crs.last_response_at,
        assignment_id=assignment.id if assignment else None,
        instructions=assignment.instructions if assignment else None,
        instructions_visibility=assignment.instructions_visibility if assignment else None,
        channels=list(assignment.channels) if assignment and assignment.channels else None,  # type: ignore[arg-type]
        cadence=assignment.cadence if assignment else None,  # type: ignore[arg-type]
        approval_mode=assignment.approval_mode if assignment else None,  # type: ignore[arg-type]
        complete_file_by=assignment.complete_file_by if assignment else None,
        consent_state=assignment.consent_state if assignment else None,  # type: ignore[arg-type]
        attempts_made=assignment.attempts_made if assignment else None,
        next_run_at=assignment.next_run_at if assignment else None,
    )


# ── POST /assign ───────────────────────────────────────────────────


@router.post(
    "/loans/{loan_id}/deal-secretary/assign",
    response_model=TaskRow,
)
async def assign_to_ai(
    loan_id: UUID,
    payload: AssignRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaskRow:
    """Move a CRS row to owner_type='ai' and create an AITaskAssignment."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)

    crs = (
        await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.client_id == loan.client_id,
                ClientRequirementStatus.loan_id == loan.id,
                ClientRequirementStatus.requirement_key == payload.requirement_key,
            )
        )
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Requirement {payload.requirement_key!r} not found on this loan",
        )

    # Catalog lookup for defaults + override gate.
    cat = (
        await db.execute(
            select(AICollectionRequirement)
            .options(selectinload(AICollectionRequirement.playbook))
            .where(AICollectionRequirement.requirement_key == payload.requirement_key)
        )
    ).scalars().first()

    # Funding-locked items: only an operator may assign. Agent gets 403.
    if cat is not None and not cat.can_agent_override and not _is_operator(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This requirement is locked by the funding team and cannot be reassigned.",
        )

    # Already AI? Treat as PATCH so the front-end can use /assign as
    # an idempotent "ensure AI ownership" call.
    existing = None
    if crs.ai_assignment_id:
        existing = (
            await db.execute(
                select(AITaskAssignment).where(
                    AITaskAssignment.id == crs.ai_assignment_id,
                    AITaskAssignment.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    if existing is not None:
        # Apply any overrides from the payload.
        _apply_assignment_overrides(existing, payload, cat)
        _sync_assignment_schedule(existing, crs)
        crs.owner_type = TaskOwnerType.AI.value
        await db.flush()
        await db.refresh(crs)
        await db.refresh(existing)
        return _build_task_row(crs, cat, existing)

    assignment = AITaskAssignment(
        id=uuid.uuid4(),
        client_requirement_status_id=crs.id,
        owner_type=TaskOwnerType.AI.value,
        created_by=user.id,
    )
    _apply_assignment_overrides(assignment, payload, cat)
    _sync_assignment_schedule(assignment, crs)
    db.add(assignment)
    await db.flush()

    crs.owner_type = TaskOwnerType.AI.value
    crs.ai_assignment_id = assignment.id
    await db.flush()
    await db.refresh(crs)
    await db.refresh(assignment)
    log.info(
        "deal_secretary.assign loan_id=%s req=%s assignment=%s user=%s",
        loan.id, payload.requirement_key, assignment.id, user.id,
    )
    return _build_task_row(crs, cat, assignment)


def _apply_assignment_overrides(
    assignment: AITaskAssignment,
    payload: AssignRequest | AssignmentUpdateRequest,
    cat: AICollectionRequirement | None,
) -> None:
    """Apply payload fields (None = keep existing). For a brand-new
    assignment, falls back to catalog defaults so the row is
    immediately usable by the cadence engine."""
    if isinstance(payload, AssignRequest):
        # New assignment defaults — catalog → payload → field default.
        if payload.instructions is not None:
            assignment.instructions = payload.instructions
        elif not assignment.instructions:
            assignment.instructions = ""
        assignment.instructions_visibility = payload.instructions_visibility or "agent"
        if payload.channels is not None:
            assignment.channels = list(payload.channels)
        elif cat is not None and cat.default_channels:
            assignment.channels = list(cat.default_channels)
        elif not assignment.channels:
            assignment.channels = ["portal"]
        if payload.cadence is not None:
            assignment.cadence = payload.cadence.model_dump(exclude_none=True)
        elif cat is not None:
            assignment.cadence = {
                "hours_between_attempts": cat.default_cadence_hours,
                "max_attempts": 3,
            }
        assignment.approval_mode = payload.approval_mode or "draft_first"
        assignment.complete_file_by = payload.complete_file_by
        assignment.link_url = payload.link_url or (cat.link_url if cat else None)
        assignment.link_label = payload.link_label or (cat.link_label if cat else None)
        assignment.link_kind = payload.link_kind or (cat.link_kind if cat else None)
        if payload.objective_text is not None:
            assignment.objective_text = payload.objective_text
        if payload.completion_criteria is not None:
            assignment.completion_criteria = payload.completion_criteria
        if payload.completion_mode is not None:
            assignment.completion_mode = payload.completion_mode
        return

    # PATCH semantics — None means "keep current".
    if payload.instructions is not None:
        assignment.instructions = payload.instructions
    if payload.instructions_visibility is not None:
        assignment.instructions_visibility = payload.instructions_visibility
    if payload.channels is not None:
        assignment.channels = list(payload.channels)
    if payload.cadence is not None:
        assignment.cadence = payload.cadence.model_dump(exclude_none=True)
    if payload.approval_mode is not None:
        assignment.approval_mode = payload.approval_mode
    if payload.complete_file_by is not None:
        assignment.complete_file_by = payload.complete_file_by
    if payload.link_url is not None:
        assignment.link_url = payload.link_url
    if payload.link_label is not None:
        assignment.link_label = payload.link_label
    if payload.link_kind is not None:
        assignment.link_kind = payload.link_kind
    if payload.objective_text is not None:
        assignment.objective_text = payload.objective_text
    if payload.completion_criteria is not None:
        assignment.completion_criteria = payload.completion_criteria
    if payload.completion_mode is not None:
        assignment.completion_mode = payload.completion_mode
    assignment.updated_at = _now()


def _sync_assignment_schedule(
    assignment: AITaskAssignment,
    crs: ClientRequirementStatus,
) -> None:
    """Keep the assignment runnable and mirror its deadline onto CRS.

    The CRS row is what the file/workflow UI reads for requirement due
    dates. The assignment row is what the cadence engine reads for next
    consideration. Keeping both aligned makes an AI-owned task behave
    like assigned staff work instead of a passive label.
    """
    if assignment.complete_file_by is None and crs.due_at is not None:
        assignment.complete_file_by = crs.due_at
    if assignment.complete_file_by is not None:
        crs.due_at = assignment.complete_file_by
    if assignment.next_run_at is None:
        assignment.next_run_at = _now()


# ── POST /unassign ─────────────────────────────────────────────────


@router.post(
    "/loans/{loan_id}/deal-secretary/unassign",
    response_model=TaskRow,
)
async def unassign_from_ai(
    loan_id: UUID,
    payload: UnassignRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaskRow:
    """Soft-delete the AI assignment + flip CRS back to owner_type='human'."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)

    crs = (
        await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.client_id == loan.client_id,
                ClientRequirementStatus.loan_id == loan.id,
                ClientRequirementStatus.requirement_key == payload.requirement_key,
            )
        )
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found on this loan")

    cat = (
        await db.execute(
            select(AICollectionRequirement)
            .where(AICollectionRequirement.requirement_key == payload.requirement_key)
        )
    ).scalars().first()

    # Funding-locked items: agent cannot un-lock either.
    if cat is not None and not cat.can_agent_override and not _is_operator(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This requirement is locked by funding and cannot be reassigned.",
        )

    if crs.ai_assignment_id:
        assignment = await db.get(AITaskAssignment, crs.ai_assignment_id)
        if assignment is not None and assignment.deleted_at is None:
            assignment.deleted_at = _now()

    crs.owner_type = TaskOwnerType.HUMAN.value
    crs.ai_assignment_id = None
    await db.flush()
    await db.refresh(crs)
    log.info(
        "deal_secretary.unassign loan_id=%s req=%s user=%s",
        loan.id, payload.requirement_key, user.id,
    )
    return _build_task_row(crs, cat, None)


# ── PATCH /assignments/{id} ────────────────────────────────────────


@router.patch(
    "/loans/{loan_id}/deal-secretary/assignments/{assignment_id}",
    response_model=TaskRow,
)
async def update_assignment(
    loan_id: UUID,
    assignment_id: UUID,
    payload: AssignmentUpdateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaskRow:
    """Edit the per-task config — instructions, channels, cadence,
    approval_mode, link, objective, completion."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)

    assignment = await db.get(AITaskAssignment, assignment_id)
    if assignment is None or assignment.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not found")
    crs = await db.get(ClientRequirementStatus, assignment.client_requirement_status_id)
    if crs is None or crs.loan_id != loan.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Assignment not on this loan")

    cat = (
        await db.execute(
            select(AICollectionRequirement)
            .where(AICollectionRequirement.requirement_key == crs.requirement_key)
        )
    ).scalars().first()
    _apply_assignment_overrides(assignment, payload, cat)
    _sync_assignment_schedule(assignment, crs)
    await db.flush()
    await db.refresh(assignment)
    await db.refresh(crs)
    return _build_task_row(crs, cat, assignment)


# ── PATCH /file-settings ───────────────────────────────────────────


@router.patch(
    "/loans/{loan_id}/deal-secretary/file-settings",
    response_model=FileSettings,
)
async def update_file_settings(
    loan_id: UUID,
    payload: FileSettingsUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileSettings:
    """Set the file-level OutreachMode + supporting config. None on
    any field means "leave it alone"."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)

    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        # Lazy-create — bootstrap should have made this already but
        # the workbench shouldn't error out if it didn't.
        plan = ClientAIPlan(
            id=uuid.uuid4(),
            client_id=loan.client_id,
            loan_id=loan.id,
            agent_id=loan.broker_id,
            current_phase="lending",
            active_playbook_versions=[],
            required_items=[],
            waived_items=[],
            ai_suggested_items=[],
            ai_secretary_settings={"outreach_mode": "draft_first"},
        )
        db.add(plan)
        await db.flush()

    settings = dict(plan.ai_secretary_settings or {})
    # Preserve pending_assignments if the wizard put any there.
    pending = settings.get("pending_assignments")
    updates = payload.model_dump(exclude_none=True)
    settings.update(updates)
    if pending is not None:
        settings["pending_assignments"] = pending
    plan.ai_secretary_settings = settings
    await db.flush()
    log.info(
        "deal_secretary.file_settings loan_id=%s mode=%s user=%s",
        loan.id, settings.get("outreach_mode"), user.id,
    )
    return _file_settings_from_jsonb(settings)


# ── POST /bootstrap (repair) ───────────────────────────────────────


@router.post(
    "/loans/{loan_id}/deal-secretary/bootstrap",
    response_model=BootstrapResponse,
)
async def repair_bootstrap(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BootstrapResponse:
    """Idempotent re-run of bootstrap_requirement_status_rows() for
    loans that pre-date this feature OR whose CRS rows fell out of
    sync with the catalog. Safe — only inserts missing rows."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)
    result = await bootstrap_requirement_status_rows(db, loan, log_label="repair")
    return BootstrapResponse(**result)


# ── POST /start + /pause — the user-visible "turn on / off the AI" ──


class StartResponse(BaseModel):
    """Response for /deal-secretary/start.

    `outreach_mode` is what we flipped to (caller passes intended_mode
    or it defaults to portal_auto). `fired_count` is how many first-
    touch outreach messages actually went out across all the
    AI-owned tasks. `skipped` carries reasons each unfired task was
    skipped (no channel match, missing borrower contact, etc.)."""
    outreach_mode: str
    fired_count: int
    skipped_count: int
    skipped: list[str]


class StartRequest(BaseModel):
    """Optional knob — default mode is portal_auto which the user
    described as 'the safest default that actually contacts the
    client'. Operators can up to portal_email / portal_email_sms here
    or downgrade to draft_first for a 'spin up draft only' pass."""
    mode: OutreachModeLiteral = "portal_auto"


@router.post(
    "/loans/{loan_id}/deal-secretary/start",
    response_model=StartResponse,
)
async def start_ai_secretary(
    loan_id: UUID,
    payload: StartRequest | None = None,
    *,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StartResponse:
    """One-click "Start AI Secretary" — the user-facing primitive that
    makes the AI actually begin contacting the borrower.

    Atomically:
      1. Flips ClientAIPlan.ai_secretary_settings.outreach_mode to the
         requested mode (default portal_auto).
      2. Fires first-touch outreach for every AI-owned task whose
         assignment hasn't already sent a message. Skips the 30-min
         cron wait — the operator sees real activity in the borrower's
         thread within seconds.

    Per-task channel + cadence still come from the assignment. The
    outreach_dispatcher handles channel gating, consent checks, and
    audit logging exactly as the cadence engine would."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)
    mode = (payload.mode if payload else "portal_auto")

    # Flip the file-level outreach mode (lazy-creates the plan if missing).
    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        plan = ClientAIPlan(
            id=uuid.uuid4(),
            client_id=loan.client_id,
            loan_id=loan.id,
            agent_id=loan.broker_id,
            current_phase="lending",
            active_playbook_versions=[],
            required_items=[],
            waived_items=[],
            ai_suggested_items=[],
            ai_secretary_settings={"outreach_mode": mode},
        )
        db.add(plan)
    else:
        settings = dict(plan.ai_secretary_settings or {})
        settings["outreach_mode"] = mode
        plan.ai_secretary_settings = settings
    await db.flush()

    # Pull every live AI assignment on this loan.
    crs_rows = (
        await db.execute(
            select(ClientRequirementStatus).where(
                ClientRequirementStatus.client_id == loan.client_id,
                ClientRequirementStatus.loan_id == loan.id,
                ClientRequirementStatus.owner_type == TaskOwnerType.AI.value,
            )
        )
    ).scalars().all()
    if not crs_rows:
        return StartResponse(
            outreach_mode=mode, fired_count=0, skipped_count=0, skipped=[],
        )
    assignment_ids = [r.ai_assignment_id for r in crs_rows if r.ai_assignment_id]
    assignments = []
    if assignment_ids:
        assignments = (
            await db.execute(
                select(AITaskAssignment).where(
                    AITaskAssignment.id.in_(assignment_ids),
                    AITaskAssignment.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    catalog_by_key: dict[str, AICollectionRequirement] = {}
    if crs_rows:
        cat_rows = (
            await db.execute(
                select(AICollectionRequirement)
                .where(AICollectionRequirement.requirement_key.in_(
                    [r.requirement_key for r in crs_rows]
                ))
            )
        ).scalars().all()
        for c in cat_rows:
            catalog_by_key.setdefault(c.requirement_key, c)

    if mode in ("off", "draft_first"):
        # Draft-only or paused — don't fire; the cadence engine will
        # still respect the mode when it next ticks.
        return StartResponse(
            outreach_mode=mode, fired_count=0, skipped_count=0, skipped=[],
        )

    from app.services.ai.outreach_dispatcher import dispatch as _dispatch
    fired = 0
    skipped: list[str] = []
    assignments_by_crs = {a.client_requirement_status_id: a for a in assignments}
    for crs in crs_rows:
        a = assignments_by_crs.get(crs.id)
        if a is None:
            skipped.append(f"{crs.requirement_key}: no live assignment")
            continue
        # Skip tasks that already have a sent outreach in the audit log —
        # /start is for first-touch only. Re-runs are a no-op.
        if (a.attempts_made or 0) > 0:
            skipped.append(f"{crs.requirement_key}: already dispatched")
            continue
        cat = catalog_by_key.get(crs.requirement_key)
        # Compose first-touch body.
        parts: list[str] = []
        if a.objective_text:
            parts.append(a.objective_text)
        elif cat is not None and cat.objective_text:
            parts.append(cat.objective_text)
        if cat is not None and cat.ai_request_message_template:
            parts.append(cat.ai_request_message_template)
        if not parts:
            parts.append(f"Hi — when you have a moment, can you take care of: {cat.label if cat else crs.requirement_key}?")
        link_url = a.link_url or (cat.link_url if cat else None)
        link_label = a.link_label or (cat.link_label if cat else None)
        if link_url and link_label:
            parts.append(f"{link_label}: {link_url}")
        msg_body = "\n\n".join(parts)

        channels = list(a.channels or ["portal"])
        sent_any = False
        for ch in channels:
            event = await _dispatch(
                db,
                assignment=a,
                channel=ch,
                message_body=msg_body,
                template_key=crs.requirement_key,
            )
            if event.status == "sent":
                sent_any = True
        if sent_any:
            fired += 1
        else:
            skipped.append(f"{crs.requirement_key}: dispatch blocked (consent / channel)")

    log.info(
        "deal_secretary.start loan_id=%s mode=%s fired=%d skipped=%d user=%s",
        loan.id, mode, fired, len(skipped), user.id,
    )
    return StartResponse(
        outreach_mode=mode,
        fired_count=fired,
        skipped_count=len(skipped),
        skipped=skipped,
    )


@router.post(
    "/loans/{loan_id}/deal-secretary/pause",
    response_model=FileSettings,
)
async def pause_ai_secretary(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileSettings:
    """One-click "Pause AI Secretary" — sets outreach_mode to 'off'.
    The AI keeps tracking the plan but stops sending anything until
    /start (or a manual mode flip) re-engages it."""
    loan = await _resolve_loan_and_gate(loan_id, user, db)
    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        # Nothing to pause — return a synthetic FileSettings so the
        # frontend's optimistic UI doesn't crash.
        return FileSettings(outreach_mode="off")
    settings = dict(plan.ai_secretary_settings or {})
    settings["outreach_mode"] = "off"
    plan.ai_secretary_settings = settings
    await db.flush()
    log.info("deal_secretary.pause loan_id=%s user=%s", loan.id, user.id)
    return _file_settings_from_jsonb(settings)


# ── POST /clients/{client_id}/deal-secretary/wizard-intent ─────────


@router.post(
    "/clients/{client_id}/deal-secretary/wizard-intent",
    response_model=WizardIntentResponse,
)
async def buffer_wizard_intent(
    client_id: UUID,
    payload: WizardIntentRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WizardIntentResponse:
    """Buffer Step 4 picks from AgentLeadModal on the Client-stage
    ClientAIPlan (loan_id IS NULL). materialize_pending_assignments()
    in deal_secretary.py converts these into real AITaskAssignment
    rows when the loan is eventually created from the prequal."""
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Borrowers cannot use this endpoint")

    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    if user.role == Role.BROKER:
        if user.broker is None or client.broker_id != user.broker.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Client belongs to another agent")

    # Find or create the realtor-phase ClientAIPlan row.
    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == client_id,
                ClientAIPlan.loan_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        plan = ClientAIPlan(
            id=uuid.uuid4(),
            client_id=client_id,
            loan_id=None,
            agent_id=client.broker_id,
            current_phase="realtor",
            active_playbook_versions=[],
            required_items=[],
            waived_items=[],
            ai_suggested_items=[],
            ai_secretary_settings={"outreach_mode": "draft_first"},
        )
        db.add(plan)
        await db.flush()

    settings = dict(plan.ai_secretary_settings or {})

    # Buffer assignments. Each entry is serialized as a plain dict
    # because Pydantic models don't survive JSONB round-trips.
    if payload.assignments:
        settings["pending_assignments"] = [
            a.model_dump(mode="json") for a in payload.assignments
        ]
    if payload.file_settings is not None:
        for k, v in payload.file_settings.model_dump(exclude_none=True).items():
            settings[k] = v

    plan.ai_secretary_settings = settings
    await db.flush()

    pending = settings.get("pending_assignments") or []
    log.info(
        "deal_secretary.wizard_intent client_id=%s pending_count=%d user=%s",
        client_id, len(pending), user.id,
    )
    return WizardIntentResponse(
        pending_count=len(pending),
        file_settings=_file_settings_from_jsonb(settings),
    )
