"""Super-admin directory for client identities and product access.

The directory projects existing Client and public-intake records. It never
merges records by a fuzzy identity match; creating/linking a login is an
explicit, audited administrator action.
"""

from __future__ import annotations

# FastAPI dependency declarations intentionally use Depends in defaults.
# ruff: noqa: B008
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.dealer_os.models import DealerBusiness
from app.deps import CurrentUser
from app.enums import ProductAccountType, Role
from app.models.application_profile import ApplicationProfile
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.models.user_access import UserAccessEvent
from app.services import clerk as clerk_service
from app.services.application_profiles import resolve_profile
from app.services.user_access import (
    access_state,
    assigned_product_values,
    record_access_event,
    request_metadata,
    set_product_access,
    synchronize_external_compatibility_role,
)

router = APIRouter(prefix="/admin/client-access", tags=["client-access"])
_EXTERNAL_ROLES = {Role.CLIENT, Role.DEALER}


class ProductEntitlement(BaseModel):
    product: ProductAccountType
    enabled: bool
    granted_at: datetime | None = None
    revoked_at: datetime | None = None
    reason: str | None = None


class AuditBusinessScope(BaseModel):
    profile_id: UUID
    dealer_id: UUID | None = None
    business_name: str
    vertical: str
    source_kind: str
    source_id: UUID | None = None
    bucket_id: UUID | None = None
    enabled_for_user: bool = False


class ClientAccessDirectoryRow(BaseModel):
    subject_kind: Literal["client", "intake", "user"]
    subject_id: UUID
    client_id: UUID | None = None
    user_id: UUID | None = None
    client_name: str
    businesses: list[str] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    origin: str
    login_state: Literal["no_login", "invited", "active", "suspended", "invite_failed"]
    account_types: list[ProductAccountType] = Field(default_factory=list)
    account_status: str | None = None
    file_count: int = 0
    last_active_at: datetime | None = None
    status: str


class ClientAccessDirectoryResponse(BaseModel):
    items: list[ClientAccessDirectoryRow]
    total: int
    page: int
    page_size: int
    sources: list[str] = Field(default_factory=list)


class AccessHistoryItem(BaseModel):
    id: UUID
    action: str
    reason: str | None = None
    actor_user_id: UUID | None = None
    before_state: dict | None = None
    after_state: dict | None = None
    request_metadata: dict | None = None
    created_at: datetime


class ClientAccessDetail(BaseModel):
    subject: ClientAccessDirectoryRow
    entitlements: list[ProductEntitlement]
    audit_scopes: list[AuditBusinessScope]
    invitation_status: str | None = None
    invitation_error: str | None = None
    access_history: list[AccessHistoryItem]


class ClientAccessInviteRequest(BaseModel):
    subject_kind: Literal["client", "intake"]
    subject_id: UUID
    email: str
    name: str | None = None
    account_types: list[ProductAccountType]
    audit_profile_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("A valid email is required")
        return normalized

    @field_validator("account_types")
    @classmethod
    def require_product(cls, value: list[ProductAccountType]) -> list[ProductAccountType]:
        unique = list(dict.fromkeys(value))
        if not unique:
            raise ValueError("Select Funding, Audit, or both")
        return unique


class ClientAccessUpdateRequest(BaseModel):
    account_types: list[ProductAccountType]
    account_status: Literal["active", "suspended"] = "active"
    audit_profile_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("account_types")
    @classmethod
    def normalize_products(cls, value: list[ProductAccountType]) -> list[ProductAccountType]:
        return list(dict.fromkeys(value))


class AccessMutationResult(BaseModel):
    user_id: UUID
    account_types: list[ProductAccountType]
    account_status: str
    login_state: str
    invitation_sent: bool = False
    clerk_synced: bool = False
    sessions_revoked: bool = False
    audit_scope_ids: list[UUID] = Field(default_factory=list)


class AccessReasonRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _require_super_admin(user: User) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin role required")


def _login_state(user: User | None) -> str:
    if user is None:
        return "no_login"
    if user.account_status == "suspended":
        return "suspended"
    if user.clerk_id:
        return "active"
    if user.last_invite_status == "failed":
        return "invite_failed"
    return "invited"


def _source_kind(profile: ApplicationProfile) -> tuple[str, UUID | None]:
    for kind in ("intake", "loan", "deal", "dealer"):
        source_id = getattr(profile, f"{kind}_id")
        if source_id is not None:
            return kind, source_id
    return "profile", profile.id


