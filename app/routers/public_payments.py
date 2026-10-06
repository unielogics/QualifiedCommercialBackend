"""PIN-protected client-room endpoints for ACH authorization.

Browser values are treated only as intent.  Fee amounts, ACH class,
obligation hashes, and certificate contents are derived again on the server.
"""

from __future__ import annotations

# FastAPI dependencies are intentionally declared as callable defaults.
# ruff: noqa: B008
import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.payments import (
    AchMandate,
    FeeObligationLine,
    PaymentFundingSource,
    PaymentInstallment,
    PaymentRefund,
    PaymentTransfer,
    PrivateFundingPaymentPlan,
)
from app.routers.application_profiles import _public_application_room
from app.schemas.payments import AchMandateCreate, PaymentFundingSourceCreate
from app.services import ach_authorization, plaid_transfer
from app.services import payment_authorization as private_storage
from app.services import payments as pay

router = APIRouter(tags=["public payments"])


class RoomPaymentAccess(BaseModel):
    passcode: str = Field(min_length=6, max_length=16)


class RoomPaymentLinkRequest(RoomPaymentAccess):
    owner_type: Literal["business", "consumer"]
    purpose: Literal["fee", "private_schedule"] = "fee"


class RoomPaymentExchange(RoomPaymentLinkRequest):
    public_token: str = Field(min_length=1)
    plaid_account_id: str = Field(min_length=1, max_length=128)


class RoomPaymentAuthorize(RoomPaymentAccess):
    obligation_id: UUID
    funding_source_id: UUID
    typed_name: str = Field(min_length=2, max_length=180)
    authorized_amount_cents: int = Field(gt=0)
    consent: Literal[True]


class RoomPrivatePlanAuthorize(RoomPaymentAccess):
    funding_source_id: UUID
    typed_name: str = Field(min_length=2, max_length=180)
    consent: Literal[True]


class RoomPaymentRepairComplete(RoomPaymentAccess):
    transfer_id: UUID


def _enabled() -> None:
    if not get_settings().payments_enabled or not plaid_transfer.enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Online ACH authorization is not enabled")


def _private_enabled() -> None:
    _enabled()
    if not get_settings().private_funding_payments_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Private-funding payments are not enabled")


