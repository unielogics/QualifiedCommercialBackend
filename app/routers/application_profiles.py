from __future__ import annotations

# FastAPI dependency injection is expressed through callable defaults throughout
# this codebase. Ruff B008 is not applicable to those framework declarations.
# ruff: noqa: B008
import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.dealer_os.models import DealerBusiness, DealerOwner, DealerPlaidItem
from app.dealer_os.services import bank_consent as dealer_bank_consent
from app.dealer_os.services import consent_delivery, plaid_client
from app.deps import CurrentUser
from app.enums import Role
from app.models.application_profile import (
    ApplicationBankConsent,
    ApplicationOwner,
    ApplicationPlaidItem,
    ApplicationProfile,
)
from app.models.bucket import BucketFile
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_profile import (
    ApplicationBankConnectionRead,
    ApplicationBankConsentGrant,
    ApplicationBankState,
    ApplicationEvidenceRead,
    ApplicationPlaidExchange,
    ApplicationPlaidItemPatch,
    ApplicationPlaidLinkTokenRead,
    ApplicationPlaidRefreshRead,
    ApplicationProfileRead,
    ApplicationProfileResolve,
    ClassificationConfirm,
    ClassificationPatch,
    ClassificationPreview,
    FileCreditInviteBatch,
    FileCreditInviteRead,
    FileCreditInviteRequest,
    FileOwnerCreate,
    FileOwnerPatch,
    FileOwnerRead,
    FileOwnerRequirementState,
    PublicFileOwnerConsentRead,
    PublicFileOwnerConsentResult,
    PublicFileOwnerConsentSubmit,
    UnifiedAuditEvent,
)
from app.services import application_profiles as profiles

router = APIRouter(prefix="/application-profiles", tags=["application-profiles"])


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.client.host if request.client else None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credit_tier(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 720:
        return "Tier 1"
    if score >= 660:
        return "Tier 2"
    return "Tier 3"


def _require_profile_bank_client(profile: ApplicationProfile, user: User) -> None:
    """Dealer OS bank actions belong to the authenticated Audit client."""
    if profile.dealer_id and user.role != Role.DEALER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The client must complete this bank action from their own account or secure link",
        )


