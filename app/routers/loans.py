"""Loans CRUD + stage transitions + simulator /recalc.

Recalc is the hot path for the desktop HUD sim and mobile Simulator slider.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, GatedUser
from app.enums import LoanStage, LoanType, LoanPurpose, MessageFrom, PropertyType, Role
from app.models.activity import Activity
from app.models.loan import Loan
from app.models.message import Message
from app.schemas.activity import ActivityRead
from app.schemas.document import RequiredDocumentRead
from app.schemas.loan import FreeCalcRequest, LoanCreate, LoanRead, LoanUpdate, RecalcRequest, RecalcResponse, SizingBreakdown, StageTransition
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
    super_admin sees everything; loan_exec sees everything (UW)."""
    if user.role == Role.CLIENT and user.client:
        return stmt.where(Loan.client_id == user.client.id)
    if user.role == Role.BROKER and user.broker:
        return stmt.where(Loan.broker_id == user.broker.id)
    return stmt


@router.get("", response_model=list[LoanRead])
async def list_loans(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[LoanRead]:
    stmt = _scope_query(user, select(Loan).order_by(Loan.created_at.desc()))
    rows = (await db.execute(stmt)).scalars().all()
    return [LoanRead.model_validate(r) for r in rows]


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
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients cannot create loans")
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
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Read-only")
    loan = await db.get(Loan, loan_id)
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
            kind="loan.updated",
            summary=f"Updated {', '.join(changes.keys())}",
            payload=changes,
        )
    )
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


@router.post("/{loan_id}/stage", response_model=LoanRead)
async def transition_stage(
    loan_id: UUID,
    payload: StageTransition,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> LoanRead:
    loan = await db.get(Loan, loan_id)
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
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    base_rate = payload.base_rate or float(loan.base_rate or 0.07)
    requested_amount = payload.loan_amount or float(loan.amount)
    sizing = _try_size(
        loan_type=LoanType(loan.type),
        purpose=payload.purpose or loan.purpose,
        arv=payload.arv if payload.arv is not None else (float(loan.arv) if loan.arv else None),
        brv=payload.brv,
        rehab_budget=payload.rehab_budget,
        payoff=payload.payoff,
        requested_amount=requested_amount,
        ltv_tier_cap=payload.ltv_tier_cap,
    )
    amount = sizing.loan_amount if sizing else requested_amount
    quote = pricing_quote(base_rate, amount, payload.discount_points)

    is_io = loan.type in {LoanType.FIX_AND_FLIP, LoanType.BRIDGE, LoanType.GROUND_UP}
    term = loan.term_months or (12 if is_io else 360)
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

    dscr_val = None
    if loan.monthly_rent and not is_io:
        dscr_val = dscr_calc(
            float(loan.monthly_rent),
            amount,
            quote.final_rate,
            term,
            annual_taxes,
            annual_insurance,
            monthly_hoa,
        )

    hud = build_hud_draft(
        loan_amount=amount,
        property_type=PropertyType(loan.property_type),
        loan_type=LoanType(loan.type),
        broker_origination_dollars=quote.broker_origination_dollars,
    )

    # Validate against fresh sizing values when available — the persisted
    # loan.ltc/loan.ltv may be stale relative to the simulator inputs.
    fresh_ltv = sizing.ltv if (sizing and sizing.ltv is not None) else (float(loan.ltv) if loan.ltv else None)
    fresh_ltc = sizing.ltc if (sizing and sizing.ltc is not None) else (float(loan.ltc) if loan.ltc else None)
    fresh_arv_ltv = (
        sizing.arv_ltv
        if (sizing and sizing.arv_ltv is not None)
        else ((amount / float(loan.arv)) if loan.arv else None)
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
