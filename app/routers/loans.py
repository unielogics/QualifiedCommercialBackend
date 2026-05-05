"""Loans CRUD + stage transitions + simulator /recalc.

Recalc is the hot path for the desktop HUD sim and mobile Simulator slider.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import LoanStage, LoanType, MessageFrom, PropertyType, Role
from app.models.activity import Activity
from app.models.loan import Loan
from app.models.message import Message
from app.schemas.activity import ActivityRead
from app.schemas.loan import FreeCalcRequest, LoanCreate, LoanRead, LoanUpdate, RecalcRequest, RecalcResponse, StageTransition
from app.services.ai.vector_store import log_event as vector_log
from app.services.email.parser import inject_deal_id
from app.services.hud_template import build_hud_draft
from app.services.lender_matrix import validate_loan
from app.services.math import dscr as dscr_calc
from app.services.math import monthly_payment, pricing_quote

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
    await db.flush()
    await db.refresh(loan)
    return LoanRead.model_validate(loan)


@router.post("/{loan_id}/recalc", response_model=RecalcResponse)
async def recalc(
    loan_id: UUID,
    payload: RecalcRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RecalcResponse:
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")

    base_rate = payload.base_rate or float(loan.base_rate or 0.07)
    amount = payload.loan_amount or float(loan.amount)
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

    warnings = validate_loan(
        loan_type=LoanType(loan.type),
        ltv=float(loan.ltv) if loan.ltv else None,
        ltc=float(loan.ltc) if loan.ltc else None,
        arv_ltv=(float(loan.amount) / float(loan.arv)) if loan.arv else None,
        purpose=loan.purpose,
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
    )


@router.post("/calc", response_model=RecalcResponse)
async def free_calc(payload: FreeCalcRequest, _: CurrentUser) -> RecalcResponse:
    """Loan-less what-if calculator. Same math as /recalc, but the operator
    supplies the type / amount / rate / term / etc. directly so they can
    sketch a deal before any loan record exists. Used by the standalone
    /simulator page on the desktop."""
    quote = pricing_quote(payload.base_rate, payload.loan_amount, payload.discount_points)
    is_io = payload.type in {LoanType.FIX_AND_FLIP, LoanType.BRIDGE, LoanType.GROUND_UP}
    term = payload.term_months or (12 if is_io else 360)
    if is_io:
        pi = round(payload.loan_amount * quote.final_rate / 12, 2)
    else:
        pi = round(monthly_payment(payload.loan_amount, quote.final_rate, term), 2)

    dscr_val = None
    if payload.monthly_rent and not is_io:
        dscr_val = dscr_calc(
            float(payload.monthly_rent),
            payload.loan_amount,
            quote.final_rate,
            term,
            float(payload.annual_taxes or 0),
            float(payload.annual_insurance or 0),
            float(payload.monthly_hoa or 0),
        )

    hud = build_hud_draft(
        loan_amount=payload.loan_amount,
        property_type=PropertyType(payload.property_type),
        loan_type=LoanType(payload.type),
        broker_origination_dollars=quote.broker_origination_dollars,
    )

    return RecalcResponse(
        final_rate=quote.final_rate,
        monthly_pi=pi,
        dscr=dscr_val,
        cash_to_close_pricing=quote.cash_to_close_pricing,
        hud_total=hud.total,
        warnings=[],  # Free-calc skips lender_matrix validation since there's no loan record to validate
    )
