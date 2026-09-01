"""Users router — operator-team listing + invite/edit/revoke."""

# ruff: noqa: B008

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.referral_partner_company import ReferralPartnerCompany
from app.models.user import User
from app.services import clerk as clerk_service

router = APIRouter(prefix="/users", tags=["users"])

_ACCOUNT_ACCESS_TYPES = {"funding", "field_desk", "audit"}


def _account_types(user: User) -> list[str]:
    values = set(user.account_access_types or [])
    if user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        values.update(_ACCOUNT_ACCESS_TYPES)
    elif user.role in {Role.BROKER, Role.REGIONAL_MANAGER}:
        values.add("funding")
    elif user.role == Role.FIELD_REP:
        values.add("field_desk")
    return sorted(values)


class UserRead(BaseModel):
    id: UUID
    email: EmailStr | str
    name: str
    role: Role
    referral_partner_company_id: UUID | None = None
    referral_partner_company_name: str | None = None
    # Whether referral_partner_company_id's company has a signed Referral
    # Protection Agreement on file — the "does this broker's company always
    # have a contract in place" visibility the business owner asked for.
    # None when the user has no linked company (not a DEALER_PARTNER).
    company_agreement_signed: bool | None = None
    account_types: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    email: EmailStr
    name: str
    role: Role
    # Required for role=DEALER_PARTNER: their company must always have a
    # signed Referral Protection Agreement on file (see
    # app/routers/dealer_ai_intake.py's _require_dealer_partner and
    # app/routers/contracts.py). Find-or-create by name (case-insensitive) —
    # the same company invited more than once links to the same row rather
    # than creating duplicates.
    company_name: str | None = None
    referral_partner_company_id: UUID | None = None
    account_types: list[str] | None = None


class UserPatch(BaseModel):
    role: Role | None = None
    name: str | None = None
    # Required when setting role=DEALER_PARTNER on a user who has no
    # referral_partner_company_id yet (e.g. promoting an existing user via
    # the Team page's role dropdown, which -- unlike the invite flow --
    # previously had no way to collect a company at all, permanently
    # locking that user out of _require_dealer_partner's company-agreement
    # check). Find-or-create by name, same as invite_user.
    company_name: str | None = None
    referral_partner_company_id: UUID | None = None
    account_types: list[str] | None = None


class SignedCompanyRead(BaseModel):
    id: UUID
    name: str


