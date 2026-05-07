"""SmartIntake submission — creates a Loan + draft HUD + activity log + vector entry."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import LoanStage, LoanType, PropertyType, Role
from app.models.activity import Activity
from app.models.client import Client
from app.models.hud import HudLineItem
from app.models.loan import Loan
from app.schemas.intake import SmartIntakePayload, SmartIntakeResponse
from app.services.ai.vector_store import log_event as vector_log
from app.services.hud_template import build_hud_draft
from app.services.math import pricing_quote

router = APIRouter(prefix="/intake", tags=["intake"])


def _new_deal_id() -> str:
    return f"L-{secrets.randbelow(9000) + 1000}"


@router.post("", response_model=SmartIntakeResponse, status_code=status.HTTP_201_CREATED)
async def submit_intake(
    payload: SmartIntakePayload, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> SmartIntakeResponse:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator-only")

    # Find or create the client by email
    client = (
        await db.execute(select(Client).where(Client.email == payload.borrower.email))
    ).scalar_one_or_none()
    if client is None:
        client = Client(
            name=payload.borrower.name,
            email=payload.borrower.email,
            phone=payload.borrower.phone,
            broker_id=user.broker.id if user.broker else None,
        )
        db.add(client)
        await db.flush()

    deal_id = _new_deal_id()
    loan = Loan(
        deal_id=deal_id,
        client_id=client.id,
        broker_id=user.broker.id if user.broker else None,
        address=payload.asset.address,
        city=payload.asset.city,
        property_type=payload.asset.property_type,
        sqft=payload.asset.sqft,
        annual_taxes=payload.asset.annual_taxes,
        annual_insurance=payload.asset.annual_insurance,
        type=payload.numbers.type,
        stage=LoanStage.PREQUALIFIED,
        amount=payload.numbers.amount,
        ltv=payload.numbers.ltv,
        ltc=payload.numbers.ltc,
        arv=payload.numbers.arv,
        base_rate=payload.numbers.base_rate,
        monthly_rent=payload.numbers.monthly_rent,
    )
    db.add(loan)
    await db.flush()

    # Seed HUD-1 draft
    quote = pricing_quote(payload.numbers.base_rate, payload.numbers.amount, 0.0)
    hud = build_hud_draft(
        loan_amount=payload.numbers.amount,
        property_type=PropertyType(payload.asset.property_type),
        loan_type=LoanType(payload.numbers.type),
        broker_origination_dollars=quote.broker_origination_dollars,
    )
    for item in hud.items:
        db.add(
            HudLineItem(
                loan_id=loan.id,
                code=item.code,
                label=item.label,
                amount=item.amount,
                category=item.category,
                editable=item.editable,
            )
        )

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="intake.submitted",
            summary=f"Smart Intake submitted — {deal_id}",
            payload=payload.model_dump(mode="json"),
        )
    )
    await vector_log(
        db,
        loan_id=loan.id,
        deal_id=deal_id,
        kind="intake.submitted",
        content=(
            f"Smart Intake — {payload.borrower.name}, {payload.asset.address}, "
            f"{payload.numbers.type.value} ${payload.numbers.amount:,.0f}, "
            f"LTV {payload.numbers.ltv:.0%}"
        ),
    )

    # Doc collection automation: read the firm's checklist for this
    # loan type and auto-create the Document rows + calendar reminders.
    # Idempotent — re-submits don't duplicate. Safe even if the
    # checklist is empty (function logs and returns 0).
    from app.models.app_settings import AppSettings as _AppSettings  # local import — keeps this module's import surface tight
    from app.services.loan_intake_automation import kickoff_loan as _kickoff
    settings_row = (await db.execute(select(_AppSettings).limit(1))).scalar_one_or_none()
    await _kickoff(db, loan, settings_row)

    return SmartIntakeResponse(loan_id=loan.id, deal_id=deal_id)