async def _owner_for_profile(
    db: AsyncSession, profile: ApplicationProfile, owner_id: UUID
) -> ApplicationOwner | DealerOwner:
    model = DealerOwner if profile.dealer_id else ApplicationOwner
    predicate = (
        DealerOwner.dealer_id == profile.dealer_id
        if profile.dealer_id
        else ApplicationOwner.profile_id == profile.id
    )
    owner = (
        await db.execute(select(model).where(model.id == owner_id, predicate))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found for this file")
    return owner


async def _assert_unique_email(
    db: AsyncSession,
    profile: ApplicationProfile,
    email: str | None,
    *,
    exclude_id: UUID | None = None,
) -> None:
    email = profiles.normalized_email(email)
    if not email:
        return
    model = DealerOwner if profile.dealer_id else ApplicationOwner
    predicate = (
        DealerOwner.dealer_id == profile.dealer_id
        if profile.dealer_id
        else ApplicationOwner.profile_id == profile.id
    )
    stmt = select(model.id).where(predicate, func.lower(model.email) == email)
    if exclude_id:
        stmt = stmt.where(model.id != exclude_id)
    if (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Every owner must use a different personal email",
        )


@router.post("/resolve", response_model=ApplicationProfileRead)
async def resolve_application_profile(
    payload: ApplicationProfileResolve,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationProfileRead:
    profile = await profiles.resolve_profile(db, payload.source_kind, payload.source_id, user)
    await db.commit()
    await db.refresh(profile)
    return profiles.profile_read(profile)


@router.get("/{profile_id}", response_model=ApplicationProfileRead)
async def get_application_profile(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationProfileRead:
    return profiles.profile_read(await profiles.load_profile(db, profile_id, user))


@router.get("/{profile_id}/owners", response_model=list[FileOwnerRead])
async def list_application_owners(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[FileOwnerRead]:
    profile = await profiles.load_profile(db, profile_id, user)
    return [profiles.owner_read(owner) for owner in await profiles.owner_rows(db, profile)]


@router.post(
    "/{profile_id}/owners",
    response_model=FileOwnerRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_application_owner(
    profile_id: UUID,
    payload: FileOwnerCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileOwnerRead:
    profile = await profiles.load_profile(db, profile_id, user)
    await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    existing = await profiles.owner_rows(db, profile)
    if len(existing) >= profiles.MAX_OWNERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A file may contain at most five owners")
    await _assert_unique_email(db, profile, str(payload.email) if payload.email else None)
    if payload.is_primary and any(owner.is_primary for owner in existing):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This file already has a primary owner")
    values = payload.model_dump()
    values["email"] = profiles.normalized_email(str(payload.email) if payload.email else None)
    model = DealerOwner if profile.dealer_id else ApplicationOwner
    row = model(**({"dealer_id": profile.dealer_id} if profile.dealer_id else {"profile_id": profile.id}), **values)
    db.add(row)
    await db.flush()
    await profiles.log_profile_action(
        db, profile, user, "owner.create", f"Added owner {row.full_name}", target_type="owner", target_id=row.id,
        metadata={"ownership_pct": float(row.ownership_pct or 0)},
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Owner conflicts with an existing row") from exc
    await db.refresh(row)
    return profiles.owner_read(row)


@router.patch("/{profile_id}/owners/{owner_id}", response_model=FileOwnerRead)
async def update_application_owner(
    profile_id: UUID,
    owner_id: UUID,
    payload: FileOwnerPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileOwnerRead:
    profile = await profiles.load_profile(db, profile_id, user)
    owner = await _owner_for_profile(db, profile, owner_id)
    patch = payload.model_dump(exclude_unset=True)
    if "email" in patch:
        patch["email"] = profiles.normalized_email(str(patch["email"]) if patch["email"] else None)
        await _assert_unique_email(db, profile, patch["email"], exclude_id=owner.id)
    before = {key: getattr(owner, key) for key in patch}
    for key, value in patch.items():
        setattr(owner, key, value)
    await profiles.log_profile_action(
        db, profile, user, "owner.update", f"Updated owner {owner.full_name}", target_type="owner", target_id=owner.id,
        metadata={"before": {k: str(v) if v is not None else None for k, v in before.items()}, "after": {k: str(v) if v is not None else None for k, v in patch.items()}},
    )
    await db.commit()
    await db.refresh(owner)
    return profiles.owner_read(owner)


@router.delete("/{profile_id}/owners/{owner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_application_owner(
    profile_id: UUID,
    owner_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    profile = await profiles.load_profile(db, profile_id, user)
    owner = await _owner_for_profile(db, profile, owner_id)
    if owner.invite_sent_at is not None or owner.credit_pulled_at is not None or owner.credit_pull_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This owner has credit history and cannot be deleted")
    name = owner.full_name
    await db.delete(owner)
    await profiles.log_profile_action(
        db, profile, user, "owner.delete", f"Removed owner {name}", target_type="owner", target_id=owner_id,
    )
    await db.commit()


@router.get("/{profile_id}/verification", response_model=FileOwnerRequirementState)
async def get_application_verification(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> FileOwnerRequirementState:
    profile = await profiles.load_profile(db, profile_id, user)
    return await profiles.verification_state(db, profile)


def _business_label(profile: ApplicationProfile, intake: PublicUnderwritingIntake | None, client: Client | None) -> str:
    return (intake.business_name if intake else None) or (client.name if client else None) or "your application"


async def _mint_credit_invite(
    db: AsyncSession,
    profile: ApplicationProfile,
    owner: ApplicationOwner | DealerOwner,
    user: CurrentUser,
    channel: str,
) -> FileCreditInviteRead:
    verification = await profiles.verification_state(db, profile)
    if not verification.ownership_complete:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Ownership must total 100.00% before credit links are sent; current total is {verification.ownership_total:.2f}%")
    if not owner.credit_required:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Credit authorization is required only for owners with 20% or more ownership")
    if not profiles.normalized_email(owner.email) or not profiles.normalized_phone(owner.phone):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "This owner needs a personal email and valid phone before credit authorization can be sent")
    if profile.dealer_id:
        from app.dealer_os.services import client_room

        dealer = await db.get(DealerBusiness, profile.dealer_id)
        await client_room.ensure_room(db, dealer)
    token = secrets.token_urlsafe(32)
    public_token = token if profile.dealer_id else f"app.{token}"
    owner.invite_token_hash = _hash_token(public_token)
    owner.invite_sent_at = datetime.now(UTC)
    owner.invite_opened_at = None
    await profiles.log_profile_action(
        db, profile, user, "owner.credit_invite", f"Created a private credit link for {owner.full_name}", target_type="owner", target_id=owner.id,
    )
    await db.commit()
    path = f"/credit-consent#t={public_token}"
    if channel == "none":
        return FileCreditInviteRead(owner_id=owner.id, owner_name=owner.full_name, token=public_token, path=path, delivered=True, channel="none", detail="Link created")
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    delivery = await consent_delivery.deliver_link_checked(
        db,
        channel=channel,
        to_email=owner.email,
        to_phone=owner.phone,
        business_name=_business_label(profile, intake, client),
        purpose="authorize a soft credit check",
        path=path,
        rep_name=user.name,
    )
    await profiles.log_profile_action(
        db, profile, user, "owner.credit_invite_delivery", delivery.detail, target_type="owner", target_id=owner.id,
        metadata={"delivered": delivery.ok, "channel": delivery.channel},
    )
    await db.commit()
    return FileCreditInviteRead(owner_id=owner.id, owner_name=owner.full_name, token=public_token, path=path, delivered=delivery.ok, channel=delivery.channel, detail=delivery.detail)


@router.post("/{profile_id}/owners/{owner_id}/credit-invite", response_model=FileCreditInviteRead)
async def create_owner_credit_invite(
    profile_id: UUID,
    owner_id: UUID,
    payload: FileCreditInviteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileCreditInviteRead:
    profile = await profiles.load_profile(db, profile_id, user)
    owner = await _owner_for_profile(db, profile, owner_id)
    return await _mint_credit_invite(db, profile, owner, user, payload.channel)


@router.post("/{profile_id}/owners/credit-invites", response_model=FileCreditInviteBatch)
async def create_pending_owner_credit_invites(
    profile_id: UUID,
    payload: FileCreditInviteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FileCreditInviteBatch:
    profile = await profiles.load_profile(db, profile_id, user)
    verification = await profiles.verification_state(db, profile)
    if not verification.ownership_complete or not verification.owner_contact_complete:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Complete the 100% ownership schedule and every required owner's personal contacts before sending")
    owners = await profiles.owner_rows(db, profile)
    items = []
    for owner in owners:
        if owner.credit_required and not owner.credit_complete:
            items.append(await _mint_credit_invite(db, profile, owner, user, payload.channel))
    return FileCreditInviteBatch(items=items)


async def _public_application_owner(db: AsyncSession, token: str) -> ApplicationOwner:
    if not token.startswith("app."):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    owner = (
        await db.execute(select(ApplicationOwner).where(ApplicationOwner.invite_token_hash == _hash_token(token)))
    ).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    return owner


@router.get("/public/credit-consent/{token}", response_model=PublicFileOwnerConsentRead)
async def public_application_credit_consent(
    token: str, db: AsyncSession = Depends(get_db)
) -> PublicFileOwnerConsentRead:
    owner = await _public_application_owner(db, token)
    profile = await db.get(ApplicationProfile, owner.profile_id)
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile and profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile and profile.client_id else None
    if owner.invite_opened_at is None:
        owner.invite_opened_at = datetime.now(UTC)
        await db.commit()
    fields_needed = [field for field in ("dob", "street", "city", "state", "zip") if not getattr(owner, field)]
    return PublicFileOwnerConsentRead(
        first_name=owner.first_name,
        last_initial=(owner.last_name or "")[:1],
        business_name=_business_label(profile, intake, client),
        fields_needed=fields_needed,
        completed=owner.credit_complete,
    )


@router.post("/public/credit-consent/{token}", response_model=PublicFileOwnerConsentResult)
async def submit_public_application_credit_consent(
    token: str,
    payload: PublicFileOwnerConsentSubmit,
    db: AsyncSession = Depends(get_db),
) -> PublicFileOwnerConsentResult:
    if not token.startswith("app."):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    token_hash = _hash_token(token)
    claimed = (
        await db.execute(
            sa_update(ApplicationOwner)
            .where(ApplicationOwner.invite_token_hash == token_hash)
            .values(invite_token_hash=None)
            .returning(ApplicationOwner.id)
        )
    ).scalar_one_or_none()
    if claimed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This consent link is no longer valid")
    owner = await db.get(ApplicationOwner, claimed)

    async def release() -> None:
        owner.invite_token_hash = token_hash
        await db.commit()

    if not payload.fcra_consent:
        await release()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "FCRA permissible-purpose consent is required")
    profile = await db.get(ApplicationProfile, owner.profile_id)
    state = await profiles.verification_state(db, profile)
    if not state.ownership_complete or not owner.credit_required:
        await release()
        raise HTTPException(status.HTTP_409_CONFLICT, "The ownership schedule changed; contact your representative")
    for field in ("dob", "street", "city", "state", "zip"):
        value = getattr(payload, field)
        if value is not None and not getattr(owner, field):
            setattr(owner, field, value)
    missing = [field for field in ("dob", "street", "city", "state", "zip") if not getattr(owner, field)]
    if missing:
        await release()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Complete these fields: {', '.join(missing)}")
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    if client is None:
        await release()
        raise HTTPException(status.HTTP_409_CONFLICT, "This application is not ready for a credit check")
    from app.services.credit_pull_core import (
        SoftPullApplicant,
        SoftPullDenied,
        SoftPullRateLimited,
        SoftPullUnavailable,
        SoftPullValidationError,
        run_soft_pull,
    )

    try:
        pull = await run_soft_pull(
            db,
            client=client,
            applicant=SoftPullApplicant(
                legal_first_name=owner.first_name,
                legal_last_name=owner.last_name,
                dob=owner.dob,
                street=owner.street,
                city=owner.city,
                state=owner.state,
                zip=owner.zip,
                ssn=payload.ssn,
            ),
        )
    except (SoftPullDenied, SoftPullRateLimited, SoftPullUnavailable, SoftPullValidationError):
        await db.rollback()
        owner = await db.get(ApplicationOwner, claimed)
        owner.invite_token_hash = token_hash
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "We could not complete the credit check right now; please try again shortly") from None
    pull.application_owner_id = owner.id
    owner.credit_pull_id = pull.id
    owner.credit_score = pull.fico
    owner.credit_tier = _credit_tier(pull.fico)
    owner.credit_pulled_at = pull.pulled_at or datetime.now(UTC)
    owner.credit_summary = {"status": str(pull.status), "expires_at": pull.expires_at.isoformat() if pull.expires_at else None}
    await profiles.log_profile_action(db, profile, None, "owner.soft_pull", f"Credit returned for {owner.full_name}", target_type="owner", target_id=owner.id, metadata={"tier": owner.credit_tier})
    await db.commit()
    return PublicFileOwnerConsentResult(completed=True, credit_tier=owner.credit_tier, credit_score_band=_score_band(owner.credit_score))


def _score_band(score: int | None) -> str | None:
    if score is None:
        return None
    low = (score // 50) * 50
    return f"{low}-{low + 49}"


async def _application_consent_granted(db: AsyncSession, profile_id: UUID) -> bool:
    row = (
        await db.execute(
            select(ApplicationBankConsent).where(
                ApplicationBankConsent.profile_id == profile_id,
                ApplicationBankConsent.granted.is_(True),
                ApplicationBankConsent.revoked_at.is_(None),
            ).order_by(ApplicationBankConsent.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


@router.get("/{profile_id}/banks", response_model=ApplicationBankState)
async def get_application_banks(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationBankState:
    profile = await profiles.load_profile(db, profile_id, user)
    disclosure = dealer_bank_consent.disclosure()
    if profile.dealer_id:
        consent = await dealer_bank_consent.has_consent(db, profile.dealer_id)
    else:
        consent = await _application_consent_granted(db, profile.id)
    return ApplicationBankState(
        enabled=plaid_client.enabled(),
        environment=plaid_client.environment(),
        consent_granted=consent,
        disclosure_version=disclosure["version"],
        disclosure_text=disclosure["text"],
        items=await profiles.bank_rows(db, profile),
    )


@router.post("/{profile_id}/bank-consent", response_model=ApplicationBankState)
async def grant_application_bank_consent(
    profile_id: UUID,
    payload: ApplicationBankConsentGrant,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankState:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_profile_bank_client(profile, user)
    disclosure = dealer_bank_consent.disclosure()
    if profile.dealer_id:
        await dealer_bank_consent.record(
            db,
            dealer_id=profile.dealer_id,
            method="self_web",
            consenter_name=payload.consenter_name,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            captured_by_user_id=user.id,
            captured_by_name=user.name,
        )
    else:
        db.add(
            ApplicationBankConsent(
                profile_id=profile.id,
                granted=payload.granted,
                method=payload.method,
                disclosure_version=disclosure["version"],
                disclosure_hash=disclosure["hash"],
                disclosure_text=disclosure["text"],
                consenter_name=payload.consenter_name,
                captured_by_user_id=user.id,
                ip_address=_client_ip(request),
                user_agent=(request.headers.get("user-agent") or "")[:400] or None,
            )
        )
    consent_action = "bank.consent.client" if profile.dealer_id else "bank.consent"
    await profiles.log_profile_action(db, profile, user, consent_action, "Recorded bank statement access authorization")
    await db.commit()
    return await get_application_banks(profile_id, user, db)


@router.post("/{profile_id}/banks/link-token", response_model=ApplicationPlaidLinkTokenRead)
async def create_application_plaid_link_token(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationPlaidLinkTokenRead:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_profile_bank_client(profile, user)
    consent = await dealer_bank_consent.has_consent(db, profile.dealer_id) if profile.dealer_id else await _application_consent_granted(db, profile.id)
    if not consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Record bank access authorization before opening Plaid")
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    token = await plaid_client.create_link_token(
        dealer_id=str(profile.id), dealer_name=client.name if client else "Qualified Commercial application"
    )
    return ApplicationPlaidLinkTokenRead(link_token=token)


async def _make_primary(db: AsyncSession, profile: ApplicationProfile, item) -> None:
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = DealerPlaidItem.dealer_id == profile.dealer_id if profile.dealer_id else ApplicationPlaidItem.profile_id == profile.id
    await db.execute(sa_update(model).where(predicate, model.id != item.id).values(is_primary_operating=False))
    item.is_primary_operating = True


@router.post("/{profile_id}/banks/exchange", response_model=ApplicationBankConnectionRead, status_code=status.HTTP_201_CREATED)
async def exchange_application_plaid_token(
    profile_id: UUID,
    payload: ApplicationPlaidExchange,
    background: BackgroundTasks,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_profile_bank_client(profile, user)
    consent = await dealer_bank_consent.has_consent(db, profile.dealer_id) if profile.dealer_id else await _application_consent_granted(db, profile.id)
    if not consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bank access authorization is required")
    try:
        access_token, plaid_item_id = await plaid_client.exchange_public_token(payload.public_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    existing = (await db.execute(select(model).where(model.item_id == plaid_item_id))).scalar_one_or_none()
    owner_id = existing.dealer_id if existing is not None and profile.dealer_id else existing.profile_id if existing is not None else None
    expected = profile.dealer_id if profile.dealer_id else profile.id
    if existing is not None and owner_id != expected:
        raise HTTPException(status.HTTP_409_CONFLICT, "This bank connection belongs to another file")
    if existing:
        item = existing
        item.encrypted_access_token = plaid_client.encrypt_token(access_token)
        item.status = "active"
        item.error = None
    else:
        item = model(
            **({"dealer_id": profile.dealer_id} if profile.dealer_id else {"profile_id": profile.id}),
            item_id=plaid_item_id,
            institution_name=payload.institution_name,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            status="active",
            next_refresh_at=datetime.now(UTC),
        )
        db.add(item)
    await db.flush()
    current = await profiles.bank_rows(db, profile)
    if payload.is_primary_operating or not any(row.is_primary_operating for row in current):
        await _make_primary(db, profile, item)
    connect_action = "plaid.connect.client" if profile.dealer_id else "plaid.connect"
    await profiles.log_profile_action(db, profile, user, connect_action, f"Connected {payload.institution_name or 'business bank'}", target_type="plaid_item", target_id=item.id)
    await db.commit()
    await db.refresh(item)
    if profile.dealer_id:
        from app.db import SessionLocal
        from app.dealer_os.services.plaid_sync import sync_item as sync_dealer_item

        async def run_dealer_sync(item_id: UUID) -> None:
            async with SessionLocal() as session:
                target = await session.get(DealerPlaidItem, item_id)
                if target:
                    await sync_dealer_item(session, target)
                    await session.commit()
        background.add_task(run_dealer_sync, item.id)
    else:
        from app.services.application_plaid_sync import sync_item_background

        background.add_task(sync_item_background, item.id)
    rows = await profiles.bank_rows(db, profile)
    return next(row for row in rows if row.id == item.id)


@router.patch("/{profile_id}/banks/{item_id}", response_model=ApplicationBankConnectionRead)
async def update_application_bank(
    profile_id: UUID,
    item_id: UUID,
    payload: ApplicationPlaidItemPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    profile = await profiles.load_profile(db, profile_id, user)
    if payload.is_primary_operating is not None:
        _require_profile_bank_client(profile, user)
    if payload.auto_refresh is not None and user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may change automatic refresh")
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = DealerPlaidItem.dealer_id == profile.dealer_id if profile.dealer_id else ApplicationPlaidItem.profile_id == profile.id
    item = (await db.execute(select(model).where(model.id == item_id, predicate))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
    if payload.auto_refresh is not None:
        item.auto_refresh = payload.auto_refresh
    if payload.is_primary_operating is True:
        await _make_primary(db, profile, item)
    elif payload.is_primary_operating is False and item.is_primary_operating:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select another primary operating bank first")
    action = (
        "plaid.primary.client"
        if profile.dealer_id and payload.is_primary_operating is not None
        else "plaid.update"
    )
    await profiles.log_profile_action(db, profile, user, action, "Updated bank connection controls", target_type="plaid_item", target_id=item.id)
    await db.commit()
    return next(row for row in await profiles.bank_rows(db, profile) if row.id == item.id)


@router.post("/{profile_id}/banks/{item_id}/refresh", response_model=ApplicationPlaidRefreshRead)
async def refresh_application_bank(
    profile_id: UUID,
    item_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationPlaidRefreshRead:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may retry statement synchronization")
    profile = await profiles.load_profile(db, profile_id, user)
    if profile.dealer_id:
        item = await db.get(DealerPlaidItem, item_id)
        if item is None or item.dealer_id != profile.dealer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
        from app.dealer_os.services.plaid_sync import sync_item
    else:
        item = await db.get(ApplicationPlaidItem, item_id)
        if item is None or item.profile_id != profile.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
        from app.services.application_plaid_sync import sync_item
    result = await sync_item(db, item)
    await profiles.log_profile_action(db, profile, user, "plaid.refresh.recovery", "Retried statement synchronization", target_type="plaid_item", target_id=item.id)
    await db.commit()
    return ApplicationPlaidRefreshRead(**result)


@router.delete("/{profile_id}/banks/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_application_bank(
    profile_id: UUID,
    item_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may disconnect a bank")
    profile = await profiles.load_profile(db, profile_id, user)
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = DealerPlaidItem.dealer_id == profile.dealer_id if profile.dealer_id else ApplicationPlaidItem.profile_id == profile.id
    item = (await db.execute(select(model).where(model.id == item_id, predicate))).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if token:
        try:
            await plaid_client.item_remove(token)
        except plaid_client.PlaidUnavailable:
            pass
    was_primary = item.is_primary_operating
    item.status = "removed"
    item.encrypted_access_token = None
    item.is_primary_operating = False
    if was_primary:
        replacement = (await db.execute(select(model).where(predicate, model.id != item.id, model.status != "removed").order_by(model.created_at.asc()).limit(1))).scalar_one_or_none()
        if replacement:
            await _make_primary(db, profile, replacement)
    await profiles.log_profile_action(db, profile, user, "plaid.disconnect.recovery", "Disconnected bank; previously collected statements were retained", target_type="plaid_item", target_id=item.id)
    await db.commit()


@router.get("/{profile_id}/evidence", response_model=ApplicationEvidenceRead)
async def get_application_evidence(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationEvidenceRead:
    profile = await profiles.load_profile(db, profile_id, user)
    state = await profiles.evidence_state(db, profile)
    for file in state.files:
        file.preview_url = f"/api/v1/application-profiles/{profile.id}/evidence/files/{file.id}/url"
    return state


@router.get("/{profile_id}/evidence/files/{file_id}/url")
async def get_application_evidence_file_url(
    profile_id: UUID,
    file_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    profile = await profiles.load_profile(db, profile_id, user)
    evidence = await profiles.evidence_state(db, profile)
    if file_id not in {row.id for row in evidence.files}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence file not found")
    file = await db.get(BucketFile, file_id)
    from app.routers.buckets import _download_url

    await profiles.log_profile_action(db, profile, user, "evidence.preview", f"Opened {file.file_name}", target_type="file", target_id=file.id)
    await db.commit()
    return {"url": _download_url(file.s3_key, disposition="inline", content_type=file.content_type), "expires_in": 900}


def _classification_dict(profile: ApplicationProfile) -> dict:
    return {
        "vertical": profile.vertical,
        "funding_category": profile.funding_category,
        "entity_type": profile.entity_type,
        "industry": profile.industry,
        "naics_code": profile.naics_code,
        "naics_label": profile.naics_label,
        "custom_industry": profile.custom_industry,
    }


@router.post("/{profile_id}/classification/preview", response_model=ClassificationPreview)
async def preview_application_classification(
    profile_id: UUID,
    payload: ClassificationPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClassificationPreview:
    profile = await profiles.load_profile(db, profile_id, user)
    return ClassificationPreview(
        profile_id=profile.id,
        current_revision=profile.classification_revision,
        before=_classification_dict(profile),
        after=payload.model_dump(),
        effects=[
            "Existing evidence and completed credit history will be preserved",
            "Document requirements and readiness blockers will be recalculated",
            "Previous AI conclusions will be marked stale and a fresh review queued",
        ],
    )


@router.post("/{profile_id}/classification/confirm", response_model=ApplicationProfileRead)
async def confirm_application_classification(
    profile_id: UUID,
    payload: ClassificationConfirm,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationProfileRead:
    profile = await profiles.load_profile(db, profile_id, user)
    await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    if profile.classification_revision != payload.expected_revision:
        raise HTTPException(status.HTTP_409_CONFLICT, "Classification changed; review the latest values before confirming")
    before = _classification_dict(profile)
    for key, value in payload.model_dump(exclude={"expected_revision"}).items():
        setattr(profile, key, value)
    profile.classification_revision += 1
    profile.classified_at = datetime.now(UTC)
    profile.classified_by_user_id = user.id
    profile.backfill_needs_review = False
    profile.classification_state = {"analysis_status": "stale", "previous": before, "current": _classification_dict(profile)}
    if profile.intake_id:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake:
            intake.loan_purpose = profile.funding_category
            state = dict(intake.intake_state or {})
            detail = dict(state.get("main_street_details") or {})
            detail.update({"industry": profile.industry, "entity_type": profile.entity_type, "naics_code": profile.naics_code, "naics_label": profile.naics_label})
            state["main_street_details"] = detail
            intake.intake_state = state
            from app.services.operator_file_links import queue_link_change_review

            review = await queue_link_change_review(db, intake=intake, requested_by_user_id=user.id)
            intake.latest_review_id = review.id
    await profiles.log_profile_action(db, profile, user, "classification.confirm", "Changed file classification and queued requirement review", metadata={"before": before, "after": _classification_dict(profile), "revision": profile.classification_revision})
    await db.commit()
    await db.refresh(profile)
    return profiles.profile_read(profile)


@router.get("/{profile_id}/audit", response_model=list[UnifiedAuditEvent])
async def get_application_audit(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = 250,
) -> list[UnifiedAuditEvent]:
    profile = await profiles.load_profile(db, profile_id, user)
    return await profiles.audit_events(db, profile, min(max(limit, 1), 500))
