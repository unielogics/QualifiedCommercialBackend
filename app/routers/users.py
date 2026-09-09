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
from app.deps import CurrentUser, require_role
from app.enums import ContractSubjectType, ContractType, Role
from app.models.contract_agreement import ContractAgreement
from app.models.referral_partner_company import (
    KIND_HOUSE,
    KIND_REFERRAL_PARTNER,
    ReferralPartnerCompany,
)
from app.models.user import User
from app.services import clerk as clerk_service

# OPERATOR_ROLES has one definition already; a second copy here is how
# permission sets drift apart.
from app.services.production_packages import OPERATOR_ROLES, house_company

router = APIRouter(prefix="/users", tags=["users"])

# Every employee is linked to a business relationship profile. These are the
# roles that are the house's own people: with no company on the invite they
# are linked to the house row, and their link can be changed but never cleared.
HOUSE_ROLES: frozenset[Role] = frozenset({Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP})

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
    # "referral_partner" or "house"; None when there is no link.
    company_kind: str | None = None
    # Whether referral_partner_company_id's company has a signed Referral
    # Protection Agreement on file — the "does this broker's company always
    # have a contract in place" visibility the business owner asked for.
    # None when the user has no linked company; False for the house, which
    # never signs one.
    company_agreement_signed: bool | None = None
    account_types: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    email: EmailStr
    name: str
    role: Role
    # The business relationship profile. Required for role=DEALER_PARTNER —
    # their company must sign the Referral Protection Agreement before they
    # have standing (see app/routers/dealer_ai_intake.py's
    # _require_dealer_partner), but the link itself may precede the signature.
    # Find-or-create by name (case-insensitive) — the same company invited
    # more than once links to the same row rather than creating duplicates.
    # A house role with neither is linked to the house.
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


class ReferralCompanyRead(BaseModel):
    id: UUID
    name: str
    kind: str
    signed: bool


async def _company_for_link(db: AsyncSession, company_id: UUID) -> ReferralPartnerCompany:
    """Linking may precede the signature. Standing (a partner's access) still
    means signed — that gate is _require_dealer_partner and is untouched."""
    company = await db.get(ReferralPartnerCompany, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


async def _company_by_name(db: AsyncSession, company_name: str) -> ReferralPartnerCompany:
    """Find-or-create by name, the way the docstrings always said it worked."""
    company = (
        await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.name.ilike(company_name)))
    ).scalar_one_or_none()
    if company is None:
        company = ReferralPartnerCompany(name=company_name, kind=KIND_REFERRAL_PARTNER)
        db.add(company)
        await db.flush()
    return company


