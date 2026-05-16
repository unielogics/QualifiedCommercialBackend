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
from app.models.user import User
from app.schemas.intake import SmartIntakePayload, SmartIntakeResponse
from app.services import clerk as clerk_service
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
        # Dashboard-created client → guided experience by default
        # (alembic 0026). Self-signups leave this NULL and the UI
        # derives self_directed from the absence of an invite flow.
        client = Client(
            name=payload.borrower.name,
            email=payload.borrower.email,
            phone=payload.borrower.phone,
            broker_id=user.broker.id if user.broker else None,
            client_experience_mode="guided",
            client_experience_mode_reason="agent_invited",
            client_experience_mode_locked_by="agent",
        )
        db.add(client)
        await db.flush()
    elif client.client_experience_mode is None:
        # Pre-existing client (e.g. created earlier via a different
        # path) gets upgraded to guided when an agent runs them
        # through the dashboard's intake flow. Don't clobber a
        # mode the borrower already explicitly chose.
        client.client_experience_mode = "guided"
        client.client_experience_mode_reason = "agent_invited"
        client.client_experience_mode_locked_by = "agent"

    deal_id = _new_deal_id()
    loan = Loan(
        deal_id=deal_id,
        client_id=client.id,
        broker_id=user.broker.id if user.broker else None,
        address=payload.asset.address,
        city=payload.asset.city,
        state=payload.asset.state,
        property_type=payload.asset.property_type,
        sqft=payload.asset.sqft,
        annual_taxes=payload.asset.annual_taxes,
        annual_insurance=payload.asset.annual_insurance,
        type=payload.numbers.type,
        # Purchase / refinance set by SmartIntakeModal Step 1's toggle.
        # Persisted on Loan.purpose so prequal + simulator branches off it.
        purpose=payload.numbers.purpose,
        # Source attribution / ownership (alembic 0029). Captured by
        # SmartIntakeModal Step 1 + Step 4 — drives downstream
        # rev-share + funding-team queue assignment.
        source_attribution=payload.source_attribution,
        referring_agent_id=payload.referring_agent_id,
        assigned_owner_id=payload.assigned_owner_id or user.id,
        invite_behavior=payload.invite_behavior,
        # Buyer / seller (alembic 0023). Drives checklist filtering.
        side=payload.side,
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

    # Delayed collection start (broker new-file modals only). When the
    # broker delays outreach, anchor the loan's collection start in
    # the future BEFORE kickoff so materialized docs anchor off it and
    # the kickoff opener/reminders stay quiet until then. delay 0 /
    # absent ⇒ collection_starts_on stays NULL ⇒ current behavior for
    # every other caller.
    _start_delay = (
        payload.document_overrides.collection_start_delay_days
        if payload.document_overrides is not None
        else 0
    )
    if _start_delay and _start_delay > 0:
        from datetime import date as _d, timedelta as _td
        loan.collection_starts_on = _d.today() + _td(days=int(_start_delay))
        await db.flush()

    # Doc collection automation: read the firm's checklist for this
    # loan type and auto-create the Document rows + calendar reminders.
    # Idempotent — re-submits don't duplicate. Safe even if the
    # checklist is empty (function logs and returns 0).
    from app.models.app_settings import AppSettings as _AppSettings  # local import — keeps this module's import surface tight
    from app.services.loan_intake_automation import kickoff_loan as _kickoff
    settings_row = (await db.execute(select(_AppSettings).limit(1))).scalar_one_or_none()
    await _kickoff(db, loan, settings_row)

    # AI Deal Secretary bootstrap (alembic 0038). Same call as
    # POST /loans — populates ClientRequirementStatus rows from the
    # resolved playbook so the workbench picker is pre-filled.
    try:
        from app.services.ai.deal_secretary import bootstrap_requirement_status_rows
        await bootstrap_requirement_status_rows(db, loan, log_label="intake")
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "deal_secretary.bootstrap_failed (intake) loan_id=%s", loan.id
        )

    # Apply pre-loan document overrides captured in the
    # SmartIntakeModal's Documents step (alembic 0023).
    # `skip_names` flips the matching Documents from REQUESTED →
    # SKIPPED so the AI doesn't chase them; `add_items` creates
    # custom one-off Documents the agent typed in.
    overrides = payload.document_overrides
    if overrides is not None:
        from app.models.document import Document as _Doc
        from app.enums import DocStatus as _DocStatus
        from datetime import date as _date_type, timedelta as _timedelta
        # Skip + due-offset overrides both target the Documents that
        # kickoff_loan just materialized — fetch once.
        if overrides.skip_names or overrides.due_offset_overrides:
            existing = (await db.execute(
                select(_Doc).where(
                    _Doc.loan_id == loan.id,
                    _Doc.status == _DocStatus.REQUESTED,
                )
            )).scalars().all()
            skip_set = {n.strip() for n in overrides.skip_names if n}
            offset_overrides = {
                k.strip(): v for k, v in (overrides.due_offset_overrides or {}).items() if k
            }
            today = _date_type.today()
            for d in existing:
                key = d.checklist_key or d.name
                if (d.checklist_key and d.checklist_key in skip_set) or d.name in skip_set:
                    d.status = _DocStatus.SKIPPED
                elif key in offset_overrides:
                    # Wins over the LoanTypeChecklist default for THIS
                    # loan only. requested_on is set by the spawner;
                    # fall back to today if absent.
                    base = d.requested_on or today
                    d.due_date = base + _timedelta(days=int(offset_overrides[key]))
        for cust in overrides.add_items or []:
            name = (cust.name or "").strip()
            if not name:
                continue
            db.add(_Doc(
                loan_id=loan.id,
                name=name[:255],
                status=_DocStatus.REQUESTED,
                requested_on=_date_type.today(),
                due_date=cust.due_date,
                checklist_key=cust.checklist_key,
                is_other=(cust.checklist_key is None),
            ))
        await db.flush()

    # Provision a borrower User + send Clerk invite (alembic 0026 /
    # realtor overhaul). Best-effort: if Clerk isn't configured, the
    # local User row still gets created so the team list / client
    # detail page reflect that an account exists. The borrower picks
    # up sign-in via Clerk's invite redirect → JIT-binds clerk_id on
    # first sign-in (deps.get_current_user).
    #
    # invite_behavior gate (alembic 0029): super-admin / underwriter
    # can choose "save_draft" or "send_after_review" to skip the
    # Clerk invite at submit time — useful when prepping a deal
    # internally before pinging the borrower.
    if payload.borrower.email and payload.invite_behavior == "send_immediately":
        existing_user = (
            await db.execute(select(User).where(User.email == payload.borrower.email.lower()))
        ).scalar_one_or_none()
        if existing_user is None:
            new_user = User(
                email=payload.borrower.email.lower(),
                name=payload.borrower.name,
                role=Role.CLIENT,
                clerk_id=None,
            )
            db.add(new_user)
            await db.flush()
            client.user_id = new_user.id
            await db.flush()
            # Fire-and-forget Clerk invitation. No-op when
            # CLERK_SECRET_KEY is unset (local dev).
            await clerk_service.invite_user(
                email=payload.borrower.email,
                name=payload.borrower.name,
                role=Role.CLIENT,
            )
        elif client.user_id is None and existing_user.deleted_at is None:
            # Existing user without a client linkage — bind it.
            client.user_id = existing_user.id
            await db.flush()

    return SmartIntakeResponse(loan_id=loan.id, deal_id=deal_id)
