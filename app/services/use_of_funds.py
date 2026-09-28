"""Exact-cent use-of-funds totals shared by every funding surface.

The requested amount stays owned by its source file. No document text, purpose
note, property value, or client-supplied ratio is treated as a budget allocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerBusiness
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketUploadLink
from app.models.deal import Deal
from app.models.loan import Loan
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.use_of_funds import (
    USE_OF_FUNDS_CATEGORIES,
    UseOfFundsItem,
    UseOfFundsPatch,
    UseOfFundsRead,
)

CENT = Decimal("0.01")
EDIT_ROLES = {
    Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER,
    Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER,
    Role.PROFESSIONAL_REFERRAL_PARTNER,
}


def require_editor(user: User) -> None:
    if user.role not in EDIT_ROLES:
        raise HTTPException(403, "This role cannot edit the file use of funds")


def _money(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            return None
        return result.quantize(CENT)
    except (ValueError, InvalidOperation):
        return None


async def source_funding_data(
    db: AsyncSession, profile: ApplicationProfile, *, intake: Any = None,
) -> tuple[Decimal | None, str | None, str | None, Any]:
    """Resolve source request/purpose without inventing a purchase-price request."""
    if intake is None and getattr(profile, "intake_id", None):
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
    dealer = (
        await db.get(DealerBusiness, profile.dealer_id)
        if getattr(profile, "dealer_id", None) else None
    )
    if intake is not None and _money(getattr(intake, "requested_loan_amount", None)) is not None:
        return (
            _money(intake.requested_loan_amount), "intake.requested_loan_amount",
            getattr(intake, "loan_purpose", None), dealer,
        )
    if dealer is not None:
        for key in ("client_requested_amount", "funding_goal"):
            amount = _money(getattr(dealer, key, None))
            if amount is not None:
                return amount, f"dealer.{key}", getattr(dealer, "funding_purpose", None), dealer
    loan = await db.get(Loan, profile.loan_id) if getattr(profile, "loan_id", None) else None
    if loan is None and getattr(profile, "deal_id", None):
        deal = await db.get(Deal, profile.deal_id)
        if deal is not None and deal.promoted_loan_id:
            loan = await db.get(Loan, deal.promoted_loan_id)
    if loan is not None and _money(loan.amount) is not None:
        purpose = loan.purpose.value if hasattr(loan.purpose, "value") else loan.purpose
        return _money(loan.amount), "loan.amount", purpose, dealer
    purpose = getattr(intake, "loan_purpose", None) or getattr(dealer, "funding_purpose", None)
    return None, None, purpose, dealer


def validated_items(raw: object) -> list[UseOfFundsItem] | None:
    """Legacy data is read only if it already has the complete structured shape."""
    if not isinstance(raw, list):
        return None
    try:
        return UseOfFundsPatch(items=raw, expected_revision=0).items
    except (ValidationError, TypeError):
        return None


def summarize(
    profile: ApplicationProfile,
    requested_amount: Decimal | None,
    requested_amount_source: str | None,
    *,
    dealer: Any = None,
    items: list[UseOfFundsItem] | None = None,
) -> UseOfFundsRead:
    warnings: list[str] = []
    source = "profile"
    if items is None:
        saved = getattr(profile, "use_of_funds", None)
        items = validated_items(saved)
        revision = getattr(profile, "use_of_funds_revision", 0) or 0
        # Clearing a saved profile budget is intentional, not permission to
        # resurrect legacy rows. Never classify legacy prose or label-only rows.
        if not items and revision == 0:
            legacy = getattr(dealer, "use_of_proceeds", None)
            legacy_items = validated_items(legacy)
            if legacy_items:
                items, source = legacy_items, "legacy_dealer"
            elif legacy:
                warnings.append("Legacy use-of-proceeds notes need structured categories before routing.")
        if saved and items is None:
            warnings.append("Saved use-of-funds data needs correction before routing.")
        if not items:
            items, source = [], "none" if revision == 0 else "profile"
    totals = {category: Decimal(0) for category in USE_OF_FUNDS_CATEGORIES}
    for item in items:
        totals[item.category] += item.amount
    total = sum(totals.values(), Decimal(0))
    real_estate_equipment = totals["real_estate"] + totals["equipment"]
    complete = requested_amount is not None and requested_amount > 0 and total == requested_amount
    if requested_amount is None or requested_amount <= 0:
        warnings.append("Set the requested amount on the source file to complete this budget.")
    elif total > requested_amount:
        warnings.append("Budget exceeds the current source request; routing percentage is unavailable.")
    # Do not round a sub-51% allocation up to 51% at the decision boundary.
    pct = float(real_estate_equipment * 100 / requested_amount) if complete else None
    return UseOfFundsRead(
        profile_id=profile.id, items=items,
        requested_amount=float(requested_amount) if requested_amount is not None else None,
        requested_amount_source=requested_amount_source,
        total=float(total), category_totals={key: float(value) for key, value in totals.items()},
        unallocated_amount=float(requested_amount - total) if requested_amount is not None else None,
        complete=complete, real_estate_equipment_amount=float(real_estate_equipment),
        real_estate_equipment_pct=pct,
        revision=getattr(profile, "use_of_funds_revision", 0) or 0,
        updated_at=getattr(profile, "use_of_funds_updated_at", None),
        updated_by_user_id=getattr(profile, "use_of_funds_updated_by_user_id", None),
        source=source, warnings=warnings,
    )


async def read_budget(db: AsyncSession, profile: ApplicationProfile) -> UseOfFundsRead:
    amount, source, _purpose, dealer = await source_funding_data(db, profile)
    return summarize(profile, amount, source, dealer=dealer)


def ai_context(budget: UseOfFundsRead) -> dict[str, Any]:
    """Bounded financial facts only: no free-text labels, actor details or notes."""
    return {
        "basis": "declared_budget_not_verified_evidence",
        "requested_amount": budget.requested_amount,
        "requested_amount_source": budget.requested_amount_source,
        "total": budget.total,
        "category_totals": budget.category_totals,
        "unallocated_amount": budget.unallocated_amount,
        "complete": budget.complete,
        "real_estate_equipment_amount": budget.real_estate_equipment_amount,
        "real_estate_equipment_pct": budget.real_estate_equipment_pct,
        "revision": budget.revision,
        "allocation_percentage_is_not_sba_occupancy": True,
    }


async def update_budget(
    db: AsyncSession, profile: ApplicationProfile, payload: UseOfFundsPatch, user: User,
) -> UseOfFundsRead:
    require_editor(user)
    return await _persist_budget(db, profile, payload, user=user)


async def update_client_budget(
    db: AsyncSession, profile: ApplicationProfile, payload: UseOfFundsPatch,
    *, room_link: BucketUploadLink,
) -> UseOfFundsRead:
    """Save only after the route has verified this room's token and PIN.

    Keep the room/profile binding explicit so no future caller can reuse a
    verified room to write another file. Client actors are never impersonated
    as an assigned staff user, and the passcode never reaches audit metadata.
    """
    if (
        room_link.bucket_id != profile.primary_bucket_id
        or room_link.status != "active"
        or (room_link.expires_at is not None and room_link.expires_at <= datetime.now(UTC))
    ):
        raise HTTPException(404, "Application room not found")
    return await _persist_budget(
        db, profile, payload, user=None, room_link_id=room_link.id,
        room_bucket_id=room_link.bucket_id,
    )


async def _persist_budget(
    db: AsyncSession, profile: ApplicationProfile, payload: UseOfFundsPatch,
    *, user: User | None, room_link_id: UUID | None = None,
    room_bucket_id: UUID | None = None,
) -> UseOfFundsRead:
    # Refresh the identity-map object after locking, so simultaneous editors
    # compare with the committed revision rather than an earlier cached value.
    locked = (
        await db.execute(
            select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
            .with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if room_bucket_id is not None and locked.primary_bucket_id != room_bucket_id:
        raise HTTPException(409, "This room's funding file changed; reload before saving")
    if (locked.use_of_funds_revision or 0) != payload.expected_revision:
        raise HTTPException(409, "Use of funds changed; reload the current budget before saving")
    amount, source, _purpose, dealer = await source_funding_data(db, locked)
    result = summarize(locked, amount, source, items=payload.items)
    exact_total = sum((item.amount for item in payload.items), Decimal(0))
    if amount is not None and exact_total > amount:
        raise HTTPException(422, "Use-of-funds total cannot exceed the requested amount")
    before = list(locked.use_of_funds or [])
    locked.use_of_funds = [item.model_dump(mode="json") for item in payload.items]
    locked.use_of_funds_revision = (locked.use_of_funds_revision or 0) + 1
    locked.use_of_funds_updated_at = datetime.now(UTC)
    locked.use_of_funds_updated_by_user_id = user.id if user else None
    from app.services.application_profiles import log_profile_action

    await log_profile_action(
        db, locked, user,
        "use_of_funds.update.application_room" if room_link_id else "use_of_funds.update",
        "Client updated the shared use-of-funds budget" if room_link_id else "Updated the shared use-of-funds budget",
        target_type="upload_link" if room_link_id else None,
        target_id=room_link_id,
        metadata={"before": before, "after": locked.use_of_funds,
                  "actor_source": "secure_room_client" if room_link_id else "staff",
                  "room_link_id": str(room_link_id) if room_link_id else None,
                  "revision": locked.use_of_funds_revision, "requested_amount_source": source,
                  "complete": result.complete, "total": str(exact_total),
                  "category_totals": result.category_totals,
                  "real_estate_equipment_pct": result.real_estate_equipment_pct},
    )
    await db.flush()
    return summarize(locked, amount, source, dealer=dealer).model_copy(update={"can_edit": True})
