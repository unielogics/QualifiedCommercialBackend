"""Loans CRUD + stage transitions + simulator /recalc.

Recalc is the hot path for the desktop HUD sim and mobile Simulator slider.
"""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, GatedUser
from app.enums import AmortizationStyle, LoanStage, LoanType, LoanPurpose, MessageFrom, PropertyType, Role
from app.models.activity import Activity
from app.models.loan import Loan
from app.models.message import Message
from app.schemas.activity import ActivityRead
from app.schemas.document import (
    DocumentCustomCreate,
    DocumentRead,
    RequiredDocumentRead,
    WorkflowDocRead,
    WorkflowRunResult,
)
from app.schemas.loan import FreeCalcRequest, LoanCreate, LoanRead, LoanUpdate, PropertyUpdate, RecalcRequest, RecalcResponse, SizingBreakdown, StageTransition, TodoItemRead
from app.models.app_settings import AppSettings
from app.services import calendar_emitter
from app.services.activity_log import mark_loan_dirty
from app.services.ai.vector_store import log_event as vector_log
from app.services.lender_connect import (
    LenderConnectError,
    NotifyToggle,
    connect_lender,
    disconnect_lender,
)
from app.services.lender_send import LenderSendError, draft_lender_send
from app.services.lender_thread import (
    LenderThreadError,
    ReplyMode,
    load_entry_audit,
    load_thread,
    post_reply,
    preview_reply,
    summarize_thread,
)
from app.services.loan_intake_automation import kickoff_loan
from app.services.email.parser import inject_deal_id
from app.services.hud_template import build_hud_draft
from app.services.lender_matrix import validate_loan
from app.services.math import compute_loan_amount, dscr as dscr_calc
from app.services.math import monthly_payment, pricing_quote
from app.services.math.sizing import SizingResult

router = APIRouter(prefix="/loans", tags=["loans"])


def _gen_deal_id() -> str:
    return f"L-{secrets.randbelow(9000) + 1000}"


def _scope_query(user, stmt):
    """Borrower sees only their own loans; broker sees their assigned loans;
    super_admin sees everything; loan_exec sees everything (UW).

    Defense-in-depth: when role is CLIENT or BROKER but the linked record
    (user.client / user.broker) is missing, we return ``where(False)`` to
    force the gate to fire — never silently fall through to "see everything".
    """
    from sqlalchemy import false as sql_false
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(sql_false())
        return stmt.where(Loan.client_id == user.client.id)
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(sql_false())
        return stmt.where(Loan.broker_id == user.broker.id)
    return stmt