async def _signed_company_ids(db: AsyncSession, company_ids: set[UUID]) -> set[UUID]:
    if not company_ids:
        return set()
    return set(
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


async def _with_company(db: AsyncSession, user: User, result: UserRead) -> UserRead:
    """The linked profile on a user read: name, kind, and whether it really signed."""
    result.account_types = _account_types(user)
    if user.referral_partner_company_id is not None:
        company = await db.get(ReferralPartnerCompany, user.referral_partner_company_id)
        result.referral_partner_company_name = company.name if company else None
        result.company_kind = getattr(company, "kind", None) if company else None
        result.company_agreement_signed = user.referral_partner_company_id in await _signed_company_ids(db, {user.referral_partner_company_id})
    return result


def _is_house(company: ReferralPartnerCompany | None) -> bool:
    return company is not None and getattr(company, "kind", None) == KIND_HOUSE


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
    "/referral-companies",
    response_model=list[ReferralCompanyRead],
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def list_referral_companies(db: AsyncSession = Depends(get_db)) -> list[ReferralCompanyRead]:
    """Every business relationship profile, the house first, each saying whether it signed."""
    rows = (await db.execute(select(ReferralPartnerCompany).order_by(ReferralPartnerCompany.name))).scalars().all()
    signed = await _signed_company_ids(db, {r.id for r in rows})
    rows = sorted(rows, key=lambda r: (0 if _is_house(r) else 1, r.name.lower()))
    return [ReferralCompanyRead(id=r.id, name=r.name, kind=getattr(r, "kind", None) or KIND_REFERRAL_PARTNER, signed=r.id in signed) for r in rows]


class TeamMemberRead(BaseModel):
    """Just enough of a colleague to name them on a document.

    `GET /users` is super-admin only, and rightly so — it carries invite state,
    account status and referral-company wiring. But the Production Package's
    relationship-manager picker renders for every operator and used that route,
    swallowing the 403, so an underwriter or a rep silently got an empty list
    and `rm_user_id` was never set. This is the list they actually need.
    """

    id: UUID
    name: str
    email: str
    phone: str | None = None
    title: str | None = None
    role: str
    # The linked business relationship profile: the package's employer line
    # and the sponsor default follow it.
    company_id: UUID | None = None
    company_name: str | None = None
    company_kind: str | None = None
    company_signed: bool = False


@router.get("/team", response_model=list[TeamMemberRead])
async def list_team(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[TeamMemberRead]:
    if user.role not in OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required")
    rows = (
        await db.execute(
            select(User)
            .where(
                # Field reps are selectable as a relationship manager today;
                # this list must not quietly narrow that.
                User.role.in_([Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP]),
                User.deleted_at.is_(None),
                User.account_status == "active",
            )
            .order_by(User.name)
        )
    ).scalars().all()
    company_ids = {getattr(r, "referral_partner_company_id", None) for r in rows} - {None}
    companies: dict[UUID, ReferralPartnerCompany] = {}
    if company_ids:
        companies = {c.id: c for c in (await db.execute(select(ReferralPartnerCompany).where(ReferralPartnerCompany.id.in_(company_ids)))).scalars().all()}
    signed = await _signed_company_ids(db, company_ids)
    out: list[TeamMemberRead] = []
    for r in rows:
        company = companies.get(getattr(r, "referral_partner_company_id", None))
        out.append(TeamMemberRead(
            id=r.id, name=r.name, email=r.email, phone=r.phone, title=r.title, role=str(r.role),
            company_id=company.id if company else None, company_name=company.name if company else None,
            company_kind=(getattr(company, "kind", None) or KIND_REFERRAL_PARTNER) if company else None,
            company_signed=bool(company and company.id in signed),
        ))
    return out


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
            user_read.company_kind = getattr(company, "kind", None) if company else None
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
            "Company name is required for Dealer Partner invites — their company must sign the "
            "Referral Protection Agreement before they can use the platform.",
        )

    referral_partner_company_id = body.referral_partner_company_id
    if referral_partner_company_id is not None:
        company = await _company_for_link(db, referral_partner_company_id)
        if body.role == Role.DEALER_PARTNER and _is_house(company):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A dealer partner belongs to their own company, not the house.")
    elif company_name:
        referral_partner_company_id = (await _company_by_name(db, company_name)).id
    elif body.role in HOUSE_ROLES:
        # Every employee is linked. Skip silently if the house row is absent
        # (a fresh database before 0198 ran) rather than fail the invite.
        house = await house_company(db)
        referral_partner_company_id = house.id if house else None

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

    return await _with_company(db, user, UserRead.model_validate(user))


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
        # invite_user, rather than silently leaving the role unusable. A
        # house-linked staffer counts as having no partner company: promoting
        # them without one would lock them out for good.
        linked = await db.get(ReferralPartnerCompany, user.referral_partner_company_id) if user.referral_partner_company_id else None
        if body.role == Role.DEALER_PARTNER and (linked is None or _is_house(linked)):
            if body.referral_partner_company_id is not None:
                company = await _company_for_link(db, body.referral_partner_company_id)
                if _is_house(company):
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "A dealer partner belongs to their own company, not the house.")
                user.referral_partner_company_id = company.id
            else:
                company_name = (body.company_name or "").strip()
                if not company_name:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "Company name is required to set the Dealer Partner role — their company must sign the "
                        "Referral Protection Agreement before they can use the platform.",
                    )
                user.referral_partner_company_id = (await _company_by_name(db, company_name)).id
        elif body.role in HOUSE_ROLES and user.role == Role.DEALER_PARTNER and "referral_partner_company_id" not in body.model_fields_set:
            # Coming in from a partner company with no new link named: the
            # house, or their old company would keep defaulting the sponsor.
            house = await house_company(db)
            if house is not None:
                user.referral_partner_company_id = house.id
        user.role = body.role
    if body.name is not None:
        user.name = body.name
    if "referral_partner_company_id" in body.model_fields_set:
        role_after = body.role or user.role
        if body.referral_partner_company_id is None and role_after in HOUSE_ROLES | {Role.DEALER_PARTNER}:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Every operator is linked to a business relationship profile — pick the house or a partner company.",
            )
        if body.referral_partner_company_id is not None:
            company = await _company_for_link(db, body.referral_partner_company_id)
            if role_after == Role.DEALER_PARTNER and _is_house(company):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "A dealer partner belongs to their own company, not the house.")
        user.referral_partner_company_id = body.referral_partner_company_id
    if body.account_types is not None:
        requested_access = set(body.account_types)
        if not requested_access.issubset(_ACCOUNT_ACCESS_TYPES):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown account access type")
        user.account_access_types = sorted(requested_access)
    await db.flush()
    await db.refresh(user)
    return await _with_company(db, user, UserRead.model_validate(user))


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
