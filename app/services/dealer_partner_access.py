"""Dealer-partner standing and auto-intake scope.

Every route that exposes an Auto Dealer Agent's assigned files must apply the
same three gates: an active dealer-partner login, both signed agreements, and
an owned auto-industry intake. Keeping this outside any router prevents a
secondary interface from silently weakening the main AI-intake boundary.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User

DEALER_INTAKE_VARIANT = "dealer_gatekeeper_v1"


async def require_dealer_partner_standing(db: AsyncSession, user: User) -> None:
    """Require the individual and company agreements used by dealer intake."""

    if (
        user.role != Role.DEALER_PARTNER
        or getattr(user, "deleted_at", None) is not None
        or getattr(user, "account_status", "active") != "active"
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dealer partner role required")

    individual_signed = (
        await db.execute(
            select(ContractAgreement.id).where(
                ContractAgreement.contract_type == ContractType.PLATFORM_ACCESS,
                ContractAgreement.subject_type == ContractSubjectType.USER,
                ContractAgreement.subject_id == user.id,
                ContractAgreement.signed_at.is_not(None),
            )
        )
    ).first()
    if individual_signed is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You must sign the Platform Access Agreement before using the platform",
        )

    company_id = getattr(user, "referral_partner_company_id", None)
    company_signed = None
    if company_id is not None:
        company_signed = (
            await db.execute(
                select(ContractAgreement.id).where(
                    ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                    ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                    ContractAgreement.subject_id == company_id,
                    ContractAgreement.signed_at.is_not(None),
                )
            )
        ).first()
    if company_signed is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your company must have a signed Referral Protection Agreement on file before using the platform",
        )


def dealer_partner_intake_is_owned(user: User, intake: PublicUnderwritingIntake) -> bool:
    return (
        intake.broker_id == user.id
        and intake.variant == DEALER_INTAKE_VARIANT
    )