async def _profiles_for_subjects(
    db: AsyncSession, client_ids: set[UUID], intake_ids: set[UUID]
) -> list[ApplicationProfile]:
    conditions = []
    if client_ids:
        conditions.append(ApplicationProfile.client_id.in_(client_ids))
    if intake_ids:
        conditions.append(ApplicationProfile.intake_id.in_(intake_ids))
    if not conditions:
        return []
    return (await db.execute(select(ApplicationProfile).where(or_(*conditions)))).scalars().all()


async def _subject_rows(db: AsyncSession, q: str | None) -> list[ClientAccessDirectoryRow]:
    normalized = (q or "").strip().lower()
    clients = (await db.execute(select(Client).order_by(Client.updated_at.desc()))).scalars().all()
    all_intakes = (
        await db.execute(
            select(PublicUnderwritingIntake).order_by(
                PublicUnderwritingIntake.updated_at.desc()
            )
        )
    ).scalars().all()
    orphan_intakes = [row for row in all_intakes if row.client_id is None]
    intakes_by_client: dict[UUID, list[PublicUnderwritingIntake]] = defaultdict(list)
    for intake in all_intakes:
        if intake.client_id:
            intakes_by_client[intake.client_id].append(intake)
    external_users = (
        await db.execute(
            select(User)
            .options(selectinload(User.product_accesses))
            .where(User.role.in_(tuple(_EXTERNAL_ROLES)), User.deleted_at.is_(None))
        )
    ).scalars().all()
    users = {row.id: row for row in external_users}
    represented_user_ids = {row.user_id for row in clients if row.user_id}
    standalone_users = [row for row in external_users if row.id not in represented_user_ids]
    standalone_user_ids = {row.id for row in standalone_users}
    standalone_dealers = (
        (
            await db.execute(
                select(DealerBusiness).where(
                    DealerBusiness.dealer_user_id.in_(standalone_user_ids)
                )
            )
        ).scalars().all()
        if standalone_user_ids
        else []
    )
    profiles = await _profiles_for_subjects(
        db, {row.id for row in clients}, {row.id for row in all_intakes}
    )
    standalone_dealer_ids = {row.id for row in standalone_dealers}
    if standalone_dealer_ids:
        standalone_profiles = (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.dealer_id.in_(standalone_dealer_ids)
                )
            )
        ).scalars().all()
        profiles = list({profile.id: profile for profile in [*profiles, *standalone_profiles]}.values())
    profiles_by_client: dict[UUID, list[ApplicationProfile]] = defaultdict(list)
    profiles_by_intake: dict[UUID, list[ApplicationProfile]] = defaultdict(list)
    profiles_by_dealer: dict[UUID, list[ApplicationProfile]] = defaultdict(list)
    for profile in profiles:
        if profile.client_id:
            profiles_by_client[profile.client_id].append(profile)
        if profile.intake_id:
            profiles_by_intake[profile.intake_id].append(profile)
        if profile.dealer_id:
            profiles_by_dealer[profile.dealer_id].append(profile)

    dealer_ids = {
        *{profile.dealer_id for profile in profiles if profile.dealer_id},
        *standalone_dealer_ids,
    }
    dealers = {
        row.id: row
        for row in (
            await db.execute(select(DealerBusiness).where(DealerBusiness.id.in_(dealer_ids)))
            if dealer_ids
            else []
        ).scalars().all()
    } if dealer_ids else {}
    linked_intakes = {row.id: row for row in all_intakes}

    def attribution_source(value: object) -> str | None:
        if not isinstance(value, dict):
            return None
        attribution = value.get("signup_attribution")
        if not isinstance(attribution, dict):
            return None
        source = attribution.get("source")
        return str(source).strip() if source else None

    def profile_businesses(rows: list[ApplicationProfile]) -> list[str]:
        values: list[str] = []
        for profile in rows:
            dealer = dealers.get(profile.dealer_id) if profile.dealer_id else None
            intake = linked_intakes.get(profile.intake_id) if profile.intake_id else None
            label = (
                (dealer.legal_name or dealer.name if dealer else None)
                or (intake.business_name if intake else None)
            )
            if label and label not in values:
                values.append(label)
        return values

    result: list[ClientAccessDirectoryRow] = []
    for client in clients:
        user = users.get(client.user_id) if client.user_id else None
        subject_profiles = profiles_by_client.get(client.id, [])
        subject_intakes = intakes_by_client.get(client.id, [])
        businesses = profile_businesses(subject_profiles)
        for intake in subject_intakes:
            if intake.business_name and intake.business_name not in businesses:
                businesses.append(intake.business_name)
        profiled_intake_ids = {
            profile.intake_id for profile in subject_profiles if profile.intake_id
        }
        unprofiled_intakes = [
            intake for intake in subject_intakes if intake.id not in profiled_intake_ids
        ]
        searchable = " ".join(
            [
                str(client.id), client.name or "", client.email or "", client.phone or "",
                client.source_channel or "", *(businesses or []),
                *(str(p.id) for p in subject_profiles),
                *(str(p.intake_id or "") for p in subject_profiles),
                *(str(p.primary_bucket_id or "") for p in subject_profiles),
                *(
                    " ".join(
                        [
                            str(intake.id),
                            str(intake.bucket_id),
                            intake.full_name or "",
                            intake.business_name or "",
                            intake.email or "",
                            intake.phone or "",
                            intake.variant or "",
                            intake.referral_source or "",
                        ]
                    )
                    for intake in subject_intakes
                ),
            ]
        ).lower()
        if normalized and normalized not in searchable:
            continue
        products = sorted(assigned_product_values(user)) if user else []
        activity_candidates = [client.updated_at]
        activity_candidates.extend(
            (intake.last_message_at or intake.updated_at) for intake in subject_intakes
        )
        if user and user.last_seen_at:
            activity_candidates.append(user.last_seen_at)
        latest_intake_source = next(
            (
                attribution_source(intake.intake_state)
                or intake.referral_source
                or intake.variant
                for intake in subject_intakes
                if attribution_source(intake.intake_state)
                or intake.referral_source
                or intake.variant
            ),
            None,
        )
        result.append(
            ClientAccessDirectoryRow(
                subject_kind="client",
                subject_id=client.id,
                client_id=client.id,
                user_id=user.id if user else None,
                client_name=client.name,
                businesses=businesses,
                email=user.email if user else client.email,
                phone=client.phone,
                origin=(
                    attribution_source(client.lead_intake)
                    or latest_intake_source
                    or client.source_channel
                    or client.lead_source
                    or "client_record"
                ),
                login_state=_login_state(user),
                account_types=[ProductAccountType(value) for value in products],
                account_status=user.account_status if user else None,
                file_count=len(subject_profiles) + len(unprofiled_intakes),
                last_active_at=max(activity_candidates),
                status=user.account_status if user else "no_login",
            )
        )
    for intake in orphan_intakes:
        subject_profiles = profiles_by_intake.get(intake.id, [])
        businesses = profile_businesses(subject_profiles) or ([intake.business_name] if intake.business_name else [])
        searchable = " ".join(
            [
                str(intake.id), intake.full_name, intake.business_name or "", intake.email,
                intake.phone or "", intake.variant or "", intake.referral_source or "",
                *(str(p.id) for p in subject_profiles), str(intake.bucket_id),
            ]
        ).lower()
        if normalized and normalized not in searchable:
            continue
        result.append(
            ClientAccessDirectoryRow(
                subject_kind="intake",
                subject_id=intake.id,
                client_name=intake.full_name,
                businesses=businesses,
                email=intake.email,
                phone=intake.phone,
                origin=intake.referral_source or intake.variant or "public_intake",
                login_state="no_login",
                file_count=max(1, len(subject_profiles)),
                last_active_at=intake.last_message_at or intake.updated_at,
                status=intake.status,
            )
        )
    standalone_dealers_by_user: dict[UUID, list[DealerBusiness]] = defaultdict(list)
    for dealer in standalone_dealers:
        if dealer.dealer_user_id:
            standalone_dealers_by_user[dealer.dealer_user_id].append(dealer)
    for external_user in standalone_users:
        assigned_dealers = standalone_dealers_by_user.get(external_user.id, [])
        subject_profiles = [
            profile
            for dealer in assigned_dealers
            for profile in profiles_by_dealer.get(dealer.id, [])
        ]
        businesses = list(
            dict.fromkeys(
                (dealer.legal_name or dealer.name)
                for dealer in assigned_dealers
                if dealer.legal_name or dealer.name
            )
        )
        searchable = " ".join(
            [
                str(external_user.id),
                external_user.name or "",
                external_user.email or "",
                *(businesses or []),
                *(dealer.email or "" for dealer in assigned_dealers),
                *(dealer.phone or "" for dealer in assigned_dealers),
                *(dealer.case_ref or "" for dealer in assigned_dealers),
                *(str(profile.id) for profile in subject_profiles),
            ]
        ).lower()
        if normalized and normalized not in searchable:
            continue
        products = sorted(assigned_product_values(external_user))
        result.append(
            ClientAccessDirectoryRow(
                subject_kind="user",
                subject_id=external_user.id,
                user_id=external_user.id,
                client_name=external_user.name or external_user.email,
                businesses=businesses,
                email=external_user.email,
                phone=next((dealer.phone for dealer in assigned_dealers if dealer.phone), None),
                origin="audit_client",
                login_state=_login_state(external_user),
                account_types=[ProductAccountType(value) for value in products],
                account_status=external_user.account_status,
                file_count=len(subject_profiles),
                last_active_at=external_user.last_seen_at or external_user.updated_at,
                status=external_user.account_status,
            )
        )
    result.sort(key=lambda row: row.last_active_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return result


def _matches_filters(
    row: ClientAccessDirectoryRow,
    *,
    source: str | None,
    login_state: str | None,
    account_type: str | None,
) -> bool:
    if source and source != "all" and row.origin != source:
        return False
    if login_state and login_state != "all" and row.login_state != login_state:
        return False
    values = {item.value for item in row.account_types}
    if account_type == "funding" and values != {"funding"}:
        return False
    if account_type == "audit" and values != {"audit"}:
        return False
    if account_type == "both" and values != {"funding", "audit"}:
        return False
    if account_type == "none" and values:
        return False
    return True


@router.get("", response_model=ClientAccessDirectoryResponse)
async def list_client_access(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(default=None, max_length=200),
    source: str | None = None,
    login_state: str | None = None,
    account_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=10, le=200),
) -> ClientAccessDirectoryResponse:
    _require_super_admin(user)
    all_rows = await _subject_rows(db, q)
    rows = [
        row
        for row in all_rows
        if _matches_filters(
            row, source=source, login_state=login_state, account_type=account_type
        )
    ]
    start = (page - 1) * page_size
    return ClientAccessDirectoryResponse(
        items=rows[start : start + page_size],
        total=len(rows),
        page=page,
        page_size=page_size,
        sources=sorted({row.origin for row in all_rows}),
    )