async def _current_private_plan(db: AsyncSession, profile_id: UUID, *, for_update: bool = False):
    stmt = (
        select(PrivateFundingPaymentPlan)
        .where(PrivateFundingPaymentPlan.application_profile_id == profile_id)
        .order_by(PrivateFundingPaymentPlan.version.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def _room(db: AsyncSession, token: str, passcode: str, request: Request):
    return await _public_application_room(db, token, passcode, request, allow_dealer=True)


async def _repairable_transfer(
    db: AsyncSession,
    *,
    purpose: Literal["fee", "private_schedule"],
    target_id: UUID,
) -> PaymentTransfer | None:
    if purpose == "fee":
        target_clause = PaymentTransfer.fee_obligation_id == target_id
    else:
        installment_ids = select(PaymentInstallment.id).where(
            PaymentInstallment.plan_id == target_id
        )
        target_clause = PaymentTransfer.installment_id.in_(installment_ids)
    return (
        await db.execute(
            select(PaymentTransfer)
            .where(
                target_clause,
                PaymentTransfer.status == "action_required",
                PaymentTransfer.plaid_authorization_id.is_not(None),
                func.upper(PaymentTransfer.provider_failure_code).in_(
                    pay.PLAID_LINK_REPAIR_CODES
                ),
            )
            .order_by(PaymentTransfer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _state(db: AsyncSession, profile) -> dict:
    settings = get_settings()
    obligation = await pay.current_obligation(db, profile.id)
    if obligation and obligation.status == "draft":
        obligation = None
    agreement_ready = bool(
        obligation and await pay._fee_agreement_is_current(db, obligation)
    )
    business_name, client_name, client_email = await pay._profile_identity(db, profile)
    funding_confirmation = await pay._current_confirmation(db, profile.id)
    funding_source = (
        await db.execute(
            select(PaymentFundingSource)
            .where(
                PaymentFundingSource.application_profile_id == profile.id,
                PaymentFundingSource.status == "verified",
            )
            .order_by(PaymentFundingSource.verified_at.desc().nullslast(), PaymentFundingSource.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    mandate = None
    transfer = None
    line_rows: list[FeeObligationLine] = []
    collected_cents = 0
    refunded_cents = 0
    if obligation:
        line_rows = (
            await db.execute(
                select(FeeObligationLine)
                .where(FeeObligationLine.obligation_id == obligation.id)
                .order_by(FeeObligationLine.line_type)
            )
        ).scalars().all()
        mandate = (
            await db.execute(
                select(AchMandate)
                .where(AchMandate.fee_obligation_id == obligation.id)
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        transfers = (
            await db.execute(
                select(PaymentTransfer)
                .where(PaymentTransfer.fee_obligation_id == obligation.id)
                .order_by(PaymentTransfer.created_at.desc())
            )
        ).scalars().all()
        transfer = transfers[0] if transfers else None
        collected_cents = sum(row.amount_cents for row in transfers if row.status == "funds_available")
        if transfers:
            refunded_cents = int((await db.execute(
                select(func.coalesce(func.sum(PaymentRefund.amount_cents), 0))
                .where(
                    PaymentRefund.transfer_id.in_([row.id for row in transfers]),
                    PaymentRefund.status.in_(pay.REFUND_COMPLETED_STATUSES),
                )
            )).scalar_one())
    mandate_current = False
    if obligation and mandate and funding_source:
        mandate_current = bool(
            mandate.status == "active"
            and mandate.revoked_at is None
            and pay._mandate_matches_source(mandate, funding_source)
            and mandate.obligation_sha256
            == await pay.fee_obligation_sha256(db, obligation)
        )

    plan = (
        await db.execute(
            select(PrivateFundingPaymentPlan)
            .where(PrivateFundingPaymentPlan.application_profile_id == profile.id)
            .order_by(PrivateFundingPaymentPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    installments = []
    plan_mandate = None
    plan_transfer = None
    if plan:
        installments = (
            await db.execute(
                select(PaymentInstallment)
                .where(PaymentInstallment.plan_id == plan.id)
                .order_by(PaymentInstallment.sequence)
            )
        ).scalars().all()
        plan_mandate = (
            await db.execute(
                select(AchMandate)
                .where(AchMandate.private_plan_id == plan.id)
                .order_by(AchMandate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if installments:
            plan_transfer = (
                await db.execute(
                    select(PaymentTransfer)
                    .where(
                        PaymentTransfer.installment_id.in_(
                            [row.id for row in installments]
                        )
                    )
                    .order_by(PaymentTransfer.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    plan_mandate_current = bool(
        plan
        and plan_mandate
        and funding_source
        and plan_mandate.status == "active"
        and plan_mandate.revoked_at is None
        and pay._mandate_matches_source(plan_mandate, funding_source)
        and plan_mandate.obligation_sha256 == plan.schedule_sha256
        and str(plan_mandate.ach_class).upper() == "CCD"
        and str(funding_source.ach_class).upper() == "CCD"
    )

    remaining_ach = obligation.client_ach_cents if obligation else 0
    line_payload = []
    for line in sorted(line_rows, key=lambda row: 0 if row.line_type == "origination" else 1):
        exact = getattr(line, "client_ach_cents", None)
        collection = int(exact) if exact is not None else min(remaining_ach, line.amount_cents)
        remaining_ach = max(0, remaining_ach - collection)
        line_payload.append({
            "id": str(line.id),
            "label": "Origination fee" if line.line_type == "origination" else "Consulting fee",
            "amount_cents": line.amount_cents,
            "collection_amount_cents": collection,
            "agreement_reference": obligation.agreement_reference if obligation else None,
        })
    return {
        "available": bool(obligation or plan),
        "payments_enabled": bool(settings.payments_enabled and plaid_transfer.enabled()),
        "private_funding_payments_enabled": bool(
            settings.payments_enabled
            and settings.private_funding_payments_enabled
            and plaid_transfer.enabled()
        ),
        "business_name": business_name,
        "obligation": ({
            "id": str(obligation.id),
            "status": obligation.status,
            "currency": obligation.currency,
            "lines": line_payload,
            "client_ach_cents": obligation.client_ach_cents,
            "accepted_amount": obligation.accepted_amount,
            "origination_points": obligation.origination_points,
            "origination_fee_cents": obligation.origination_fee_cents,
            "consulting_fee_cents": obligation.consulting_fee_cents,
            "gross_fee_cents": obligation.gross_fee_cents,
            "agreement_reference": obligation.agreement_reference,
            "agreement_sha256": obligation.agreement_sha256,
            "agreement_document_id": (
                str(obligation.agreement_document_id)
                if obligation.agreement_document_id
                else None
            ),
            "calculation": {
                "basis": "accepted_amount",
                "formula": "accepted amount × origination percentage",
            },
            "collected_cents": max(0, collected_cents - refunded_cents),
            "outstanding_cents": max(0, obligation.client_ach_cents - collected_cents + refunded_cents),
            "agreement_ready": agreement_ready,
        } if obligation else None),
        "funding_confirmation": ({
            "actual_funding_date": funding_confirmation.actual_funding_date,
            "actual_funded_amount": funding_confirmation.actual_funded_amount,
            "funding_party_name": funding_confirmation.funding_party_name,
        } if funding_confirmation else None),
        "funding_source": ({
            "id": str(funding_source.id),
            "status": "connected",
            "owner_type": funding_source.owner_type,
            "institution_name": funding_source.institution_name,
            "account_name": funding_source.account_name,
            "account_mask": funding_source.account_mask,
        } if funding_source else None),
        "mandate": ({
            "id": str(mandate.id),
            "status": "signed" if mandate_current else mandate.status,
            "current": mandate_current,
            "ach_class": mandate.ach_class.lower(),
            "authorized_amount_cents": mandate.authorized_amount_cents,
            "payer_name": mandate.payer_name,
            "signed_at": mandate.signed_at,
            "certificate_available": bool(mandate.certificate_s3_key),
        } if mandate else None),
        "transfer": ({
            "id": str(transfer.id),
            "status": transfer.status,
            "amount_cents": transfer.amount_cents,
            "submitted_at": transfer.submitted_at,
            "funds_available_at": transfer.funds_available_at,
            "returned_at": transfer.returned_at,
            "repair_needed": bool(
                transfer.status == "action_required"
                and transfer.plaid_authorization_id
                and (transfer.provider_failure_code or "").upper()
                in pay.PLAID_LINK_REPAIR_CODES
            ),
            "resume_eligible": pay._same_intent_resume_eligible(transfer),
        } if transfer else None),
        "private_plan": ({
            "id": str(plan.id),
            "status": plan.status,
            "cadence": plan.cadence,
            "total_amount_cents": plan.total_amount_cents,
            "creditor_name": plan.creditor_name,
            "payee_name": plan.payee_name,
            "settlement_destination_ref": plan.settlement_destination_ref,
            "agreement_reference": plan.agreement_reference,
            "agreement_sha256": plan.agreement_sha256,
            "agreement_executed_at": plan.agreement_executed_at,
            "production_package_id": str(plan.production_package_id),
            "production_package_revision_id": (
                str(plan.production_package_revision_id)
                if plan.production_package_revision_id
                else None
            ),
            "production_term_sheet_id": str(plan.production_term_sheet_id),
            "production_term_sheet_version": plan.production_term_sheet_version,
            "schedule_sha256": plan.schedule_sha256,
            "next_due_date": plan.next_due_date,
            "mandate": ({
                "id": str(plan_mandate.id),
                "status": "signed" if plan_mandate_current else plan_mandate.status,
                "current": plan_mandate_current,
                "authorized_amount_cents": plan_mandate.authorized_amount_cents,
                "payer_name": plan_mandate.payer_name,
                "signed_at": plan_mandate.signed_at,
                "certificate_available": bool(plan_mandate.certificate_s3_key),
            } if plan_mandate else None),
            "transfer": ({
                "id": str(plan_transfer.id),
                "status": plan_transfer.status,
                "amount_cents": plan_transfer.amount_cents,
                "submitted_at": plan_transfer.submitted_at,
                "funds_available_at": plan_transfer.funds_available_at,
                "returned_at": plan_transfer.returned_at,
                "repair_needed": bool(
                    plan_transfer.status == "action_required"
                    and plan_transfer.plaid_authorization_id
                    and (plan_transfer.provider_failure_code or "").upper()
                    in pay.PLAID_LINK_REPAIR_CODES
                ),
                "resume_eligible": pay._same_intent_resume_eligible(plan_transfer),
            } if plan_transfer else None),
            "installments": [{
                "id": str(row.id), "sequence": row.sequence, "due_date": row.due_date,
                "amount_cents": row.amount_cents, "status": row.status,
            } for row in installments],
            "can_revoke_future_authorization": bool(
                plan.status == "active" and plan_mandate and plan_mandate.status == "active"
            ),
        } if plan else None),
        "client_name": client_name,
        "client_email": client_email,
    }


@router.post("/application-profiles/public/room/{token}/payments/state")
async def public_payment_state(
    token: str, payload: RoomPaymentAccess, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/link-token")
async def public_payment_link_token(
    token: str, payload: RoomPaymentLinkRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.purpose == "private_schedule":
        _private_enabled()
    else:
        _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    target_id: UUID
    plan = None
    obligation = None
    if payload.purpose == "private_schedule":
        if payload.owner_type != "business":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private schedules require a business account")
        plan = await _current_private_plan(db, profile.id)
        if not plan:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no private schedule awaiting authorization")
        target_id = plan.id
    else:
        obligation = await pay.current_obligation(db, profile.id)
        if not obligation or obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no active fee authorization request")
        if obligation.client_ach_cents <= 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "No client ACH amount is allocated")
        target_id = obligation.id
    repair_transfer = await _repairable_transfer(
        db,
        purpose=payload.purpose,
        target_id=target_id,
    )
    if payload.purpose == "private_schedule":
        if not repair_transfer and plan.status not in {"draft", "paused"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "There is no private schedule awaiting authorization",
            )
    link_token = await plaid_transfer.create_link_token(
        client_user_id=f"payment:{payload.purpose}:{profile.id}:{target_id}",
        authorization_id=(
            repair_transfer.plaid_authorization_id if repair_transfer else None
        ),
    )
    return {
        "link_token": link_token,
        "owner_type": payload.owner_type,
        "purpose": payload.purpose,
        "repair_mode": bool(repair_transfer),
        "exchange_required": not bool(repair_transfer),
        "transfer_id": str(repair_transfer.id) if repair_transfer else None,
    }


@router.post(
    "/application-profiles/public/room/{token}/payments/repair-complete"
)
async def public_payment_repair_complete(
    token: str,
    payload: RoomPaymentRepairComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    await pay.resume_action_required_transfer(
        db,
        transfer_id=payload.transfer_id,
        profile_id=profile.id,
        actor_id=None,
        bank_repair_completed=True,
    )
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/exchange")
async def public_payment_exchange(
    token: str, payload: RoomPaymentExchange, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    if payload.purpose == "private_schedule":
        _private_enabled()
    else:
        _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    if payload.purpose == "private_schedule":
        if payload.owner_type != "business":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Private schedules require a business account")
        plan = await _current_private_plan(db, profile.id)
        if not plan or plan.status not in {"draft", "paused"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no private schedule awaiting authorization")
    else:
        obligation = await pay.current_obligation(db, profile.id)
        if not obligation or obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "There is no active fee authorization request")
    access_token, item_id = await plaid_transfer.exchange_public_token(payload.public_token)
    accounts = await plaid_transfer.accounts(access_token)
    account = next((row for row in accounts if row["account_id"] == payload.plaid_account_id), None)
    if not account:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The selected payment account was not returned by Plaid")
    if account.get("type") != "depository" or account.get("subtype") not in {"checking", "savings"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose an eligible checking or savings account")
    # Serialize source replacement per file. The provider exchange happens
    # before this lock so no database lock is held while waiting on Plaid.
    await pay._lock_profile(db, profile.id)
    previous = (
        await db.execute(select(PaymentFundingSource).where(
            PaymentFundingSource.application_profile_id == profile.id,
            PaymentFundingSource.status == "verified",
        ).with_for_update())
    ).scalars().all()
    revoked_source_ids = [row.id for row in previous]
    source_mandates = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.funding_source_id.in_(revoked_source_ids),
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all() if revoked_source_ids else []
    now = datetime.now(UTC)
    for mandate in source_mandates:
        mandate.status = "superseded"
        mandate.revoked_at = now
    for row in previous:
        row.status = "revoked"
        row.revoked_at = now
    await pay.cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=[mandate.id for mandate in source_mandates],
        reason="payment_source_reconnected",
    )
    await pay.cancel_unclaimed_transfer_intents(
        db,
        funding_source_ids=revoked_source_ids,
        reason="payment_source_replaced",
    )
    source = await pay.create_funding_source(db, profile=profile, payload=PaymentFundingSourceCreate(
        owner_type=payload.owner_type,
        plaid_item_id=item_id,
        plaid_account_id=payload.plaid_account_id,
        access_token_ciphertext=plaid_transfer.encrypt_access_token(access_token),
        account_name=str(account.get("official_name") or account.get("name") or "Bank account"),
        account_mask=str(account.get("mask") or "")[-8:],
        account_subtype=str(account.get("subtype") or ""),
        metadata_json={"payment_only": True, "account_type": account.get("type")},
    ))
    # A reconnect creates a new immutable funding-source version. Historical
    # mandates and transfers retain the exact account/classification evidence
    # that was used when they were authorized.
    await pay.log_event(
        db, profile_id=profile.id, actor_id=None, event_type="payment_source.connected",
        entity_type="payment_funding_source", entity_id=source.id,
        summary="Client connected a dedicated ACH payment account",
        metadata={"owner_type": payload.owner_type, "ach_class": source.ach_class},
    )
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/authorize")
async def public_payment_authorize(
    token: str, payload: RoomPaymentAuthorize, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    obligation = await pay.current_obligation(db, profile.id, for_update=True)
    if not obligation or obligation.id != payload.obligation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The fee obligation changed; refresh before authorizing")
    if obligation.status not in {"awaiting_authorization", "authorized", "returned"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This fee request can no longer be authorized")
    if not await pay._fee_agreement_is_current(db, obligation):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The signed fee agreement artifact changed; ask QC to prepare a new request",
        )
    if payload.authorized_amount_cents != obligation.client_ach_cents:
        raise HTTPException(status.HTTP_409_CONFLICT, "The authorization amount changed; refresh before signing")
    source = await db.get(PaymentFundingSource, payload.funding_source_id)
    if not source or source.application_profile_id != profile.id or source.status != "verified":
        raise HTTPException(status.HTTP_409_CONFLICT, "Reconnect the ACH payment account")
    lines = (
        await db.execute(
            select(FeeObligationLine)
            .where(FeeObligationLine.obligation_id == obligation.id)
            .order_by(FeeObligationLine.line_type)
        )
    ).scalars().all()
    obligation_sha = await pay.fee_obligation_sha256(db, obligation)
    active = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.fee_obligation_id == obligation.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .order_by(AchMandate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        active
        and active.funding_source_id == source.id
        and active.authorized_amount_cents == obligation.client_ach_cents
        and active.obligation_sha256 == obligation_sha
        and active.typed_name.casefold() == payload.typed_name.strip().casefold()
    ):
        return await _state(db, profile)
    business_name, client_name, client_email = await pay._profile_identity(db, profile)
    mandate = await pay.create_ach_mandate(db, profile=profile, payload=AchMandateCreate(
        funding_source_id=source.id,
        fee_obligation_id=obligation.id,
        authorized_amount_cents=obligation.client_ach_cents,
        authorization_text_version=ach_authorization.AUTHORIZATION_TEXT_VERSION,
        obligation_sha256=obligation_sha,
        typed_name=payload.typed_name.strip(),
        payer_name=payload.typed_name.strip(),
        payer_email=client_email,
    ), ip_address=(request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)),
       user_agent=(request.headers.get("user-agent") or "")[:512] or None)
    mandate.signature_sha256 = ach_authorization.signature_hash(
        typed_name=mandate.typed_name, obligation_sha256=obligation_sha,
        signed_at=mandate.signed_at, ip_address=mandate.ip_address,
    )
    fee_lines: list[tuple[str, int, str | None]] = []
    remaining = obligation.client_ach_cents
    for row in sorted(lines, key=lambda item: 0 if item.line_type == "origination" else 1):
        exact = getattr(row, "client_ach_cents", None)
        amount = int(exact) if exact is not None else min(remaining, row.amount_cents)
        remaining = max(0, remaining - amount)
        if amount:
            fee_lines.append(("Origination fee" if row.line_type == "origination" else "Consulting fee", amount, obligation.agreement_reference))
    pdf = await asyncio.to_thread(
        ach_authorization.render_certificate_pdf,
        mandate=mandate, funding_source=source, business_name=business_name,
        client_name=client_name, client_email=client_email, fee_lines=fee_lines,
        accepted_amount=obligation.accepted_amount,
        origination_points=obligation.origination_points,
        origination_fee_cents=obligation.origination_fee_cents,
        consulting_fee_cents=obligation.consulting_fee_cents,
        agreement_reference=obligation.agreement_reference,
        agreement_sha256=obligation.agreement_sha256,
    )
    key = f"payments/ach-mandates/{profile.id}/{mandate.id}/certificate.pdf"
    await asyncio.to_thread(private_storage.put_private_s3_object, key=key, body=pdf, content_type="application/pdf")
    mandate.certificate_s3_key = key
    mandate.certificate_sha256 = hashlib.sha256(pdf).hexdigest()
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/private-plans/{plan_id}/authorize")
async def public_private_plan_authorize(
    token: str,
    plan_id: UUID,
    payload: RoomPrivatePlanAuthorize,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _private_enabled()
    _link, profile = await _room(db, token, payload.passcode, request)
    plan = await _current_private_plan(db, profile.id, for_update=True)
    if not plan or plan.id != plan_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "The private schedule changed; refresh before authorizing")
    if plan.status not in {"draft", "paused"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "This private schedule can no longer be authorized")
    if not await pay._private_agreement_is_current(db, plan=plan):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The executed stage-two agreement changed; ask QC to replace this schedule",
        )
    source = await db.get(PaymentFundingSource, payload.funding_source_id)
    if (
        not source
        or source.application_profile_id != profile.id
        or source.status != "verified"
        or source.revoked_at
        or source.owner_type != "business"
        or source.ach_class != "CCD"
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect an eligible business checking account")
    installments = (
        await db.execute(
            select(PaymentInstallment)
            .where(PaymentInstallment.plan_id == plan.id)
            .order_by(PaymentInstallment.sequence)
        )
    ).scalars().all()
    if not installments or sum(row.amount_cents for row in installments) != plan.total_amount_cents:
        raise HTTPException(status.HTTP_409_CONFLICT, "The fixed schedule is incomplete; ask QC to review it")
    active = (
        await db.execute(
            select(AchMandate)
            .where(
                AchMandate.private_plan_id == plan.id,
                AchMandate.status == "active",
                AchMandate.revoked_at.is_(None),
            )
            .order_by(AchMandate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        active
        and active.funding_source_id == source.id
        and active.authorized_amount_cents == plan.total_amount_cents
        and active.obligation_sha256 == plan.schedule_sha256
        and active.typed_name.casefold() == payload.typed_name.strip().casefold()
    ):
        return await _state(db, profile)
    business_name, client_name, client_email = await pay._profile_identity(db, profile)
    mandate = await pay.create_ach_mandate(
        db,
        profile=profile,
        payload=AchMandateCreate(
            funding_source_id=source.id,
            private_plan_id=plan.id,
            authorized_amount_cents=plan.total_amount_cents,
            authorization_text_version=ach_authorization.PRIVATE_SCHEDULE_AUTHORIZATION_TEXT_VERSION,
            obligation_sha256=plan.schedule_sha256,
            typed_name=payload.typed_name.strip(),
            payer_name=payload.typed_name.strip(),
            payer_email=client_email,
        ),
        ip_address=(
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None)
        ),
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
    )
    mandate.signature_sha256 = ach_authorization.signature_hash(
        typed_name=mandate.typed_name,
        obligation_sha256=plan.schedule_sha256,
        signed_at=mandate.signed_at,
        ip_address=mandate.ip_address,
    )
    pdf = await asyncio.to_thread(
        ach_authorization.render_private_schedule_certificate_pdf,
        mandate=mandate,
        funding_source=source,
        business_name=business_name,
        client_name=client_name,
        client_email=client_email,
        creditor_name=plan.creditor_name,
        payee_name=plan.payee_name,
        settlement_destination_ref=plan.settlement_destination_ref,
        agreement_reference=plan.agreement_reference,
        production_term_sheet_id=plan.production_term_sheet_id,
        production_term_sheet_version=plan.production_term_sheet_version,
        schedule_sha256=plan.schedule_sha256,
        cadence=plan.cadence,
        installments=[
            (row.sequence, row.due_date.isoformat(), row.amount_cents) for row in installments
        ],
    )
    key = f"payments/ach-mandates/{profile.id}/{mandate.id}/certificate.pdf"
    await asyncio.to_thread(
        private_storage.put_private_s3_object,
        key=key,
        body=pdf,
        content_type="application/pdf",
    )
    mandate.certificate_s3_key = key
    mandate.certificate_sha256 = hashlib.sha256(pdf).hexdigest()
    await db.commit()
    return await _state(db, profile)


@router.post("/application-profiles/public/room/{token}/payments/mandates/{mandate_id}/certificate")
async def public_payment_certificate(
    token: str, mandate_id: UUID, payload: RoomPaymentAccess, request: Request,
    db: AsyncSession = Depends(get_db),
):
    _link, profile = await _room(db, token, payload.passcode, request)
    mandate = await db.get(AchMandate, mandate_id)
    if not mandate or mandate.application_profile_id != profile.id or not mandate.certificate_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Authorization certificate not found")
    url = private_storage.presign_private_s3_object(
        mandate.certificate_s3_key, ttl_seconds=300, download_filename="QC-ACH-authorization.pdf"
    )
    if not url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authorization certificate is unavailable")
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/application-profiles/public/room/{token}/payments/private-plans/{plan_id}/revoke")
async def public_revoke_private_plan(
    token: str, plan_id: UUID, payload: RoomPaymentAccess, request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _link, profile = await _room(db, token, payload.passcode, request)
    plan = (
        await db.execute(select(PrivateFundingPaymentPlan).where(
            PrivateFundingPaymentPlan.id == plan_id,
            PrivateFundingPaymentPlan.application_profile_id == profile.id,
        ).with_for_update())
    ).scalar_one_or_none()
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Private payment schedule not found")
    if plan.status in {"cancelled", "completed", "superseded"}:
        return await _state(db, profile)
    now = datetime.now(UTC)
    mandates = (
        await db.execute(select(AchMandate).where(
            AchMandate.private_plan_id == plan.id, AchMandate.status == "active"
        ).with_for_update())
    ).scalars().all()
    mandate_ids = [mandate.id for mandate in mandates]
    for mandate in mandates:
        mandate.status = "revoked"
        mandate.revoked_at = now
    await pay.cancel_unclaimed_transfer_intents(
        db,
        mandate_ids=mandate_ids,
        reason="schedule_authorization_revoked",
    )
    plan.status = "paused"
    plan.record_version += 1
    await pay.log_event(
        db, profile_id=profile.id, actor_id=None, event_type="private_plan.authorization_revoked",
        entity_type="private_payment_plan", entity_id=plan.id,
        summary="Client revoked future scheduled-payment authorization",
    )
    await db.commit()
    return await _state(db, profile)
