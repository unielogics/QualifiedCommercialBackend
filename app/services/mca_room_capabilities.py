"""Expose later-stage room actions only when this application has requested them."""
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payments import FeeObligation, PrivateFundingPaymentPlan


async def payment_methods_available(db: AsyncSession, profile_id: UUID) -> bool:
    """Read an existing authorization/plan; never prepare fees or payments.

    A different application owned by the same client does not enable this room.
    A fresh three-document MCA intake has neither and keeps its navigation focused.
    """
    inactive = ("cancelled", "canceled", "superseded")
    sent_fee = exists().where(
        FeeObligation.application_profile_id == profile_id,
        FeeObligation.superseded_at.is_(None),
        FeeObligation.status.not_in(inactive),
        FeeObligation.authorization_sent_at.is_not(None),
    )
    assigned_plan = exists().where(
        PrivateFundingPaymentPlan.application_profile_id == profile_id,
        PrivateFundingPaymentPlan.status.in_(("authorized", "active", "paused", "completed")),
    )
    return bool((await db.execute(select(or_(sent_fee, assigned_plan)))).scalar_one())