async def _resolve_subject(
    db: AsyncSession, subject_kind: str, subject_id: UUID
) -> tuple[Client | None, PublicUnderwritingIntake | None]:
    if subject_kind == "client":
        client = await db.get(Client, subject_id)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client access subject not found")
        return client, None
    if subject_kind == "intake":
        intake = await db.get(PublicUnderwritingIntake, subject_id)
        if intake is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client access subject not found")
        client = await db.get(Client, intake.client_id) if intake.client_id else None
        return client, intake
    if subject_kind == "user":
        target = (
            await db.execute(
                select(User)
                .options(selectinload(User.client))
                .where(
                    User.id == subject_id,
                    User.role.in_(tuple(_EXTERNAL_ROLES)),
                    User.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client access subject not found")
        return target.client, None
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Client access subject not found")


async def _ensure_subject_client(
    db: AsyncSession,
    *,
    client: Client | None,
    intake: PublicUnderwritingIntake | None,
) -> Client:
    if client is not None:
        return client
    if intake is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A Funding client record is required")
    client = Client(
        name=intake.full_name,
        email=intake.email.strip().lower(),
        phone=intake.phone,
        source_channel="public_intake",
        referral_source=intake.referral_source,
        client_experience_mode="self_directed",
        client_experience_mode_reason="public_intake_access_enabled",
        client_experience_mode_locked_by="firm",
    )
    db.add(client)
    await db.flush()
    intake.client_id = client.id
    await db.execute(
        ApplicationProfile.__table__.update()
        .where(ApplicationProfile.intake_id == intake.id)
        .values(client_id=client.id)
    )
    return client


async def _profile_scope_options(
    db: AsyncSession,
    *,
    client: Client | None,
    intake: PublicUnderwritingIntake | None,
    user_id: UUID | None,
) -> list[AuditBusinessScope]:
    conditions = []
    if client:
        conditions.append(ApplicationProfile.client_id == client.id)
    if intake:
        conditions.append(ApplicationProfile.intake_id == intake.id)
    if user_id:
        assigned_dealer_ids = select(DealerBusiness.id).where(
            DealerBusiness.dealer_user_id == user_id
        )
        conditions.append(ApplicationProfile.dealer_id.in_(assigned_dealer_ids))
    profiles = (
        (await db.execute(select(ApplicationProfile).where(or_(*conditions)))).scalars().all()
        if conditions
        else []
    )
    dealer_ids = {profile.dealer_id for profile in profiles if profile.dealer_id}
    dealers = {
        row.id: row
        for row in (
            await db.execute(select(DealerBusiness).where(DealerBusiness.id.in_(dealer_ids)))
        ).scalars().all()
    } if dealer_ids else {}
    intake_ids = {profile.intake_id for profile in profiles if profile.intake_id}
    intakes = {
        row.id: row
        for row in (
            await db.execute(
                select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.id.in_(intake_ids))
            )
        ).scalars().all()
    } if intake_ids else {}
    result = []
    for profile in profiles:
        dealer = dealers.get(profile.dealer_id) if profile.dealer_id else None
        source_intake = intakes.get(profile.intake_id) if profile.intake_id else intake
        label = (
            (dealer.legal_name or dealer.name if dealer else None)
            or (source_intake.business_name if source_intake else None)
            or (client.name if client else None)
            or "Application file"
        )
        source_kind, source_id = _source_kind(profile)
        result.append(
            AuditBusinessScope(
                profile_id=profile.id,
                dealer_id=profile.dealer_id,
                business_name=label,
                vertical=profile.vertical,
                source_kind=source_kind,
                source_id=source_id,
                bucket_id=profile.primary_bucket_id,
                enabled_for_user=bool(dealer and user_id and dealer.dealer_user_id == user_id),
            )
        )
    return result


async def _detail(
    db: AsyncSession, subject_kind: str, subject_id: UUID
) -> ClientAccessDetail:
    rows = await _subject_rows(db, str(subject_id))
    subject = next(
        (row for row in rows if row.subject_kind == subject_kind and row.subject_id == subject_id),
        None,
    )
    if subject is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client access subject not found")
    client, intake = await _resolve_subject(db, subject_kind, subject_id)
    user = None
    if subject.user_id:
        user = (
            await db.execute(
                select(User)
                .options(selectinload(User.product_accesses))
                .where(User.id == subject.user_id)
            )
        ).scalar_one_or_none()
    entitlements = [
        ProductEntitlement(
            product=ProductAccountType(row.product),
            enabled=row.enabled,
            granted_at=row.granted_at,
            revoked_at=row.revoked_at,
            reason=row.reason,
        )
        for row in (user.product_accesses if user else [])
    ]
    events = (
        (
            await db.execute(
                select(UserAccessEvent)
                .where(UserAccessEvent.user_id == user.id)
                .order_by(UserAccessEvent.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        if user
        else []
    )
    return ClientAccessDetail(
        subject=subject,
        entitlements=entitlements,
        audit_scopes=await _profile_scope_options(
            db, client=client, intake=intake, user_id=user.id if user else None
        ),
        invitation_status=user.last_invite_status if user else None,
        invitation_error=user.last_invite_error if user else None,
        access_history=[AccessHistoryItem.model_validate(row, from_attributes=True) for row in events],
    )


@router.get(
    "/subjects/{subject_kind}/{subject_id}", response_model=ClientAccessDetail
)
async def get_client_access_detail(
    subject_kind: str,
    subject_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClientAccessDetail:
    _require_super_admin(user)
    return await _detail(db, subject_kind, subject_id)


async def _load_or_create_external_user(
    db: AsyncSession,
    *,
    client: Client,
    email: str,
    name: str,
    products: set[str],
) -> User:
    existing = (
        await db.execute(
            select(User)
            .options(selectinload(User.product_accesses), selectinload(User.client))
            .where(func.lower(User.email) == email)
        )
    ).scalar_one_or_none()
    if existing and existing.role not in _EXTERNAL_ROLES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"That email belongs to a {existing.role.value} account and cannot be reused.",
        )
    if existing and existing.client is not None and existing.client.id != client.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That login is already linked to another Funding client record.",
        )
    if existing:
        existing.deleted_at = None
        existing.account_status = "active"
        existing.name = name or existing.name
        target = existing
    else:
        target = User(
            email=email,
            name=name,
            role=Role.CLIENT if "funding" in products else Role.DEALER,
            account_status="active",
        )
        db.add(target)
        await db.flush()
    if client.user_id and client.user_id != target.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This client already has another login.")
    client.user_id = target.id
    await db.flush()
    return target


async def _ensure_profiles_exist(
    db: AsyncSession,
    *,
    client: Client | None,
    intake: PublicUnderwritingIntake | None,
    actor: User,
) -> None:
    conditions = []
    if client:
        conditions.append(ApplicationProfile.client_id == client.id)
    if intake:
        conditions.append(ApplicationProfile.intake_id == intake.id)
    exists_row = (
        await db.execute(select(ApplicationProfile.id).where(or_(*conditions)).limit(1))
    ).scalar_one_or_none() if conditions else None
    if exists_row is None and intake is not None:
        await resolve_profile(db, "intake", intake.id, actor)


async def _assign_audit_scopes(
    db: AsyncSession,
    *,
    target: User,
    client: Client | None,
    intake: PublicUnderwritingIntake | None,
    profile_ids: list[UUID],
    actor: User,
) -> list[UUID]:
    if not profile_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Select at least one business file when enabling Audit.",
        )
    await _ensure_profiles_exist(db, client=client, intake=intake, actor=actor)
    conditions = []
    if client:
        conditions.append(ApplicationProfile.client_id == client.id)
    if intake:
        conditions.append(ApplicationProfile.intake_id == intake.id)
    assigned_dealer_ids = select(DealerBusiness.id).where(
        DealerBusiness.dealer_user_id == target.id
    )
    conditions.append(ApplicationProfile.dealer_id.in_(assigned_dealer_ids))
    profiles = (
        await db.execute(
            select(ApplicationProfile).where(
                ApplicationProfile.id.in_(profile_ids), or_(*conditions)
            )
        )
    ).scalars().all()
    if len({profile.id for profile in profiles}) != len(set(profile_ids)):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "One or more selected business files are not part of this client subject.",
        )
    intake_ids = {profile.intake_id for profile in profiles if profile.intake_id}
    intakes = {
        row.id: row
        for row in (
            await db.execute(
                select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.id.in_(intake_ids))
            )
        ).scalars().all()
    } if intake_ids else {}
    previously_assigned = (
        await db.execute(
            select(DealerBusiness).where(DealerBusiness.dealer_user_id == target.id)
        )
    ).scalars().all()
    selected_dealer_ids: set[UUID] = set()
    for profile in profiles:
        source_intake = intakes.get(profile.intake_id) if profile.intake_id else intake
        dealer = await db.get(DealerBusiness, profile.dealer_id) if profile.dealer_id else None
        if dealer is not None and dealer.dealer_user_id not in (None, target.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A selected Audit business is already assigned to another login.",
            )
        if dealer is None:
            label = (
                (source_intake.business_name if source_intake else None)
                or (client.name if client else None)
                or target.name
            )
            dealer = DealerBusiness(
                name=label,
                legal_name=label,
                email=(source_intake.email if source_intake else client.email if client else target.email),
                phone=(source_intake.phone if source_intake else client.phone if client else None),
                bucket_id=profile.primary_bucket_id,
                handoff_intake_id=profile.intake_id,
                owner_user_id=actor.id,
                dealer_user_id=target.id,
                audit_client_since=datetime.now(UTC),
                funding_purpose=profile.funding_category,
                industry=profile.industry or "other",
                entity_type=profile.entity_type,
                naics_code=profile.naics_code,
                naics_label=profile.naics_label,
            )
            db.add(dealer)
            await db.flush()
            profile.dealer_id = dealer.id
        else:
            dealer.dealer_user_id = target.id
            dealer.audit_client_since = dealer.audit_client_since or datetime.now(UTC)
            if dealer.bucket_id is None:
                dealer.bucket_id = profile.primary_bucket_id
        if client is not None and profile.client_id is None:
            profile.client_id = client.id
        selected_dealer_ids.add(dealer.id)
    for dealer in previously_assigned:
        if dealer.id not in selected_dealer_ids:
            dealer.dealer_user_id = None
    await db.flush()
    return [profile.id for profile in profiles]


async def _clear_audit_scopes(db: AsyncSession, *, target: User) -> None:
    dealers = (
        await db.execute(
            select(DealerBusiness).where(DealerBusiness.dealer_user_id == target.id)
        )
    ).scalars().all()
    for dealer in dealers:
        dealer.dealer_user_id = None
    await db.flush()


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",", 1)[0].strip() if forwarded else request.client.host if request.client else None


