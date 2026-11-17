"""Referral-partner standing and ownership across all intake programs.

Stored role keys remain compatible with existing accounts and signatures.
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
REFERRAL_PARTNER_ROLES = frozenset({Role.DEALER_PARTNER, Role.PROFESSIONAL_REFERRAL_PARTNER})
REFERRAL_INTAKE_ROLES = REFERRAL_PARTNER_ROLES | {Role.BROKER}


async def require_dealer_partner_standing(db: AsyncSession, user: User) -> None:
    """Require the individual and company agreements used by dealer intake."""

    if (
        user.role not in REFERRAL_INTAKE_ROLES
        or getattr(user, "deleted_at", None) is not None
        or getattr(user, "account_status", "active") != "active"
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Active referral partner role required")

    # Existing real estate originators retain their established onboarding.
    if user.role == Role.BROKER:
        return

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
    return intake.broker_id == user.id