async def _signed_company(
    db: AsyncSession, company_id: UUID
) -> ReferralPartnerCompany:
    company = await db.get(ReferralPartnerCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    signed = (
        await db.execute(
            select(ContractAgreement.id).where(
                ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                ContractAgreement.subject_id == company_id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if signed is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The selected company does not have a signed Referral Protection Agreement.",
        )
    return company


@router.get(
    "/referral-companies/signed",
    response_model=list[SignedCompanyRead],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def list_signed_referral_companies(
    db: AsyncSession = Depends(get_db),
) -> list[SignedCompanyRead]:
    rows = (
        await db.execute(
            select(ReferralPartnerCompany)
            .join(
                ContractAgreement,
                (ContractAgreement.subject_id == ReferralPartnerCompany.id)
                & (ContractAgreement.subject_type == ContractSubjectType.COMPANY)
                & (ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION),
            )
            .distinct()
            .order_by(ReferralPartnerCompany.name)
        )
    ).scalars().all()
    return [SignedCompanyRead(id=row.id, name=row.name) for row in rows]


@router.get(
    "",
    response_model=list[UserRead],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def list_users(db: AsyncSession = Depends(get_db)) -> list[UserRead]:
    """List every operator-team user. Super-admin only.

    Excludes CLIENT/LENDER/VENDOR users (Team is the operator team) and soft-deleted rows.
    """
    rows = (
        await db.execute(
            select(User)
            .where(User.role.notin_([Role.CLIENT, Role.LENDER, Role.VENDOR]), User.deleted_at.is_(None))
            .order_by(User.name)
        )
    ).scalars().all()

    company_ids = {r.referral_partner_company_id for r in rows if r.referral_partner_company_id is not None}
    companies: dict[UUID, ReferralPartnerCompany] = {}
    signed_company_ids: set[UUID] = set()
    if company_ids:
        company_rows = (
            await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.id.in_(company_ids)))
        ).scalars().all()
        companies = {c.id: c for c in company_rows}
        signed_company_ids = set(
            (
                await db.execute(
                    select(ContractAgreement.subject_id).where(
                        ContractAgreement.contract_type == ContractType.REFERRAL_PROTECTION,
                        ContractAgreement.subject_type == ContractSubjectType.COMPANY,
                        ContractAgreement.subject_id.in_(company_ids),
                    )
                )
            ).scalars().all()
        )

    results = []
    for r in rows:
        user_read = UserRead.model_validate(r)
        user_read.account_types = _account_types(r)
        if r.referral_partner_company_id is not None:
            company = companies.get(r.referral_partner_company_id)
            user_read.referral_partner_company_name = company.name if company else None
            user_read.company_agreement_signed = r.referral_partner_company_id in signed_company_ids
        results.append(user_read)
    return results


# Which product each role signs in to. Roles absent from this map fall back to
# Clerk's default (the desktop sign-up page), which is correct for the
# operator-console roles.
_INVITE_LANDING: dict[Role, str] = {
    Role.FIELD_REP: "https://rep.qualifiedcommercial.com/sign-in",
    Role.DEALER: "https://audit.qualifiedcommercial.com/sign-in",
}


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def invite_user(
    body: UserInvite,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Invite a new operator-team member.

    Creates a local User row immediately so the team list updates, then sends
    a Clerk invitation email (best-effort — invite still completes if Clerk
    isn't configured locally). Blocks role=CLIENT — borrowers are created via
    /clients.
    """
    if body.role == Role.CLIENT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "CLIENT role belongs to /clients — use that endpoint to create borrowers.",
        )
    if body.role == Role.VENDOR:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "VENDOR role belongs to bucket vendor access — use /buckets/admin/vendors.",
        )
    company_name = (body.company_name or "").strip()
    requested_access = set(body.account_types or [])
    if not requested_access.issubset(_ACCOUNT_ACCESS_TYPES):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown account access type")
    if body.role == Role.DEALER_PARTNER and not company_name and body.referral_partner_company_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Company name is required for Dealer Partner invites — their company must always have a "
            "signed Referral Protection Agreement on file.",
        )

    referral_partner_company_id = body.referral_partner_company_id
    if referral_partner_company_id is not None:
        await _signed_company(db, referral_partner_company_id)
    elif company_name:
        company = (
            await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.name.ilike(company_name)))
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "No company with that name and a signed agreement was found.",
            )
        await _signed_company(db, company.id)
        referral_partner_company_id = company.id

    existing = (
        await db.execute(select(User).where(User.email == body.email.lower()))
    ).scalar_one_or_none()
    if existing is not None and existing.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists.")
    if existing is not None and existing.deleted_at is not None:
        # Resurrect a soft-deleted row instead of failing on the unique index.
        existing.deleted_at = None
        existing.name = body.name
        existing.role = body.role
        existing.clerk_id = None  # force re-bind on next sign-in
        existing.referral_partner_company_id = referral_partner_company_id
        existing.account_access_types = sorted(requested_access)
        user = existing
    else:
        user = User(
            email=body.email.lower(),
            name=body.name,
            role=body.role,
            clerk_id=None,  # bound on first sign-in via JIT provision
            referral_partner_company_id=referral_partner_company_id,
            account_access_types=sorted(requested_access),
        )
        db.add(user)

    await db.flush()
    await db.refresh(user)

    # Fire-and-forget Clerk invitation. No-op when CLERK_SECRET_KEY is unset.
    # Land them on the app they actually work in. Without this the invite goes
    # to the desktop sign-up page, and a field rep or client signs in somewhere
    # their role has no access and gets bounced with no explanation.
    await clerk_service.invite_user(
        email=body.email,
        name=body.name,
        role=body.role,
        redirect_url=_INVITE_LANDING.get(body.role),
    )

    result = UserRead.model_validate(user)
    result.account_types = _account_types(user)
    if referral_partner_company_id is not None:
        company = await db.get(ReferralPartnerCompany, referral_partner_company_id)
        result.referral_partner_company_name = company.name if company else None
        result.company_agreement_signed = True
    return result


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_user(
    user_id: UUID,
    body: UserPatch,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.role is not None:
        if body.role == Role.CLIENT:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Use /clients to convert a user to a borrower.",
            )
        # DEALER_PARTNER is hard-blocked by _require_dealer_partner
        # (dealer_ai_intake.py) until a linked ReferralPartnerCompany has a
        # signed Referral Protection Agreement -- a user with no company
        # link at all can never pass that check. Require one here, same as
        # invite_user, rather than silently leaving the role unusable.
        if body.role == Role.DEALER_PARTNER and user.referral_partner_company_id is None:
            if body.referral_partner_company_id is not None:
                await _signed_company(db, body.referral_partner_company_id)
                user.referral_partner_company_id = body.referral_partner_company_id
                user.role = body.role
            else:
                company_name = (body.company_name or "").strip()
                if not company_name:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "Company name is required to set the Dealer Partner role — their company must have a "
                        "signed Referral Protection Agreement on file.",
                    )
                company = (
                    await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.name.ilike(company_name)))
                ).scalar_one_or_none()
                if company is None:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        "No company with that name and a signed agreement was found.",
                    )
                await _signed_company(db, company.id)
                user.referral_partner_company_id = company.id
        user.role = body.role
    if body.name is not None:
        user.name = body.name
    if "referral_partner_company_id" in body.model_fields_set:
        if body.referral_partner_company_id is None and (body.role or user.role) == Role.DEALER_PARTNER:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Dealer Partner access requires a company with a signed agreement.",
            )
        if body.referral_partner_company_id is not None:
            await _signed_company(db, body.referral_partner_company_id)
        user.referral_partner_company_id = body.referral_partner_company_id
    if body.account_types is not None:
        requested_access = set(body.account_types)
        if not requested_access.issubset(_ACCOUNT_ACCESS_TYPES):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown account access type")
        user.account_access_types = sorted(requested_access)
    await db.flush()
    await db.refresh(user)
    result = UserRead.model_validate(user)
    result.account_types = _account_types(user)
    if user.referral_partner_company_id is not None:
        company = await db.get(ReferralPartnerCompany, user.referral_partner_company_id)
        result.referral_partner_company_name = company.name if company else None
        result.company_agreement_signed = True
    return result


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current: User = Depends(require_role(Role.SUPER_ADMIN)),
) -> None:
    """Soft-delete a team member. Self-delete is blocked."""
    if current.id == user_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You can't remove your own super-admin account."
        )
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.deleted_at = datetime.now(UTC)
    await db.flush()
    # Best-effort revoke in Clerk so the invited user can't sign in afterward.
    if user.clerk_id:
        await clerk_service.revoke_user(user.clerk_id)