async def _apply_access(
    db: AsyncSession,
    *,
    target: User,
    products: set[str],
    actor: User,
    reason: str,
) -> None:
    for product in ProductAccountType:
        await set_product_access(
            db,
            user=target,
            product=product,
            enabled=product.value in products,
            actor_user_id=actor.id,
            reason=reason,
        )
    synchronize_external_compatibility_role(target, products)


@router.post("/invite", response_model=AccessMutationResult, status_code=status.HTTP_201_CREATED)
async def invite_client_access(
    body: ClientAccessInviteRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AccessMutationResult:
    _require_super_admin(user)
    before = None
    client, intake = await _resolve_subject(db, body.subject_kind, body.subject_id)
    client = await _ensure_subject_client(db, client=client, intake=intake)
    products = {product.value for product in body.account_types}
    target = await _load_or_create_external_user(
        db,
        client=client,
        email=body.email,
        name=(body.name or client.name or body.email.split("@", 1)[0]).strip(),
        products=products,
    )
    before = access_state(target)
    audit_scope_ids: list[UUID] = []
    if ProductAccountType.AUDIT.value in products:
        audit_scope_ids = await _assign_audit_scopes(
            db,
            target=target,
            client=client,
            intake=intake,
            profile_ids=body.audit_profile_ids,
            actor=user,
        )
    else:
        await _clear_audit_scopes(db, target=target)
    await _apply_access(db, target=target, products=products, actor=user, reason=body.reason)
    invitation_sent = False
    if target.clerk_id is None:
        invited = await clerk_service.invite_user(
            email=target.email,
            name=target.name,
            role=target.role,
            redirect_url=(
                "https://app.qualifiedcommercial.com/sign-in"
                if "funding" in products
                else "https://audit.qualifiedcommercial.com/sign-in"
            ),
            account_types=sorted(products),
            account_status=target.account_status,
        )
        invitation_sent = invited is not None
        target.last_invited_at = datetime.now(UTC)
        target.last_invite_status = "sent" if invitation_sent else "failed"
        target.last_invite_error = None if invitation_sent else "Clerk invitation was not accepted"
    else:
        target.last_invite_status = "active"
        target.last_invite_error = None
    clerk_synced = await clerk_service.update_user_access_metadata(
        target.clerk_id or "",
        role=target.role,
        account_types=sorted(products),
        account_status=target.account_status,
    )
    after = access_state(target)
    record_access_event(
        db,
        user_id=target.id,
        actor_user_id=user.id,
        action="client_access.invited" if target.clerk_id is None else "client_access.granted",
        reason=body.reason,
        before_state=before,
        after_state={**after, "audit_profile_ids": [str(value) for value in audit_scope_ids]},
        metadata=request_metadata(
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent")
        ),
    )
    await db.commit()
    return AccessMutationResult(
        user_id=target.id,
        account_types=[ProductAccountType(value) for value in sorted(products)],
        account_status=target.account_status,
        login_state=_login_state(target),
        invitation_sent=invitation_sent,
        clerk_synced=clerk_synced,
        audit_scope_ids=audit_scope_ids,
    )


@router.patch("/users/{user_id}", response_model=AccessMutationResult)
async def update_client_access(
    user_id: UUID,
    body: ClientAccessUpdateRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AccessMutationResult:
    _require_super_admin(user)
    target = (
        await db.execute(
            select(User)
            .options(selectinload(User.product_accesses), selectinload(User.client))
            .where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target is None or target.role not in _EXTERNAL_ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client login not found")
    products = {product.value for product in body.account_types}
    if body.account_status == "active" and not products:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "An active client login must have Funding, Audit, or both.",
        )
    before = access_state(target)
    client = target.client
    if "funding" in products and client is None:
        client = Client(
            user_id=target.id,
            name=target.name,
            email=target.email,
            source_channel="admin_access",
            client_experience_mode="self_directed",
        )
        db.add(client)
        await db.flush()
        target.client = client
    audit_scope_ids: list[UUID] = []
    if "audit" in products:
        audit_scope_ids = await _assign_audit_scopes(
            db,
            target=target,
            client=client,
            intake=None,
            profile_ids=body.audit_profile_ids,
            actor=user,
        )
    else:
        await _clear_audit_scopes(db, target=target)
    await _apply_access(db, target=target, products=products, actor=user, reason=body.reason)
    was_suspended = target.account_status == "suspended"
    target.account_status = body.account_status
    target.suspended_at = datetime.now(UTC) if body.account_status == "suspended" else None
    target.suspended_by_user_id = user.id if body.account_status == "suspended" else None
    sessions_revoked = False
    if was_suspended != (body.account_status == "suspended"):
        await clerk_service.set_user_suspended(
            target.clerk_id or "", body.account_status == "suspended"
        )
    if body.account_status == "suspended":
        sessions_revoked = await clerk_service.revoke_user_sessions(target.clerk_id or "")
    clerk_synced = await clerk_service.update_user_access_metadata(
        target.clerk_id or "",
        role=target.role,
        account_types=sorted(products),
        account_status=target.account_status,
    )
    after = access_state(target)
    record_access_event(
        db,
        user_id=target.id,
        actor_user_id=user.id,
        action="client_access.suspended" if body.account_status == "suspended" else "client_access.updated",
        reason=body.reason,
        before_state=before,
        after_state={**after, "audit_profile_ids": [str(value) for value in audit_scope_ids]},
        metadata=request_metadata(
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent")
        ),
    )
    await db.commit()
    return AccessMutationResult(
        user_id=target.id,
        account_types=[ProductAccountType(value) for value in sorted(products)],
        account_status=target.account_status,
        login_state=_login_state(target),
        clerk_synced=clerk_synced,
        sessions_revoked=sessions_revoked,
        audit_scope_ids=audit_scope_ids,
    )


@router.post("/users/{user_id}/resend-invite", response_model=AccessMutationResult)
async def resend_client_invite(
    user_id: UUID,
    body: AccessReasonRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AccessMutationResult:
    _require_super_admin(user)
    target = (
        await db.execute(
            select(User)
            .options(selectinload(User.product_accesses))
            .where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target is None or target.role not in _EXTERNAL_ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client login not found")
    if target.clerk_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This login is already active; revoke sessions instead of resending an invitation.",
        )
    products = sorted(assigned_product_values(target))
    invited = await clerk_service.invite_user(
        email=target.email,
        name=target.name,
        role=target.role,
        redirect_url=(
            "https://app.qualifiedcommercial.com/sign-in"
            if "funding" in products
            else "https://audit.qualifiedcommercial.com/sign-in"
        ),
        account_types=products,
        account_status=target.account_status,
    )
    target.last_invited_at = datetime.now(UTC)
    target.last_invite_status = "sent" if invited else "failed"
    target.last_invite_error = None if invited else "Clerk invitation was not accepted"
    record_access_event(
        db,
        user_id=target.id,
        actor_user_id=user.id,
        action="client_access.invite_resent",
        reason=body.reason,
        before_state=access_state(target),
        after_state=access_state(target),
        metadata=request_metadata(
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent")
        ),
    )
    await db.commit()
    return AccessMutationResult(
        user_id=target.id,
        account_types=[ProductAccountType(value) for value in products],
        account_status=target.account_status,
        login_state=_login_state(target),
        invitation_sent=invited is not None,
    )


@router.post("/users/{user_id}/revoke-sessions", response_model=AccessMutationResult)
async def revoke_client_sessions(
    user_id: UUID,
    body: AccessReasonRequest,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AccessMutationResult:
    _require_super_admin(user)
    target = (
        await db.execute(
            select(User)
            .options(selectinload(User.product_accesses))
            .where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if target is None or target.role not in _EXTERNAL_ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client login not found")
    revoked = await clerk_service.revoke_user_sessions(target.clerk_id or "")
    record_access_event(
        db,
        user_id=target.id,
        actor_user_id=user.id,
        action="client_access.sessions_revoked",
        reason=body.reason,
        before_state=access_state(target),
        after_state=access_state(target),
        metadata=request_metadata(
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent")
        ),
    )
    await db.commit()
    products = sorted(assigned_product_values(target))
    return AccessMutationResult(
        user_id=target.id,
        account_types=[ProductAccountType(value) for value in products],
        account_status=target.account_status,
        login_state=_login_state(target),
        sessions_revoked=revoked,
    )