@router.get("", response_model=list[LoanRead])
async def list_loans(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[LoanRead]:
    """List loans visible to the calling user. Returns broker_name +
    client_name on each row so the operator pipeline can show the
    owner reference in its header without an extra round-trip per row."""
    from sqlalchemy.orm import selectinload
    from app.models.broker import Broker
    from app.models.client import Client as _Client

    stmt = _scope_query(
        user,
        select(Loan)
        .options(
            selectinload(Loan.broker),
            selectinload(Loan.client),
        )
        .order_by(Loan.created_at.desc()),
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[LoanRead] = []
    for r in rows:
        d = LoanRead.model_validate(r).model_dump()
        # Pull broker / client display names off the eager-loaded relationships.
        broker_obj = getattr(r, "broker", None)
        client_obj = getattr(r, "client", None)
        d["broker_name"] = getattr(broker_obj, "display_name", None) if broker_obj else None
        d["client_name"] = getattr(client_obj, "name", None) if client_obj else None
        d["client_fico"] = getattr(client_obj, "fico", None) if client_obj else None
        out.append(LoanRead.model_validate(d))
    return out


@router.get("/{loan_id}", response_model=LoanRead)
async def get_loan(loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> LoanRead:
    stmt = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    return LoanRead.model_validate(row)


@router.get("/{loan_id}/required-documents", response_model=list[RequiredDocumentRead])
async def list_required_documents(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[RequiredDocumentRead]:
    """Returns the loan's checklist (per `AppSettings.checklists` for
    its product, falling back to defaults) joined against existing
    Document rows so the upload modal can render which slots are
    filled, in flight, or empty. Always appends an "Other / not in
    checklist" sentinel at the end so the borrower has an escape
    hatch."""
    from datetime import date as _date_type

    from app.models.app_settings import AppSettings as _AppSettings
    from app.models.document import Document as _Document
    from app.enums import DocStatus as _DocStatus
    from app.services.loan_intake_automation import _checklist_for, _coerce_settings

    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    settings_row = (
        await db.execute(select(_AppSettings).limit(1))
    ).scalar_one_or_none()
    settings = _coerce_settings(settings_row)
    checklist = _checklist_for(settings, str(loan.type))

    docs = (
        await db.execute(
            select(_Document).where(_Document.loan_id == loan_id)
        )
    ).scalars().all()
    by_key: dict[str, _Document] = {}
    for d in docs:
        if d.checklist_key:
            existing = by_key.get(d.checklist_key)
            # Prefer the one that's furthest along (verified > received > pending > requested)
            order = {
                _DocStatus.VERIFIED: 5,
                _DocStatus.RECEIVED: 4,
                _DocStatus.FLAGGED: 3,
                _DocStatus.PENDING: 2,
                _DocStatus.REQUESTED: 1,
            }
            if existing is None or order.get(d.status, 0) > order.get(existing.status, 0):
                by_key[d.checklist_key] = d

    today = _date_type.today()
    out: list[RequiredDocumentRead] = []
    for item in checklist.docs:
        cur = by_key.get(item.name)
        days_since = None
        if cur and cur.requested_on:
            days_since = (today - cur.requested_on).days
        out.append(
            RequiredDocumentRead(
                checklist_key=item.name,
                label=item.name,
                required=bool(item.required),
                auto_request=bool(item.auto_request),
                is_other=False,
                current_document_id=cur.id if cur else None,
                current_status=cur.status if cur else None,
                received_on=cur.received_on if cur else None,
                verified_at=cur.verified_at if cur else None,
                days_since_requested=days_since,
            )
        )
    out.append(
        RequiredDocumentRead(
            checklist_key=None,
            label="Other — not in checklist",
            required=False,
            auto_request=False,
            is_other=True,
            current_document_id=None,
            current_status=None,
            received_on=None,
            verified_at=None,
            days_since_requested=None,
        )
    )
    return out


@router.get("/{loan_id}/todo", response_model=list[TodoItemRead])
async def list_loan_todo(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[TodoItemRead]:
    """Client-facing To-Do for a loan, composed from existing data:
    outstanding documents + upcoming calls + open agent/AI asks.
    No new storage. Scoped by the loan's role-scope so a client only
    ever sees their own loan."""
    from datetime import datetime as _dt, timezone as _tz

    from app.models.document import Document as _Document
    from app.models.event import CalendarEvent as _Cal
    from app.models.ai_task import AITask as _AITask
    from app.enums import (
        DocStatus as _DocStatus,
        CalendarEventKind as _CalKind,
        CalendarEventStatus as _CalStatus,
        AITaskStatus as _TaskStatus,
    )
    from app.routers.calendar import _scope_calendar_for_audience

    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    out: list[TodoItemRead] = []

    # Outstanding documents the borrower still owes.
    docs = (
        await db.execute(
            select(_Document).where(
                _Document.loan_id == loan_id,
                _Document.status.in_([_DocStatus.REQUESTED, _DocStatus.PENDING]),
            )
        )
    ).scalars().all()
    for d in docs:
        out.append(
            TodoItemRead(
                id=f"doc:{d.id}",
                kind="document",
                title=d.name or "Document requested",
                subtitle="Upload requested",
                status=str(d.status),
                due_at=None,
                deeplink=f"/loan/{loan_id}?tab=docs",
            )
        )

    # Upcoming calls visible to this user on this loan.
    now = _dt.now(_tz.utc)
    cal_stmt = _scope_calendar_for_audience(
        user,
        select(_Cal).where(
            _Cal.loan_id == loan_id,
            _Cal.kind == _CalKind.CALL,
            _Cal.status != _CalStatus.DONE,
            _Cal.starts_at >= now,
        ),
    )
    for ev in (await db.execute(cal_stmt)).scalars().all():
        out.append(
            TodoItemRead(
                id=f"call:{ev.id}",
                kind="call",
                title=ev.title or "Scheduled call",
                subtitle=ev.starts_at.strftime("%b %d, %I:%M %p"),
                status=str(ev.status),
                due_at=ev.starts_at.isoformat(),
                deeplink=f"/loan/{loan_id}?tab=todo",
            )
        )

    # Open agent / AI asks tied to this loan.
    tasks = (
        await db.execute(
            select(_AITask).where(
                _AITask.loan_id == loan_id,
                _AITask.status == _TaskStatus.PENDING,
            )
        )
    ).scalars().all()
    for tk in tasks:
        out.append(
            TodoItemRead(
                id=f"task:{tk.id}",
                kind="task",
                title=tk.title or "Action needed",
                subtitle=(tk.summary or None),
                status=str(tk.status),
                due_at=None,
                deeplink=f"/loan/{loan_id}?tab=todo",
            )
        )

    return out


@router.get("/{loan_id}/workflow", response_model=list[WorkflowDocRead])
async def list_loan_workflow(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[WorkflowDocRead]:
    """The AI's collection plan for this loan. One row per Document
    on the loan, joined with the per-loan-type checklist's
    `due_offset_days` + the live scenario classifier so the operator
    sees what the AI will say next and when.

    Used by the Workflow tab on the loan detail page. Mutations are
    PATCH /documents/{id}; manual reminder dispatch is
    POST /loans/{id}/run-doc-reminders below.
    """
    from datetime import date as _date_type, timedelta as _timedelta

    from app.models.app_settings import AppSettings as _AppSettings
    from app.models.document import Document as _Document
    from app.services.doc_collection_ai import classify as _classify
    from app.services.loan_intake_automation import (
        _coerce_settings,
        _DEFAULT_FIRST_DAYS,
    )

    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    settings_row = (
        await db.execute(select(_AppSettings).limit(1))
    ).scalar_one_or_none()
    settings = _coerce_settings(settings_row)
    # Resolved checklist (alembic 0023): firm baseline + side filter +
    # agent overlay. Same view the cron + kickoff materializer use.
    from app.services.agent_checklist import resolve_loan_checklist
    resolved_items, base_checklist = await resolve_loan_checklist(
        db, loan=loan, settings=settings,
    )
    item_offset_by_name: dict[str, int] = {}
    item_side_by_name: dict[str, str] = {}
    for item in resolved_items:
        item_offset_by_name[item.name] = item.due_offset_days
        item_side_by_name[item.name] = item.side
    fallback_offset = base_checklist.first_reminder_days if base_checklist else _DEFAULT_FIRST_DAYS

    docs = (
        await db.execute(
            select(_Document)
            .where(_Document.loan_id == loan_id)
            .order_by(_Document.name.asc())
        )
    ).scalars().all()

    today = _date_type.today()
    out: list[WorkflowDocRead] = []
    for d in docs:
        offset = item_offset_by_name.get(d.checklist_key or "", fallback_offset)
        default_due = (
            d.requested_on + _timedelta(days=offset) if d.requested_on else None
        )
        effective_due = d.due_date or default_due
        days_until = (effective_due - today).days if effective_due else None
        scenario = _classify(days_until) if days_until is not None else None

        # Compute the next scenario boundary so the UI can show
        # "next message in X days". Scenarios in order:
        #   heads_up (3 → 1) → due_today (0) → just_late (-1 → -3)
        #   → week_late (-4 → -7) → escalating (-8+)
        next_scenario: str | None = None
        next_in: int | None = None
        if days_until is not None and effective_due is not None:
            for boundary, label in [
                (3, "heads_up"),
                (0, "due_today"),
                (-1, "just_late"),
                (-4, "week_late"),
                (-8, "escalating"),
            ]:
                # Days until the doc reaches `boundary` from today.
                gap = days_until - boundary
                if gap > 0:
                    next_scenario = label
                    next_in = gap
                    break

        side = item_side_by_name.get(d.checklist_key or "", "both")
        out.append(
            WorkflowDocRead(
                document_id=d.id,
                name=d.name,
                status=d.status,
                checklist_key=d.checklist_key,
                is_other=bool(d.is_other),
                requested_on=d.requested_on,
                received_on=d.received_on,
                due_date=d.due_date,
                default_due_date=default_due,
                effective_due_date=effective_due,
                days_until_due=days_until,
                scenario=scenario,
                next_scenario=next_scenario,
                next_scenario_in_days=next_in,
                side=side,
            )
        )
    return out


@router.post(
    "/{loan_id}/documents/custom",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_document(
    loan_id: UUID,
    payload: DocumentCustomCreate,
    user: GatedUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentRead:
    """Operator/agent adds a one-off doc to this loan's collection
    plan. Creates a `Document` row with `is_other=True,
    checklist_key=null, status=REQUESTED, requested_on=today` and
    optionally a per-loan `due_date`. Used by the WorkflowTab's
    '+ Add custom item' button and the SmartIntakeModal's
    pre-loan Documents step.

    Borrowers can't add docs via this — that's an operator workflow
    lever (it implies "we're collecting this from you").
    """
    from datetime import date as _date_type
    from app.models.document import Document as _Document
    from app.enums import DocStatus as _DocStatus

    if user.role == Role.CLIENT:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Borrowers cannot add custom docs"
        )
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name is required")

    doc = _Document(
        loan_id=loan.id,
        name=name[:255],
        category=None,
        s3_key=None,
        status=_DocStatus.REQUESTED,
        requested_on=_date_type.today(),
        due_date=payload.due_date,
        checklist_key=payload.checklist_key,
        is_other=(payload.checklist_key is None),
    )
    db.add(doc)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="document.custom_created",
            summary=f"Custom doc added: {name}",
            payload={
                "doc_id": None,  # filled after flush
                "name": name,
                "checklist_key": payload.checklist_key,
                "due_date": payload.due_date.isoformat() if payload.due_date else None,
            },
        )
    )
    await db.flush()
    await db.refresh(doc)
    await mark_loan_dirty(db, loan.id)
    return DocumentRead.model_validate(doc)


@router.post(
    "/{loan_id}/run-doc-reminders",
    response_model=WorkflowRunResult,
)
async def run_loan_doc_reminders(
    loan_id: UUID, user: GatedUser, db: AsyncSession = Depends(get_db)
) -> WorkflowRunResult:
    """Manually fire the doc-collection evaluator for THIS loan only.

    Operator clicks "Send reminder now" on the Workflow tab; we run
    `evaluate_doc_reminders(scope_loan_id=...)` so any doc currently
    in a reminder scenario gets a fresh AI message (subject to the
    per-(doc, scenario) dedup — already-fired scenarios stay quiet).
    Returns the per-scenario count for the toast.
    """
    if user.role == Role.CLIENT:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Borrowers cannot manually run reminders"
        )
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    from app.services.loan_intake_automation import evaluate_doc_reminders
    counts = await evaluate_doc_reminders(scope_loan_id=loan_id)
    return WorkflowRunResult(counts=counts)


@router.get("/{loan_id}/activity", response_model=list[ActivityRead])
async def list_loan_activity(
    loan_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ActivityRead]:
    """Immutable activity log for a loan. Newest first."""
    # First confirm the user can see this loan (apply scope).
    scope = _scope_query(user, select(Loan.id).where(Loan.id == loan_id))
    visible = (await db.execute(scope)).scalar_one_or_none()
    if visible is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    rows = (
        await db.execute(
            select(Activity)
            .where(Activity.loan_id == loan_id)
            .order_by(Activity.occurred_at.desc())
        )
    ).scalars().all()
    return [ActivityRead.model_validate(r) for r in rows]


@router.post("", response_model=LoanRead, status_code=status.HTTP_201_CREATED)
async def create_loan(
    payload: LoanCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> LoanRead:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal funding role required")
    deal_id = payload.deal_id or _gen_deal_id()
    loan = Loan(deal_id=deal_id, **payload.model_dump(exclude={"deal_id"}))
    db.add(loan)
    await db.flush()
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="loan.created",
            summary=f"Loan {deal_id} created — {loan.address}",
        )
    )
    await vector_log(
        db,
        loan_id=loan.id,
        deal_id=deal_id,
        kind="loan.created",
        content=f"Loan {deal_id} for {loan.address}, type={loan.type.value}, amount={loan.amount}",
    )
    # Doc collection automation: read the firm's checklist for this
    # loan type and auto-create the Document rows + calendar reminders.
    # Idempotent — safe across retries.
    settings_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    await kickoff_loan(db, loan, settings_row)

    # AI Deal Secretary bootstrap (alembic 0038). Creates one
    # ClientRequirementStatus per resolved playbook requirement so the
    # workbench picker is pre-populated. Outreach stays off until an
    # operator flips ai_secretary_settings.outreach_mode in the UI.
    try:
        from app.services.ai.deal_secretary import bootstrap_requirement_status_rows
        await bootstrap_requirement_status_rows(db, loan, log_label="loans.create")
    except Exception:  # noqa: BLE001
        # Don't fail loan creation on a bootstrap hiccup — operator can
        # re-run via the repair endpoint. Log loud so we notice.
        log.exception("deal_secretary.bootstrap_failed deal_id=%s", deal_id)

    # Living Loan File debounced refresh — flag for the dirty-drain.
    await mark_loan_dirty(db, loan.id)

    await db.flush()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


@router.patch("/{loan_id}", response_model=LoanRead)
async def update_loan(
    loan_id: UUID,
    payload: LoanUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanRead:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal funding role required")
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    # Snapshot BEFORE the mutation so the diff helper can compute
    # field-level changes for the activity payload.
    from app.services.activity_log import log_loan_diff, loan_snapshot
    before = loan_snapshot(loan)

    changes = payload.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(loan, k, v)

    # Broker-assignment cascade. A client relationship is owned by ONE
    # broker — the client.broker_id is the source of truth. Reassigning
    # a loan to a different broker (e.g. via the desktop
    # MultiLoanReassignModal, which PATCHes loans only) must carry the
    # client and the client's sibling loans with it, otherwise the
    # relationship splits: loans owned by broker X while the Clients
    # tab / "Message client" path (scoped by client.broker_id) still
    # points at broker Y. That split is exactly the
    # franco@unielogics.com bug. Keep everything under one broker.
    if "broker_id" in changes and loan.client_id is not None:
        new_broker_id = changes["broker_id"]
        from app.models.client import Client as _Client

        client_row = (
            await db.execute(select(_Client).where(_Client.id == loan.client_id))
        ).scalar_one_or_none()
        if client_row is not None and client_row.broker_id != new_broker_id:
            client_row.broker_id = new_broker_id
            db.add(
                Activity(
                    client_id=client_row.id,
                    actor_id=user.id,
                    actor_label=user.email,
                    kind="client.broker_reassigned",
                    summary=(
                        f"Client reassigned to broker {new_broker_id} "
                        f"(cascaded from loan {loan.deal_id} reassignment)"
                    ),
                )
            )
        # Sibling loans for the same client move too, so the
        # relationship isn't split across brokers.
        sibling_loans = (
            await db.execute(
                select(Loan).where(
                    Loan.client_id == loan.client_id,
                    Loan.id != loan.id,
                    Loan.broker_id != new_broker_id,
                )
            )
        ).scalars().all()
        for sib in sibling_loans:
            sib.broker_id = new_broker_id

    # Diff-aware activity. Writes one row tagged either
    # `loan.criteria_changed` (operator-visible) or `loan.pricing_changed`
    # (internal-only) with structured before→after payload. Returns
    # None when nothing in LOAN_DIFF_FIELDS actually changed (e.g. PATCH
    # touched only `status_summary` or another non-diffed field).
    await log_loan_diff(db, loan=loan, before=before, actor=user, source="operator_edit")

    # If close_date moved (and the loan is at CLOSING, or already
    # has a close milestone on the calendar), the upsert keeps it
    # in sync. Cheap to call even when close_date didn't change —
    # ON CONFLICT collapses the no-op write.
    if "close_date" in changes and loan.stage == LoanStage.CLOSING:
        await calendar_emitter.emit_for_loan_close(db, loan)
    # Phase 6 — flag dirty so the next drain picks up the change.
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


@router.get("/{loan_id}/term-sheet.pdf")
async def download_term_sheet_pdf(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    interest_only_months: int = 0,
):
    """Streams a PDF term sheet + amortization schedule for the loan.
    Open to brokers and the funding team — the same audience that sees
    Criteria tab. Borrowers don't hit this directly; operators share the
    file with them once they download it."""
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    # Generate PDF in a thread so we don't block the event loop on the
    # CPU-heavy WeasyPrint render.
    import asyncio
    from app.services.term_sheet_pdf import render_term_sheet_pdf

    def _render() -> bytes:
        return render_term_sheet_pdf(
            deal_id=loan.deal_id or str(loan.id)[:8],
            address=loan.address,
            city=loan.city,
            state=loan.state,
            loan_amount=float(loan.amount),
            base_rate=float(loan.base_rate) if loan.base_rate is not None else None,
            final_rate=float(loan.final_rate) if loan.final_rate is not None else None,
            discount_points=float(loan.discount_points or 0),
            origination_pct=float(loan.origination_pct or 0),
            term_months=loan.term_months,
            purpose=loan.purpose,
            arv=float(loan.arv) if loan.arv is not None else None,
            ltv=float(loan.ltv) if loan.ltv is not None else None,
            annual_taxes=float(loan.annual_taxes or 0),
            annual_insurance=float(loan.annual_insurance or 0),
            monthly_hoa=float(loan.monthly_hoa or 0),
            monthly_rent=float(loan.monthly_rent) if loan.monthly_rent is not None else None,
            interest_only_months=max(0, int(interest_only_months or 0)),
            amortization_style=loan.amortization_style,
            prepay_penalty=loan.prepay_penalty,
            vacancy_pct=float(loan.vacancy_pct) if loan.vacancy_pct is not None else None,
            expense_ratio_pct=(
                float(loan.expense_ratio_pct) if loan.expense_ratio_pct is not None else None
            ),
            reserves_required=(
                float(loan.reserves_required) if loan.reserves_required is not None else None
            ),
            lender_fees=float(loan.lender_fees) if loan.lender_fees is not None else None,
            construction_holdback_pct=(
                float(loan.construction_holdback_pct)
                if loan.construction_holdback_pct is not None
                else None
            ),
            exit_strategy=loan.exit_strategy,
            entity_type=loan.entity_type,
            experience_tier=loan.experience_tier,
            fico_override=loan.fico_override,
            cash_to_borrower=(
                float(loan.cash_to_borrower) if loan.cash_to_borrower is not None else None
            ),
            seasoning_months=loan.seasoning_months,
            property_count=loan.property_count,
            draw_count=loan.draw_count,
        )

    pdf_bytes = await asyncio.to_thread(_render)
    filename = f"term-sheet-{loan.deal_id or str(loan.id)[:8]}.pdf"
    from fastapi.responses import Response
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{loan_id}/property", response_model=LoanRead)
async def update_property(
    loan_id: UUID,
    payload: PropertyUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanRead:
    """Property/listing-style patch endpoint. Open to brokers as well
    as the funding team so agents can fill the listing-page surface
    on the Property tab. Scope is enforced by `_scope_query`, which
    only returns loans the broker owns."""
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Property edits require an operator role")
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    changes = payload.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(loan, k, v)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="loan.property_updated",
            summary=f"Property updated: {', '.join(changes.keys())}",
            payload=changes,
        )
    )
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


@router.post("/{loan_id}/stage", response_model=LoanRead)
async def transition_stage(
    loan_id: UUID,
    payload: StageTransition,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanRead:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Internal funding role required")
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    old = loan.stage
    loan.stage = payload.new_stage
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="loan.stage_change",
            summary=f"{old} → {payload.new_stage}",
            payload={"from": old, "to": payload.new_stage, "note": payload.note},
        )
    )
    await vector_log(
        db,
        loan_id=loan.id,
        deal_id=loan.deal_id,
        kind="loan.stage_change",
        content=f"Stage moved {old} → {payload.new_stage}. {payload.note}",
    )
    # TODO: when payload.new_stage == LoanStage.FUNDED, invoke broker points
    # award; deferred per architecture constraint #8.
    # Emit / cancel the CLOSING milestone on the calendar. Idempotent:
    # rerunning the same PATCH leaves the existing event in place.
    await calendar_emitter.emit_for_loan_stage(db, loan, old, payload.new_stage)
    # Phase 6 — stage moves are the most informative signal for the
    # Living Loan File. Mark dirty for the next drain.
    await mark_loan_dirty(db, loan.id)
    await db.flush()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


# ── Connect / disconnect lender ───────────────────────────────────────


class NotifyTogglePayload(BaseModel):
    participant_id: UUID
    cc_outbound: bool = False
    bcc_outbound: bool = False


class ConnectLenderPayload(BaseModel):
    lender_id: UUID
    notify: list[NotifyTogglePayload] = Field(default_factory=list)


class ConnectLenderResponse(BaseModel):
    loan: LoanRead
    lender_id: UUID
    lender_name: str
    cc_count: int
    bcc_count: int
    stage_advanced: bool


@router.post("/{loan_id}/connect-lender", response_model=ConnectLenderResponse)
async def connect_lender_endpoint(
    loan_id: UUID,
    payload: ConnectLenderPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectLenderResponse:
    """Wire a lender onto this loan — sets `loan.lender_id`, ensures a
    hide_identity LENDER participant row exists, applies the
    operator's notify-list toggles, and promotes stage to
    LENDER_CONNECTED if it hadn't reached it. Super-admin only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")

    toggles = [
        NotifyToggle(
            participant_id=t.participant_id,
            cc_outbound=t.cc_outbound,
            bcc_outbound=t.bcc_outbound,
        )
        for t in payload.notify
    ]
    try:
        result = await connect_lender(
            db,
            loan_id=loan_id,
            lender_id=payload.lender_id,
            notify_toggles=toggles,
            actor_user_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
        )
    except LenderConnectError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if result.stage_advanced:
        # Mirror /stage's emitter call so the calendar still reflects
        # the LENDER_CONNECTED milestone if there's a hook for it.
        await calendar_emitter.emit_for_loan_stage(
            db, result.loan, LoanStage.COLLECTING_DOCS, result.loan.stage
        )

    await db.commit()
    await db.refresh(result.loan)
    return ConnectLenderResponse(
        loan=LoanRead.model_validate(result.loan),
        lender_id=result.lender.id,
        lender_name=result.lender.name,
        cc_count=result.cc_count,
        bcc_count=result.bcc_count,
        stage_advanced=result.stage_advanced,
    )


class LenderSendPayload(BaseModel):
    document_ids: list[UUID] = Field(default_factory=list)
    delivery: str = Field(default="links", pattern="^(links|zip)$")


class LenderSendResponse(BaseModel):
    draft_id: UUID
    lender_id: UUID
    lender_name: str
    delivery: str
    document_count: int
    zip_s3_key: str | None
    to_email: str
    subject: str


@router.post("/{loan_id}/lender/send", response_model=LenderSendResponse)
async def lender_send_endpoint(
    loan_id: UUID,
    payload: LenderSendPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderSendResponse:
    """Package the picked docs and create an EmailDraft to the
    connected lender. The draft lands in the existing
    pending-broker-review queue; sending happens through the
    standard /email-drafts/{id}/approve route. Super-admin only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    try:
        result = await draft_lender_send(
            db,
            loan_id=loan_id,
            document_ids=payload.document_ids,
            delivery=payload.delivery,  # type: ignore[arg-type]
            actor_user_id=user.id,
        )
    except LenderSendError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return LenderSendResponse(
        draft_id=result.draft.id,
        lender_id=result.lender.id,
        lender_name=result.lender.name,
        delivery=result.delivery,
        document_count=result.document_count,
        zip_s3_key=result.zip_s3_key,
        to_email=result.draft.to_email,
        subject=result.draft.subject,
    )


class LenderThreadEntryRead(BaseModel):
    id: str
    kind: str  # "inbound" | "outbound" | "ai_outbound" | "pending_draft"
    sender_label: str
    sender_role: str
    sent_at: str
    body: str
    subject: str | None = None
    is_ai_drafted: bool = False
    sent_message_id: str | None = None
    draft_id: str | None = None
    # Round-2: Gmail delivery outcome surfaced per-entry.
    send_status: str = "n/a"  # "sent" | "saved" | "failed" | "n/a"
    send_note: str | None = None
    to_email: str | None = None
    # Round-4: committed attachments on this entry. Empty list when
    # there are none; download_urls are short-lived (1h).
    attachments: list["LenderThreadAttachmentRead"] = []


class GmailPayloadRead(BaseModel):
    from_email: str
    to_email: str
    subject: str
    body: str
    raw_base64: str | None
    would_send_via: str  # "service-account-DWD" | "(not configured)"


class LenderThreadEntryAuditRead(BaseModel):
    entry_id: str
    message: dict | None
    email_draft: dict | None
    activity: dict | None
    gmail_payload: GmailPayloadRead | None
    send_status: str
    send_note: str | None


class LenderThreadPreviewPayload(BaseModel):
    mode: str = Field(pattern="^(send_now|instruct_ai|save_draft)$")
    text: str = Field(min_length=1)


class LenderThreadPreviewResponse(BaseModel):
    mode: str
    to_email: str
    subject: str
    body: str
    gmail_payload: GmailPayloadRead
    gmail_ready: bool
    gmail_status_note: str


class LenderThreadResponse(BaseModel):
    loan_id: str
    lender_name: str | None
    entries: list[LenderThreadEntryRead]
    # Round-3: structured AI extract — per-viewer filtered. See
    # app/services/lender_thread.load_thread for the role gate.
    lender_extract: dict | None = None


class LenderThreadSummaryRead(BaseModel):
    loan_id: str
    headline: str
    open_asks: list[str]
    suggested_next_reply: str
    message_count: int


class LenderThreadAttachmentRead(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    source: str  # 'outbound_upload' | 'system_doc_ref' | 'inbound_lender'
    direction: str  # 'outbound' | 'inbound'
    document_id: str | None = None
    download_url: str | None = None


class AttachmentInitPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=120)
    size_bytes: int = Field(ge=1)


class AttachmentInitResponse(BaseModel):
    attachment_id: str
    upload_url: str | None
    s3_key: str
    filename: str
    mime_type: str
    size_bytes: int


class AttachmentFromDocPayload(BaseModel):
    document_id: UUID


class LenderThreadReplyPayload(BaseModel):
    mode: str = Field(pattern="^(send_now|instruct_ai|save_draft)$")
    text: str = Field(min_length=1)
    # Round-4: attachment refs to commit + send with this reply. IDs
    # come from /attachment/upload-init (or /from-doc). Optional —
    # backwards-compatible with round-3 reply payloads.
    attachment_ids: list[UUID] = Field(default_factory=list)


class LenderThreadReplyResponse(BaseModel):
    mode: str
    note: str
    entry: LenderThreadEntryRead | None


def _entry_to_read(entry) -> LenderThreadEntryRead:
    return LenderThreadEntryRead(
        id=entry.id,
        kind=entry.kind,
        sender_label=entry.sender_label,
        sender_role=entry.sender_role,
        sent_at=entry.sent_at.isoformat(),
        body=entry.body,
        subject=entry.subject,
        is_ai_drafted=entry.is_ai_drafted,
        sent_message_id=entry.sent_message_id,
        draft_id=entry.draft_id,
        send_status=getattr(entry, "send_status", "n/a"),
        send_note=getattr(entry, "send_note", None),
        to_email=getattr(entry, "to_email", None),
        attachments=[
            LenderThreadAttachmentRead(
                id=a.id,
                filename=a.filename,
                mime_type=a.mime_type,
                size_bytes=a.size_bytes,
                source=a.source,
                direction=a.direction,
                download_url=a.download_url,
            )
            for a in (getattr(entry, "attachments", []) or [])
        ],
    )


def _gmail_payload_to_read(view) -> GmailPayloadRead:
    return GmailPayloadRead(
        from_email=view.from_email,
        to_email=view.to_email,
        subject=view.subject,
        body=view.body,
        raw_base64=view.raw_base64,
        would_send_via=view.would_send_via,
    )


@router.get("/{loan_id}/lender-thread", response_model=LenderThreadResponse)
async def get_lender_thread(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderThreadResponse:
    """Return the lender ↔ brokerage timeline for this loan.

    Visible to anyone with access to the loan (the standard /loans
    scope check applies via CurrentUser → role); PII is redacted
    server-side for broker/client viewers."""
    # Defense-in-depth: re-run the visibility scope used elsewhere.
    visibility_stmt = _scope_query(user, select(Loan.id).where(Loan.id == loan_id))
    visible_id = (await db.execute(visibility_stmt)).scalar_one_or_none()
    if visible_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    try:
        thread = await load_thread(db, loan_id=loan_id, viewer=user)
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LenderThreadResponse(
        loan_id=thread.loan_id,
        lender_name=thread.lender_name,
        entries=[_entry_to_read(e) for e in thread.entries],
        lender_extract=thread.lender_extract,
    )


@router.get("/{loan_id}/lender-thread/summary", response_model=LenderThreadSummaryRead)
async def get_lender_thread_summary(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderThreadSummaryRead:
    """Short 'what's happening on the lender side' mini-summary.
    Distinct from the loan-level Living Profile — this one is scoped
    to the lender conversation only and is regenerated on demand."""
    visibility_stmt = _scope_query(user, select(Loan.id).where(Loan.id == loan_id))
    visible_id = (await db.execute(visibility_stmt)).scalar_one_or_none()
    if visible_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    try:
        summary = await summarize_thread(db, loan_id=loan_id, viewer=user)
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LenderThreadSummaryRead(
        loan_id=summary.loan_id,
        headline=summary.headline,
        open_asks=summary.open_asks,
        suggested_next_reply=summary.suggested_next_reply,
        message_count=summary.message_count,
    )


@router.post("/{loan_id}/lender-thread/reply", response_model=LenderThreadReplyResponse)
async def post_lender_thread_reply(
    loan_id: UUID,
    payload: LenderThreadReplyPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderThreadReplyResponse:
    """Post a reply to the lender thread in one of three modes.

    Super-admin and loan-exec only — no funding-team approval gate
    on this surface (per Op direction). Modes:
      send_now    — send via Gmail immediately
      instruct_ai — AI writes the email from the operator's prompt and sends
      save_draft  — drop into EmailDrafts(status=PENDING), no send
    """
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only super_admin and loan_exec can post to the lender thread.",
        )
    try:
        result = await post_reply(
            db,
            loan_id=loan_id,
            actor=user,
            mode=payload.mode,  # type: ignore[arg-type]
            text=payload.text,
            attachment_ids=payload.attachment_ids or None,
        )
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return LenderThreadReplyResponse(
        mode=result.mode,
        note=result.note,
        entry=_entry_to_read(result.entry) if result.entry else None,
    )


@router.get(
    "/{loan_id}/lender-thread/entry/{entry_id}/audit",
    response_model=LenderThreadEntryAuditRead,
)
async def get_lender_thread_entry_audit(
    loan_id: UUID,
    entry_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderThreadEntryAuditRead:
    """Return the raw DB rows + the would-be-sent Gmail payload for a
    single thread entry. Super-admin and loan-exec only. Powers the
    'Show details' audit drawer on each mailbox row."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only super_admin and loan_exec can view raw send audit.",
        )
    visibility_stmt = _scope_query(user, select(Loan.id).where(Loan.id == loan_id))
    if (await db.execute(visibility_stmt)).scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    try:
        audit = await load_entry_audit(db, loan_id=loan_id, entry_id=entry_id, viewer=user)
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LenderThreadEntryAuditRead(
        entry_id=audit.entry_id,
        message=audit.message,
        email_draft=audit.email_draft,
        activity=audit.activity,
        gmail_payload=_gmail_payload_to_read(audit.gmail_payload) if audit.gmail_payload else None,
        send_status=audit.send_status,
        send_note=audit.send_note,
    )


@router.post(
    "/{loan_id}/lender-thread/preview",
    response_model=LenderThreadPreviewResponse,
)
async def post_lender_thread_preview(
    loan_id: UUID,
    payload: LenderThreadPreviewPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LenderThreadPreviewResponse:
    """Compute the exact payload that WOULD be sent if the operator
    clicked Send Now (or Instruct AI). Writes NOTHING; safe to call
    repeatedly. Powers the 'Preview before send' modal."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only super_admin and loan_exec can preview a lender reply.",
        )
    try:
        result = await preview_reply(
            db,
            loan_id=loan_id,
            actor=user,
            mode=payload.mode,  # type: ignore[arg-type]
            text=payload.text,
        )
    except LenderThreadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return LenderThreadPreviewResponse(
        mode=result.mode,
        to_email=result.to_email,
        subject=result.subject,
        body=result.body,
        gmail_payload=_gmail_payload_to_read(result.gmail_payload),
        gmail_ready=result.gmail_ready,
        gmail_status_note=result.gmail_status_note,
    )


# ────────────────────────────────────────────────────────────────────
# Lender-thread attachments — composer + audit drawer
# ────────────────────────────────────────────────────────────────────


@router.post(
    "/{loan_id}/lender-thread/attachment/upload-init",
    response_model=AttachmentInitResponse,
)
async def lender_attachment_upload_init(
    loan_id: UUID,
    payload: AttachmentInitPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AttachmentInitResponse:
    """Reserve a staged outbound attachment + presigned S3 PUT URL.
    Super-admin / loan-exec only — the same gate as posting a reply."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only super_admin and loan_exec can attach files to the lender thread.",
        )
    from app.services.lender_attachments import AttachmentError, init_outbound_upload

    try:
        result = await init_outbound_upload(
            db,
            loan_id=loan_id,
            filename=payload.filename,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
            uploaded_by=user.id,
        )
    except AttachmentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return AttachmentInitResponse(**result)


class AttachmentReadOnly(BaseModel):
    """Compact attachment shape used by upload-complete + from-doc.
    The frontend stores these in its composer chip list."""

    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    source: str


@router.post(
    "/{loan_id}/lender-thread/attachment/upload-complete",
    response_model=AttachmentReadOnly,
)
async def lender_attachment_upload_complete(
    loan_id: UUID,
    attachment_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AttachmentReadOnly:
    """Browser calls this after the S3 PUT completes. We don't HEAD
    the object (boto's signed PUT already enforces size+content-type
    server-side), just mark the row ready-for-send and return its
    metadata."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    from app.models.message_attachment import MessageAttachment

    att = (
        await db.execute(
            select(MessageAttachment).where(
                MessageAttachment.id == attachment_id,
                MessageAttachment.loan_id == loan_id,
            )
        )
    ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    # We don't flip status here; staged → committed flip happens when
    # the reply handler commits attachments to a Message. This call
    # is mainly for the future when we want to validate the upload
    # actually succeeded (e.g., HEAD against S3).
    return AttachmentReadOnly(
        attachment_id=str(att.id),
        filename=att.filename,
        mime_type=att.mime_type,
        size_bytes=att.size_bytes,
        source=att.source,
    )


@router.post(
    "/{loan_id}/lender-thread/attachment/from-doc",
    response_model=AttachmentReadOnly,
)
async def lender_attachment_from_doc(
    loan_id: UUID,
    payload: AttachmentFromDocPayload,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AttachmentReadOnly:
    """Operator picked an existing loan Document to attach. We
    re-reference its s3_key without duplicating bytes on S3."""
    if user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    from app.services.lender_attachments import AttachmentError, from_existing_document

    try:
        att = await from_existing_document(
            db,
            loan_id=loan_id,
            document_id=payload.document_id,
            uploaded_by=user.id,
        )
    except AttachmentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return AttachmentReadOnly(
        attachment_id=str(att.id),
        filename=att.filename,
        mime_type=att.mime_type,
        size_bytes=att.size_bytes,
        source=att.source,
    )


@router.get(
    "/{loan_id}/lender-thread/attachment/{attachment_id}/download",
    response_model=dict,
)
async def lender_attachment_download(
    loan_id: UUID,
    attachment_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return a short-lived signed URL the browser can use to
    download the attachment. Anyone with access to the loan can read
    (visibility filter at /lender-thread already gates which
    attachments are exposed)."""
    visibility_stmt = _scope_query(user, select(Loan.id).where(Loan.id == loan_id))
    if (await db.execute(visibility_stmt)).scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    from app.models.message_attachment import MessageAttachment
    from app.services.lender_attachments import presign_download

    att = (
        await db.execute(
            select(MessageAttachment).where(
                MessageAttachment.id == attachment_id,
                MessageAttachment.loan_id == loan_id,
            )
        )
    ).scalar_one_or_none()
    if att is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    url = await presign_download(att)
    if url is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "S3 not configured — cannot presign downloads.",
        )
    return {
        "attachment_id": str(att.id),
        "url": url,
        "filename": att.filename,
        "mime_type": att.mime_type,
    }


@router.post("/{loan_id}/disconnect-lender", response_model=LoanRead)
async def disconnect_lender_endpoint(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanRead:
    """Remove the lender connection. Stage stays where it is — we
    never auto-regress. Super-admin only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")
    try:
        loan = await disconnect_lender(
            db,
            loan_id=loan_id,
            actor_user_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
        )
    except LenderConnectError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


def _try_size(
    *,
    loan_type: LoanType,
    purpose: LoanPurpose | None,
    arv: float | None,
    brv: float | None,
    rehab_budget: float | None,
    payoff: float | None,
    requested_amount: float | None,
    ltv_tier_cap: float | None,
) -> SizingResult | None:
    """Run the sizing engine when we have enough inputs; otherwise None.

    DSCR sizing needs `arv`. F&F / GU sizing needs `arv` AND `brv`.
    Other product types (BRIDGE, PORTFOLIO, CASH_OUT_REFI) skip sizing —
    they fall through to caller-supplied amount.
    """
    try:
        if loan_type == LoanType.DSCR and arv:
            return compute_loan_amount(
                loan_type=loan_type,
                purpose=purpose,
                arv=arv,
                payoff=payoff,
                requested_amount=requested_amount,
                ltv_tier_cap=ltv_tier_cap,
            )
        if loan_type in {LoanType.FIX_AND_FLIP, LoanType.GROUND_UP} and arv and brv:
            return compute_loan_amount(
                loan_type=loan_type,
                purpose=purpose,
                arv=arv,
                brv=brv,
                rehab_budget=rehab_budget,
                requested_amount=requested_amount,
            )
    except ValueError:
        return None
    return None


def _sizing_to_breakdown(result: SizingResult) -> SizingBreakdown:
    return SizingBreakdown(
        loan_amount=result.loan_amount,
        max_allowed=result.max_allowed,
        binding_constraint=result.binding_constraint,
        clamped=result.clamped,
        ltv=result.ltv,
        ltc=result.ltc,
        arv_ltv=result.arv_ltv,
        effective_ltv_cap=result.effective_ltv_cap,
        total_cost=result.total_cost,
        cash_to_borrower=result.cash_to_borrower,
        cash_to_close=result.cash_to_close,
    )


@router.post("/{loan_id}/recalc", response_model=RecalcResponse)
async def recalc(
    loan_id: UUID,
    payload: RecalcRequest,
    user: GatedUser,
    db: AsyncSession = Depends(get_db),
) -> RecalcResponse:
    scope = _scope_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(scope)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    base_rate = payload.base_rate or float(loan.base_rate or 0.07)
    arv_for_sizing = payload.arv if payload.arv is not None else (float(loan.arv) if loan.arv else None)
    requested_amount = payload.loan_amount or float(loan.amount)
    if payload.loan_amount is None and payload.ltv is not None and arv_for_sizing:
        requested_amount = float(payload.ltv) * arv_for_sizing
    sizing = _try_size(
        loan_type=LoanType(loan.type),
        purpose=payload.purpose or loan.purpose,
        arv=arv_for_sizing,
        brv=payload.brv,
        rehab_budget=payload.rehab_budget,
        payoff=payload.payoff,
        requested_amount=requested_amount,
        ltv_tier_cap=payload.ltv_tier_cap,
    )
    amount = sizing.loan_amount if sizing else requested_amount
    origination_pct = (
        payload.origination_pct
        if payload.origination_pct is not None
        else float(loan.origination_pct or 0.015)
    )
    quote = pricing_quote(base_rate, amount, payload.discount_points, origination_pct=origination_pct)

    # Amortization style — explicit override beats the loan-type default.
    # F&F / Bridge / Ground Up default to interest-only; everything else
    # defaults to fully amortizing. The new amortization_style column
    # (alembic 0044) lets an underwriter flip either way per file.
    default_io = loan.type in {LoanType.FIX_AND_FLIP, LoanType.BRIDGE, LoanType.GROUND_UP}
    stored_style = (
        AmortizationStyle(loan.amortization_style) if loan.amortization_style else None
    )
    style = payload.amortization_style or stored_style or (
        AmortizationStyle.INTEREST_ONLY if default_io else AmortizationStyle.FULLY_AMORTIZING
    )
    is_io = style == AmortizationStyle.INTEREST_ONLY
    term = payload.term_months or loan.term_months or (12 if is_io else 360)
    if is_io:
        pi = round(amount * quote.final_rate / 12, 2)
    else:
        pi = round(monthly_payment(amount, quote.final_rate, term), 2)

    # Advanced-mode overrides: simulator can supply taxes / insurance / HOA
    # without persisting them to the loan record. Falls back to the loan's
    # stored values when omitted.
    annual_taxes = (
        payload.annual_taxes if payload.annual_taxes is not None else float(loan.annual_taxes or 0)
    )
    annual_insurance = (
        payload.annual_insurance
        if payload.annual_insurance is not None
        else float(loan.annual_insurance or 0)
    )
    monthly_hoa = (
        payload.monthly_hoa if payload.monthly_hoa is not None else float(loan.monthly_hoa or 0)
    )

    monthly_rent = (
        payload.monthly_rent
        if payload.monthly_rent is not None
        else float(loan.monthly_rent or 0)
    )

    # Vacancy & expense-ratio underwrites — applied to the gross rent
    # before it lands in the DSCR ratio. Stored as 0..1 fractions.
    vacancy = (
        payload.vacancy_pct if payload.vacancy_pct is not None else float(loan.vacancy_pct or 0)
    )
    expense_ratio = (
        payload.expense_ratio_pct
        if payload.expense_ratio_pct is not None
        else float(loan.expense_ratio_pct or 0)
    )
    effective_rent = float(monthly_rent or 0) * max(0.0, 1.0 - vacancy) * max(0.0, 1.0 - expense_ratio)

    # PITIA — uses the effective monthly debt service even when the loan
    # is interest-only (in that case the P&I is just the interest leg).
    pitia_pi = pi
    effective_pitia = round(
        pitia_pi + annual_taxes / 12 + annual_insurance / 12 + monthly_hoa, 2
    )

    dscr_val: float | None = None
    if effective_rent and effective_pitia:
        dscr_val = round(effective_rent / effective_pitia, 4)

    hud = build_hud_draft(
        loan_amount=amount,
        property_type=PropertyType(loan.property_type),
        loan_type=LoanType(loan.type),
        broker_origination_dollars=quote.broker_origination_dollars,
    )

    # Cash-to-close = pricing cash (origination + discount) + flat lender
    # fees + required reserves, less the day-1 construction holdback
    # (borrower doesn't wire the holdback at close; it's reserved by the
    # lender and drawn over the rehab schedule).
    lender_fees = (
        payload.lender_fees if payload.lender_fees is not None else float(loan.lender_fees or 0)
    )
    reserves_required = (
        payload.reserves_required
        if payload.reserves_required is not None
        else float(loan.reserves_required or 0)
    )
    construction_holdback_pct = (
        payload.construction_holdback_pct
        if payload.construction_holdback_pct is not None
        else float(loan.construction_holdback_pct or 0)
    )
    holdback_dollars = amount * max(0.0, min(1.0, construction_holdback_pct))
    total_cash_to_close = round(
        quote.cash_to_close_pricing + lender_fees + reserves_required - holdback_dollars, 2
    )

    # Total interest over the life of the loan (fully amortizing) or one
    # year of interest as a ballpark for IO products. Surfaced so the UI
    # can show summary stats without rerunning amortization client-side.
    if is_io:
        total_interest = round(amount * quote.final_rate, 2)  # 12 × monthly interest
    else:
        total_interest = round(pi * term - amount, 2) if term and pi else 0.0

    monthly_interest = round(amount * quote.final_rate / 12, 2)

    # Validate against fresh sizing values when available — the persisted
    # loan.ltc/loan.ltv may be stale relative to the simulator inputs.
    fresh_ltv = sizing.ltv if (sizing and sizing.ltv is not None) else (float(loan.ltv) if loan.ltv else None)
    fresh_ltc = sizing.ltc if (sizing and sizing.ltc is not None) else (float(loan.ltc) if loan.ltc else None)
    fresh_arv_ltv = (
        sizing.arv_ltv
        if (sizing and sizing.arv_ltv is not None)
        else ((amount / arv_for_sizing) if arv_for_sizing else None)
    )
    warnings = validate_loan(
        loan_type=LoanType(loan.type),
        ltv=fresh_ltv,
        ltc=fresh_ltc,
        arv_ltv=fresh_arv_ltv,
        purpose=payload.purpose or loan.purpose,
        dscr_ratio=dscr_val,
        term_months=term if is_io else None,
    )

    return RecalcResponse(
        final_rate=quote.final_rate,
        monthly_pi=pi,
        dscr=dscr_val,
        cash_to_close_pricing=quote.cash_to_close_pricing,
        hud_total=hud.total,
        warnings=[{"code": w.code, "message": w.message, "severity": w.severity} for w in warnings],
        loan_amount=amount,
        sizing=_sizing_to_breakdown(sizing) if sizing else None,
        monthly_interest=monthly_interest,
        total_interest=total_interest,
        total_cash_to_close=total_cash_to_close,
        effective_pitia=effective_pitia,
        effective_rent=round(effective_rent, 2) if effective_rent else None,
    )


@router.post("/calc", response_model=RecalcResponse)
async def free_calc(payload: FreeCalcRequest, _: GatedUser) -> RecalcResponse:
    """Loan-less what-if calculator. Same math as /recalc, but the operator
    supplies the type / amount / rate / term / etc. directly so they can
    sketch a deal before any loan record exists. Used by the standalone
    /simulator page on the desktop."""
    sizing = _try_size(
        loan_type=LoanType(payload.type),
        purpose=payload.purpose,
        arv=payload.arv,
        brv=payload.brv,
        rehab_budget=payload.rehab_budget,
        payoff=payload.payoff,
        requested_amount=payload.loan_amount,
        ltv_tier_cap=payload.ltv_tier_cap,
    )
    amount = sizing.loan_amount if sizing else payload.loan_amount
    quote = pricing_quote(payload.base_rate, amount, payload.discount_points)
    is_io = payload.type in {LoanType.FIX_AND_FLIP, LoanType.BRIDGE, LoanType.GROUND_UP}
    term = payload.term_months or (12 if is_io else 360)
    if is_io:
        pi = round(amount * quote.final_rate / 12, 2)
    else:
        pi = round(monthly_payment(amount, quote.final_rate, term), 2)

    dscr_val = None
    if payload.monthly_rent and not is_io:
        dscr_val = dscr_calc(
            float(payload.monthly_rent),
            amount,
            quote.final_rate,
            term,
            float(payload.annual_taxes or 0),
            float(payload.annual_insurance or 0),
            float(payload.monthly_hoa or 0),
        )

    hud = build_hud_draft(
        loan_amount=amount,
        property_type=PropertyType(payload.property_type),
        loan_type=LoanType(payload.type),
        broker_origination_dollars=quote.broker_origination_dollars,
    )

    # Validate against fresh sizing when we have it. Caps below are still
    # advisory in /calc (no loan record to lock down) but they're surfaced
    # so the operator sees the same warnings as /recalc would produce.
    warnings = []
    if sizing is not None:
        ws = validate_loan(
            loan_type=LoanType(payload.type),
            ltv=sizing.ltv,
            ltc=sizing.ltc,
            arv_ltv=sizing.arv_ltv,
            purpose=payload.purpose,
            dscr_ratio=dscr_val,
            term_months=term if is_io else None,
        )
        warnings = [{"code": w.code, "message": w.message, "severity": w.severity} for w in ws]

    return RecalcResponse(
        final_rate=quote.final_rate,
        monthly_pi=pi,
        dscr=dscr_val,
        cash_to_close_pricing=quote.cash_to_close_pricing,
        hud_total=hud.total,
        warnings=warnings,
        loan_amount=amount,
        sizing=_sizing_to_breakdown(sizing) if sizing else None,
    )
