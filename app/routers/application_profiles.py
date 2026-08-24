from __future__ import annotations

# FastAPI dependency injection is expressed through callable defaults throughout
# this codebase. Ruff B008 is not applicable to those framework declarations.
# ruff: noqa: B008
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, or_, select
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
    ApplicationExtractedFact,
    ApplicationOwner,
    ApplicationPlaidItem,
    ApplicationProfile,
    ApplicationTaxonomyEntry,
    ApplicationVerificationInvitation,
    FundingCategory,
    PlaidAssetReport,
)
from app.models.bucket import BucketFile
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_profile import (
    ApplicationBankConnectionRead,
    ApplicationBankConsentGrant,
    ApplicationBankState,
    ApplicationDraftAnalysisStatus,
    ApplicationEvidenceRead,
    ApplicationIntelligenceRead,
    ApplicationPlaidExchange,
    ApplicationPlaidItemPatch,
    ApplicationPlaidLinkTokenRead,
    ApplicationPlaidRefreshRead,
    ApplicationPlaidUpdateLinkRequest,
    ApplicationProfileRead,
    ApplicationProfileResolve,
    ClassificationConfirm,
    ClassificationPatch,
    ClassificationPreview,
    ExtractedFactRead,
    ExtractedFactReview,
    FileCreditInviteBatch,
    FileCreditInviteRead,
    FileCreditInviteRequest,
    FileOwnerCreate,
    FileOwnerPatch,
    FileOwnerRead,
    FileOwnerRequirementState,
    FundingCategoryCreate,
    FundingCategoryRead,
    ManualBankOverrideRequest,
    PlaidAssetReportCreate,
    PlaidAssetReportRead,
    PublicBankVerificationRead,
    PublicFileOwnerConsentRead,
    PublicFileOwnerConsentResult,
    PublicFileOwnerConsentSubmit,
    SecureBankFileUploadComplete,
    SecureBankFileUploadInit,
    TaxonomyContributionCreate,
    TaxonomyEntryRead,
    TaxonomyReviewRequest,
    TaxonomySearchRead,
    UnifiedAuditEvent,
    VerificationInvitationCreate,
    VerificationInvitationRead,
)
from app.schemas.bucket import BucketFileRead, BucketFileUploadInitResponse
from app.services import application_profiles as profiles
from app.services import plaid_lifecycle

router = APIRouter(prefix="/application-profiles", tags=["application-profiles"])


def _normalize_label(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _taxonomy_read(row: ApplicationTaxonomyEntry) -> TaxonomyEntryRead:
    return TaxonomyEntryRead(
        id=row.id, level=row.level, code=row.code, label=row.label, parent_id=row.parent_id,
        source=row.source, taxonomy_version=row.taxonomy_version, status=row.status,
        aliases=[str(value) for value in (row.aliases or [])],
        originating_profile_id=row.originating_profile_id, canonical_entry_id=row.canonical_entry_id,
    )


@router.get("/taxonomy/search", response_model=TaxonomySearchRead)
async def search_application_taxonomy(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    q: str = "",
    level: int | None = None,
    parent_id: UUID | None = None,
    profile_id: UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> TaxonomySearchRead:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    allowed = [ApplicationTaxonomyEntry.status.in_(["official", "approved"])]
    if profile_id:
        profile = await profiles.load_profile(db, profile_id, user)
        allowed.append(
            (ApplicationTaxonomyEntry.status == "pending")
            & (ApplicationTaxonomyEntry.originating_profile_id == profile.id)
        )
    stmt = select(ApplicationTaxonomyEntry).where(or_(*allowed))
    if level is not None:
        if level not in {2, 3, 6}:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Taxonomy level must be 2, 3, or 6")
        stmt = stmt.where(ApplicationTaxonomyEntry.level == level)
    if parent_id:
        stmt = stmt.where(ApplicationTaxonomyEntry.parent_id == parent_id)
    query = _normalize_label(q)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(or_(
            ApplicationTaxonomyEntry.normalized_label.ilike(like),
            ApplicationTaxonomyEntry.code.ilike(f"%{q.strip()}%"),
            func.cast(ApplicationTaxonomyEntry.aliases, sa.Text).ilike(like),
        ))
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    rows = list((await db.execute(
        stmt.order_by(ApplicationTaxonomyEntry.code.asc().nullslast(), ApplicationTaxonomyEntry.label.asc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all())
    return TaxonomySearchRead(items=[_taxonomy_read(row) for row in rows], total=total, page=page, page_size=page_size)


@router.get("/taxonomy/review-queue", response_model=TaxonomySearchRead)
async def taxonomy_review_queue(
    user: CurrentUser, db: AsyncSession = Depends(get_db), page: int = 1, page_size: int = 50
) -> TaxonomySearchRead:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may review taxonomy contributions")
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    stmt = select(ApplicationTaxonomyEntry).where(ApplicationTaxonomyEntry.status == "pending")
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = list((await db.execute(stmt.order_by(ApplicationTaxonomyEntry.created_at.asc()).offset((page - 1) * page_size).limit(page_size))).scalars().all())
    return TaxonomySearchRead(items=[_taxonomy_read(row) for row in rows], total=total, page=page, page_size=page_size)


@router.post("/{profile_id}/taxonomy/contributions", response_model=TaxonomyEntryRead, status_code=status.HTTP_201_CREATED)
async def contribute_application_taxonomy(
    profile_id: UUID,
    payload: TaxonomyContributionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaxonomyEntryRead:
    profile = await profiles.load_profile(db, profile_id, user)
    if user.role in {Role.VENDOR, Role.LENDER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This role cannot contribute file classifications")
    parent = await db.get(ApplicationTaxonomyEntry, payload.parent_id) if payload.parent_id else None
    expected_parent_level = {3: 2, 6: 3}.get(payload.level)
    if expected_parent_level and (parent is None or parent.level != expected_parent_level):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select a valid parent classification")
    normalized = _normalize_label(payload.label)
    duplicate = (
        await db.execute(
            select(ApplicationTaxonomyEntry).where(
                ApplicationTaxonomyEntry.level == payload.level,
                ApplicationTaxonomyEntry.status.in_(["official", "approved"]),
                or_(
                    ApplicationTaxonomyEntry.normalized_label == normalized,
                    ApplicationTaxonomyEntry.code == payload.code if payload.code else False,
                ),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if duplicate and (not payload.code or duplicate.label.casefold() == payload.label.casefold()):
        return _taxonomy_read(duplicate)
    pending_duplicate = (
        await db.execute(select(ApplicationTaxonomyEntry).where(
            ApplicationTaxonomyEntry.originating_profile_id == profile.id,
            ApplicationTaxonomyEntry.level == payload.level,
            ApplicationTaxonomyEntry.status == "pending",
            ApplicationTaxonomyEntry.normalized_label == normalized,
        ).limit(1))
    ).scalar_one_or_none()
    if pending_duplicate:
        return _taxonomy_read(pending_duplicate)
    row = ApplicationTaxonomyEntry(
        level=payload.level, code=payload.code, label=payload.label.strip(), normalized_label=normalized,
        parent_id=parent.id if parent else None, source="custom", status="pending",
        originating_profile_id=profile.id, created_by_user_id=user.id,
        canonical_entry_id=duplicate.id if duplicate else None,
    )
    db.add(row)
    await db.flush()
    field_map = {2: ("industry_entry_id", "industry"), 3: ("subindustry_entry_id", "subindustry"), 6: ("activity_entry_id", "naics_label")}
    id_field, label_field = field_map[payload.level]
    setattr(profile, id_field, row.id)
    setattr(profile, label_field, row.label)
    if payload.level == 6:
        profile.naics_code = row.code
    profile.classification_provenance = {"status": "pending", "entry_id": str(row.id), "source": "user_contribution"}
    await profiles.log_profile_action(db, profile, user, "taxonomy.contribute", f"Added pending {payload.level}-digit classification {row.label}", target_type="taxonomy", target_id=row.id)
    await db.commit()
    await db.refresh(row)
    return _taxonomy_read(row)


@router.post("/taxonomy/{entry_id}/review", response_model=TaxonomyEntryRead)
async def review_application_taxonomy(
    entry_id: UUID,
    payload: TaxonomyReviewRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TaxonomyEntryRead:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may review taxonomy contributions")
    row = await db.get(ApplicationTaxonomyEntry, entry_id)
    if row is None or row.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pending taxonomy contribution not found")
    if payload.action in {"merge", "map"}:
        canonical = await db.get(ApplicationTaxonomyEntry, payload.canonical_entry_id) if payload.canonical_entry_id else None
        if canonical is None or canonical.status not in {"official", "approved"} or canonical.level != row.level:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose a canonical entry at the same level")
        row.status = "merged"
        row.canonical_entry_id = canonical.id
    elif payload.action == "edit":
        if payload.label:
            row.label = payload.label.strip()
            row.normalized_label = _normalize_label(row.label)
        if payload.code:
            if row.level == 6 and len(payload.code) != 6:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Activity codes must contain six digits")
            row.code = payload.code
        row.status = "approved"
    else:
        row.status = "approved" if payload.action == "approve" else "rejected"
    if row.status == "approved":
        matches = [ApplicationTaxonomyEntry.level == row.level, ApplicationTaxonomyEntry.id != row.id]
        duplicate_terms = [ApplicationTaxonomyEntry.normalized_label == row.normalized_label]
        if row.code:
            duplicate_terms.append(ApplicationTaxonomyEntry.code == row.code)
        duplicate = (
            await db.execute(
                select(ApplicationTaxonomyEntry).where(
                    *matches,
                    ApplicationTaxonomyEntry.status.in_(["official", "approved"]),
                    or_(*duplicate_terms),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if duplicate:
            row.status = "merged"
            row.canonical_entry_id = duplicate.id
    row.reviewed_by_user_id = user.id
    row.reviewed_at = datetime.now(UTC)
    row.review_note = payload.note
    if row.originating_profile_id:
        origin = await db.get(ApplicationProfile, row.originating_profile_id)
        if origin:
            await profiles.log_profile_action(
                db,
                origin,
                user,
                "taxonomy.review",
                f"{payload.action.title()} taxonomy contribution {row.label}",
                target_type="taxonomy",
                target_id=row.id,
                metadata={
                    "action": payload.action,
                    "status": row.status,
                    "canonical_entry_id": str(row.canonical_entry_id) if row.canonical_entry_id else None,
                    "note": payload.note,
                },
            )
    await db.commit()
    await db.refresh(row)
    return _taxonomy_read(row)


@router.get("/funding-categories", response_model=list[FundingCategoryRead])
async def list_funding_categories(
    user: CurrentUser, db: AsyncSession = Depends(get_db), vertical: str | None = None, q: str = ""
) -> list[FundingCategoryRead]:
    stmt = select(FundingCategory).where(FundingCategory.status.in_(["active", "needs_configuration"]))
    if vertical:
        stmt = stmt.where(FundingCategory.vertical == vertical)
    if q.strip():
        stmt = stmt.where(FundingCategory.label.ilike(f"%{q.strip()}%"))
    rows = list((await db.execute(stmt.order_by(FundingCategory.label.asc()).limit(100))).scalars().all())
    return [FundingCategoryRead(id=row.id, vertical=row.vertical, slug=row.slug, label=row.label, status=row.status, is_system=row.is_system) for row in rows]


@router.post("/funding-categories", response_model=FundingCategoryRead, status_code=status.HTTP_201_CREATED)
async def create_funding_category(
    payload: FundingCategoryCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> FundingCategoryRead:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may create funding categories")
    slug = _slug(payload.label)
    existing = (await db.execute(select(FundingCategory).where(FundingCategory.vertical == payload.vertical, FundingCategory.slug == slug))).scalar_one_or_none()
    if existing:
        return FundingCategoryRead(id=existing.id, vertical=existing.vertical, slug=existing.slug, label=existing.label, status=existing.status, is_system=existing.is_system)
    row = FundingCategory(vertical=payload.vertical, slug=slug, label=payload.label.strip(), status="needs_configuration", created_by_user_id=user.id, requirements={"template": "generic"})
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return FundingCategoryRead(id=row.id, vertical=row.vertical, slug=row.slug, label=row.label, status=row.status, is_system=row.is_system)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.client.host if request.client else None)


async def _profile_plaid_display_name(db: AsyncSession, profile: ApplicationProfile) -> str:
    if profile.dealer_id:
        dealer = await db.get(DealerBusiness, profile.dealer_id)
        if dealer:
            return dealer.legal_name or dealer.name
    if profile.intake_id:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake:
            return intake.business_name or intake.full_name
    if profile.client_id:
        client = await db.get(Client, profile.client_id)
        if client:
            return client.name
    return "Application"


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
    """A bank authorization can only be performed by the owning client."""
    expected_role = Role.DEALER if profile.dealer_id else Role.CLIENT
    if user.role != expected_role:
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


@router.get("/{profile_id}/extracted-facts", response_model=list[ExtractedFactRead])
async def list_extracted_facts(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> list[ExtractedFactRead]:
    profile = await profiles.load_profile(db, profile_id, user)
    rows = list((await db.execute(select(ApplicationExtractedFact).where(
        ApplicationExtractedFact.profile_id == profile.id,
    ).order_by(ApplicationExtractedFact.created_at.desc()))).scalars().all())
    return [ExtractedFactRead.model_validate(row, from_attributes=True) for row in rows]


@router.post("/{profile_id}/extracted-facts/{fact_id}/review", response_model=ExtractedFactRead)
async def review_extracted_fact(
    profile_id: UUID,
    fact_id: UUID,
    payload: ExtractedFactReview,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ExtractedFactRead:
    profile = await profiles.load_profile(db, profile_id, user)
    fact = (await db.execute(select(ApplicationExtractedFact).where(
        ApplicationExtractedFact.id == fact_id,
        ApplicationExtractedFact.profile_id == profile.id,
    ))).scalar_one_or_none()
    if fact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extracted fact not found")
    fact.status = "accepted" if payload.action == "accept" else "rejected"
    fact.reviewed_by_user_id = user.id
    fact.reviewed_at = datetime.now(UTC)
    allowed_profile_fields = {"funding_category", "entity_type", "industry", "subindustry", "naics_code", "naics_label"}
    if payload.action == "accept" and fact.field_key in allowed_profile_fields:
        raw = fact.value.get("value") if isinstance(fact.value, dict) else None
        if raw not in (None, ""):
            setattr(profile, fact.field_key, str(raw))
    remaining = (await db.execute(select(func.count()).where(
        ApplicationExtractedFact.profile_id == profile.id,
        ApplicationExtractedFact.status == "suggested",
        ApplicationExtractedFact.id != fact.id,
    ))).scalar_one()
    if remaining == 0:
        profile.extraction_reviewed_at = datetime.now(UTC)
    await profiles.log_profile_action(db, profile, user, f"extraction.{fact.status}", f"{fact.status.title()} extracted {fact.field_key}", target_type="extracted_fact", target_id=fact.id)
    await db.commit()
    await db.refresh(fact)
    return ExtractedFactRead.model_validate(fact, from_attributes=True)


@router.post("/{profile_id}/finalize", response_model=ApplicationProfileRead)
async def finalize_application_draft(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationProfileRead:
    profile = await profiles.load_profile(db, profile_id, user)
    draft_status = await profiles.draft_analysis_status(db, profile)
    if draft_status.processing_file_count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Wait for {draft_status.processing_file_count} file analysis job(s) to finish",
        )
    if draft_status.failed_file_count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Retry or review {draft_status.failed_file_count} failed file analysis job(s)",
        )
    if draft_status.suggested_fact_count:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Review {draft_status.suggested_fact_count} extracted fact(s) before finalizing this file",
        )
    profile.is_draft = False
    profile.draft_finalized_at = datetime.now(UTC)
    profile.extraction_reviewed_at = profile.extraction_reviewed_at or datetime.now(UTC)
    await profiles.log_profile_action(db, profile, user, "draft.finalize", "Finalized the extracted application draft")
    await db.commit()
    await db.refresh(profile)
    return profiles.profile_read(profile)


@router.get("/{profile_id}/draft-status", response_model=ApplicationDraftAnalysisStatus)
async def get_application_draft_status(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationDraftAnalysisStatus:
    profile = await profiles.load_profile(db, profile_id, user)
    return await profiles.draft_analysis_status(db, profile)


@router.post("/{profile_id}/draft", response_model=ApplicationProfileRead)
async def mark_application_draft(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationProfileRead:
    profile = await profiles.load_profile(db, profile_id, user)
    profile.is_draft = True
    profile.draft_finalized_at = None
    await profiles.log_profile_action(db, profile, user, "draft.start", "Started a resumable evidence-first application draft")
    await db.commit()
    await db.refresh(profile)
    return profiles.profile_read(profile)


@router.get("/{profile_id}/intelligence", response_model=ApplicationIntelligenceRead)
async def get_application_intelligence(
    profile_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> ApplicationIntelligenceRead:
    profile = await profiles.load_profile(db, profile_id, user)
    return await profiles.intelligence_state(db, profile)


@router.post("/{profile_id}/bank-invitations", response_model=VerificationInvitationRead, status_code=status.HTTP_201_CREATED)
async def create_bank_verification_invitation(
    profile_id: UUID,
    payload: VerificationInvitationCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> VerificationInvitationRead:
    profile = await profiles.load_profile(db, profile_id, user)
    if user.role in {Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only scoped staff may send a bank verification request")
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    email = profiles.normalized_email(str(payload.recipient_email) if payload.recipient_email else (intake.email if intake else None))
    phone = profiles.normalized_phone(payload.recipient_phone or (intake.phone if intake else None))
    if payload.channel == "email" and not email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A client email is required")
    if payload.channel == "sms" and not phone:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A consented client phone is required")
    token = f"bank.{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(UTC) + timedelta(days=7)
    row = ApplicationVerificationInvitation(
        profile_id=profile.id, token_hash=_hash_token(token), recipient_email=email, recipient_phone=phone,
        created_by_user_id=user.id, expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    path = f"/application-verification#t={token}"
    if payload.channel == "none":
        row.delivery_status = "created"
    else:
        delivery = await consent_delivery.deliver_link_checked(
            db, channel=payload.channel, to_email=email, to_phone=phone,
            business_name=_business_label(profile, intake, client),
            purpose="connect or upload business bank statements", path=path, rep_name=user.name,
        )
        row.delivery_status = "sent" if delivery.ok else "failed"
    await profiles.log_profile_action(db, profile, user, "bank.invitation", "Sent secure business-bank verification request", target_type="verification_invitation", target_id=row.id, metadata={"channel": payload.channel, "expires_at": expires_at.isoformat()})
    await db.commit()
    await db.refresh(row)
    return VerificationInvitationRead(id=row.id, path=path, token=token, delivery_status=row.delivery_status, expires_at=row.expires_at)


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
                ApplicationBankConsent.disclosure_version
                == dealer_bank_consent.BANK_DISCLOSURE_VERSION,
            ).order_by(ApplicationBankConsent.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _public_bank_invitation(
    db: AsyncSession, token: str
) -> tuple[ApplicationVerificationInvitation, ApplicationProfile]:
    if not token.startswith("bank."):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This bank verification link is not valid")
    invitation = (
        await db.execute(select(ApplicationVerificationInvitation).where(
            ApplicationVerificationInvitation.token_hash == _hash_token(token),
        ))
    ).scalar_one_or_none()
    if invitation is None or invitation.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This bank verification link has expired")
    profile = await db.get(ApplicationProfile, invitation.profile_id)
    if profile is None or profile.dealer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This bank verification link is not valid")
    return invitation, profile


@router.get("/public/bank-verification/{token}", response_model=PublicBankVerificationRead)
async def public_bank_verification(
    token: str, db: AsyncSession = Depends(get_db)
) -> PublicBankVerificationRead:
    invitation, profile = await _public_bank_invitation(db, token)
    if invitation.opened_at is None:
        invitation.opened_at = datetime.now(UTC)
        await db.commit()
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    disclosure = dealer_bank_consent.disclosure()
    return PublicBankVerificationRead(
        business_name=_business_label(profile, intake, client),
        disclosure_version=disclosure["version"], disclosure_text=disclosure["text"],
        consent_granted=await _application_consent_granted(db, profile.id),
        items=await profiles.bank_rows(db, profile),
        manual_statement_months=await profiles.manual_statement_months(db, profile),
        assets_enabled=plaid_client.assets_enabled(),
        asset_reports=[
            PlaidAssetReportRead.model_validate(row)
            for row in await plaid_lifecycle.owner_asset_reports(db, profile_id=profile.id)
        ],
        statement_upload_enabled=bool(intake and intake.bucket_upload_link_id),
        expires_at=invitation.expires_at,
    )


@router.post("/public/bank-verification/{token}/consent", response_model=PublicBankVerificationRead)
async def public_bank_verification_consent(
    token: str,
    payload: ApplicationBankConsentGrant,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PublicBankVerificationRead:
    invitation, profile = await _public_bank_invitation(db, token)
    disclosure = dealer_bank_consent.disclosure()
    db.add(ApplicationBankConsent(
        profile_id=profile.id, granted=payload.granted, method="secure_room",
        disclosure_version=disclosure["version"], disclosure_hash=disclosure["hash"], disclosure_text=disclosure["text"],
        consenter_name=payload.consenter_name, ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:400] or None,
    ))
    await profiles.log_profile_action(db, profile, None, "bank.consent.secure_room", "Client authorized bank evidence from the secure verification room", target_type="verification_invitation", target_id=invitation.id)
    await db.commit()
    return await public_bank_verification(token, db)


@router.post("/public/bank-verification/{token}/link-token", response_model=ApplicationPlaidLinkTokenRead)
async def public_bank_verification_link_token(
    token: str, db: AsyncSession = Depends(get_db)
) -> ApplicationPlaidLinkTokenRead:
    _invitation, profile = await _public_bank_invitation(db, token)
    if not await _application_consent_granted(db, profile.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Accept the bank disclosure before continuing")
    value = await plaid_client.create_link_token(
        dealer_id=str(profile.id),
        dealer_name=await _profile_plaid_display_name(db, profile),
        redirect_override=plaid_client.room_redirect_uri() or None,
    )
    return ApplicationPlaidLinkTokenRead(link_token=value)


@router.post(
    "/public/bank-verification/{token}/banks/{item_id}/update-link-token",
    response_model=ApplicationPlaidLinkTokenRead,
)
async def public_bank_verification_update_link_token(
    token: str,
    item_id: UUID,
    payload: ApplicationPlaidUpdateLinkRequest,
    db: AsyncSession = Depends(get_db),
) -> ApplicationPlaidLinkTokenRead:
    _invitation, profile = await _public_bank_invitation(db, token)
    if not await _application_consent_granted(db, profile.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Accept the current bank disclosure before continuing")
    item = await _profile_plaid_item(db, profile, item_id)
    account_selection = payload.account_selection_enabled or item.update_mode_account_selection
    try:
        value = await plaid_client.create_update_link_token(
            access_token=plaid_lifecycle.decrypted_access_token(item),
            client_user_id=str(profile.id),
            display_name=await _profile_plaid_display_name(db, profile),
            redirect_override=plaid_client.room_redirect_uri() or None,
            account_selection_enabled=account_selection,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ApplicationPlaidLinkTokenRead(link_token=value)


@router.post(
    "/public/bank-verification/{token}/banks/{item_id}/update-complete",
    response_model=ApplicationBankConnectionRead,
)
async def public_bank_verification_update_complete(
    token: str,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    invitation, profile = await _public_bank_invitation(db, token)
    item = await _profile_plaid_item(db, profile, item_id)
    try:
        await plaid_lifecycle.complete_update(item)
    except plaid_client.PlaidUnavailable as exc:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "plaid.update_mode.completed.secure_room",
        "Client repaired the business bank connection",
        target_type="plaid_item",
        target_id=item.id,
        metadata={"invitation_id": str(invitation.id)},
    )
    await db.commit()
    return next(row for row in await profiles.bank_rows(db, profile) if row.id == item.id)


@router.delete(
    "/public/bank-verification/{token}/banks/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def public_bank_verification_disconnect(
    token: str,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    invitation, profile = await _public_bank_invitation(db, token)
    item = await _profile_plaid_item(db, profile, item_id)
    was_primary = item.is_primary_operating
    try:
        await plaid_lifecycle.disconnect_item(db, item)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if was_primary:
        replacement = next(
            (candidate for candidate in await _profile_plaid_items(db, profile) if candidate.id != item.id),
            None,
        )
        if replacement:
            await _make_primary(db, profile, replacement)
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "plaid.disconnect.secure_room",
        "Client disconnected the bank; retained evidence remains on the file",
        target_type="plaid_item",
        target_id=item.id,
        metadata={"invitation_id": str(invitation.id)},
    )
    await db.commit()


_BANK_UPLOAD_EXTENSIONS = {
    ".csv", ".jpeg", ".jpg", ".pdf", ".png", ".webp", ".xls", ".xlsx", ".zip",
}


async def _public_bank_upload_context(
    db: AsyncSession, token: str, file_name: str
) -> tuple[ApplicationVerificationInvitation, ApplicationProfile, PublicUnderwritingIntake]:
    invitation, profile = await _public_bank_invitation(db, token)
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    if intake is None or intake.bucket_upload_link_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Statement upload is not enabled for this verification link",
        )
    suffix = "." + file_name.rsplit(".", 1)[-1].casefold() if "." in file_name else ""
    if suffix not in _BANK_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Upload a PDF, spreadsheet, image, CSV, or ZIP containing business bank statements",
        )
    return invitation, profile, intake


@router.post(
    "/public/bank-verification/{token}/files/upload-init",
    response_model=BucketFileUploadInitResponse,
)
async def public_bank_statement_upload_init(
    token: str,
    payload: SecureBankFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    invitation, profile, intake = await _public_bank_upload_context(db, token, payload.file_name)
    from app.routers.dealer_ai_intake import _start_upload

    result = await _start_upload(
        db,
        intake,
        payload,
        request,
        actor_name="Secure bank verification client",
        actor_email=invitation.recipient_email or "",
    )
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "bank.statement_upload_started.secure_room",
        f"Client started uploading {payload.file_name}",
        target_type="verification_invitation",
        target_id=invitation.id,
    )
    await db.commit()
    return result


@router.post(
    "/public/bank-verification/{token}/files/complete",
    response_model=BucketFileRead,
)
async def public_bank_statement_upload_complete(
    token: str,
    payload: SecureBankFileUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    invitation, profile = await _public_bank_invitation(db, token)
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    if intake is None or intake.bucket_upload_link_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Statement upload is not enabled for this link")
    from app.routers.dealer_ai_intake import _complete_upload

    result = await _complete_upload(
        db,
        intake,
        payload,
        request,
        actor_name="Secure bank verification client",
        actor_email=invitation.recipient_email or "",
    )
    invitation.completed_at = datetime.now(UTC)
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "bank.statement_uploaded.secure_room",
        f"Client uploaded {result.file_name} as business bank evidence",
        target_type="file",
        target_id=result.id,
    )
    await db.commit()
    return result


@router.post("/public/bank-verification/{token}/exchange", response_model=ApplicationBankConnectionRead)
async def public_bank_verification_exchange(
    token: str,
    payload: ApplicationPlaidExchange,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    invitation, profile = await _public_bank_invitation(db, token)
    if not await _application_consent_granted(db, profile.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bank authorization is required")
    try:
        access_token, plaid_item_id = await plaid_client.exchange_public_token(payload.public_token)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    existing = (await db.execute(select(ApplicationPlaidItem).where(ApplicationPlaidItem.item_id == plaid_item_id))).scalar_one_or_none()
    if existing and existing.profile_id != profile.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This bank connection belongs to another file")
    item = existing or ApplicationPlaidItem(profile_id=profile.id, item_id=plaid_item_id)
    if existing is None:
        db.add(item)
    item.institution_name = payload.institution_name
    item.encrypted_access_token = plaid_client.encrypt_token(access_token)
    item.environment = plaid_client.environment()
    item.status = "active"
    item.error = None
    item.update_mode_reason = None
    item.update_mode_account_selection = False
    item.next_refresh_at = datetime.now(UTC)
    await db.flush()
    current = await profiles.bank_rows(db, profile)
    if payload.is_primary_operating or not any(row.is_primary_operating for row in current):
        await _make_primary(db, profile, item)
    invitation.completed_at = datetime.now(UTC)
    await profiles.log_profile_action(db, profile, None, "plaid.connect.secure_room", f"Client connected {payload.institution_name or 'business bank'}", target_type="plaid_item", target_id=item.id)
    await db.commit()
    await db.refresh(item)
    from app.services.application_plaid_sync import sync_item_background

    background.add_task(sync_item_background, item.id)
    return next(row for row in await profiles.bank_rows(db, profile) if row.id == item.id)


@router.patch("/public/bank-verification/{token}/banks/{item_id}", response_model=ApplicationBankConnectionRead)
async def public_bank_verification_primary(
    token: str,
    item_id: UUID,
    payload: ApplicationPlaidItemPatch,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    _invitation, profile = await _public_bank_invitation(db, token)
    item = await _profile_plaid_item(db, profile, item_id)
    if payload.is_primary_operating is not True:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The secure room may only select the primary operating bank")
    await _make_primary(db, profile, item)
    await profiles.log_profile_action(db, profile, None, "plaid.primary.secure_room", "Client selected the primary operating bank", target_type="plaid_item", target_id=item.id)
    await db.commit()
    return next(row for row in await profiles.bank_rows(db, profile) if row.id == item.id)


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
    manual_months = await profiles.manual_statement_months(db, profile)
    return ApplicationBankState(
        enabled=plaid_client.enabled(),
        environment=plaid_client.environment(),
        consent_granted=consent,
        disclosure_version=disclosure["version"],
        disclosure_text=disclosure["text"],
        items=await profiles.bank_rows(db, profile),
        manual_override=bool(profile.bank_verification_override_at),
        manual_override_reason=profile.bank_verification_override_reason,
        manual_statement_months=manual_months,
        assets_enabled=plaid_client.assets_enabled(),
        asset_reports=[
            PlaidAssetReportRead.model_validate(row)
            for row in await plaid_lifecycle.owner_asset_reports(
                db,
                dealer_id=profile.dealer_id,
                profile_id=None if profile.dealer_id else profile.id,
            )
        ],
    )


@router.post("/{profile_id}/banks/manual-override", response_model=ApplicationBankState)
async def approve_manual_bank_evidence(
    profile_id: UUID,
    payload: ManualBankOverrideRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankState:
    profile = await profiles.load_profile(db, profile_id, user)
    if user.role in {Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only scoped staff may approve manual bank evidence")
    months = await profiles.manual_statement_months(db, profile)
    if not months:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Upload and extract at least one business bank statement before overriding Plaid")
    profile.bank_verification_override_at = datetime.now(UTC)
    profile.bank_verification_override_by_user_id = user.id
    profile.bank_verification_override_reason = payload.reason.strip()
    await profiles.log_profile_action(
        db, profile, user, "bank.manual_override", "Approved uploaded bank statements in place of Plaid",
        metadata={"statement_months": months, "reason": payload.reason.strip()},
    )
    await db.commit()
    return await get_application_banks(profile_id, user, db)


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
    token = await plaid_client.create_link_token(
        dealer_id=str(profile.id), dealer_name=await _profile_plaid_display_name(db, profile)
    )
    return ApplicationPlaidLinkTokenRead(link_token=token)


@router.post(
    "/{profile_id}/banks/{item_id}/update-link-token",
    response_model=ApplicationPlaidLinkTokenRead,
)
async def create_application_plaid_update_link_token(
    profile_id: UUID,
    item_id: UUID,
    payload: ApplicationPlaidUpdateLinkRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationPlaidLinkTokenRead:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_profile_bank_client(profile, user)
    consent = (
        await dealer_bank_consent.has_consent(db, profile.dealer_id)
        if profile.dealer_id
        else await _application_consent_granted(db, profile.id)
    )
    if not consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Accept the current bank disclosure before continuing")
    item = await _profile_plaid_item(db, profile, item_id)
    account_selection = payload.account_selection_enabled or item.update_mode_account_selection
    try:
        token = await plaid_client.create_update_link_token(
            access_token=plaid_lifecycle.decrypted_access_token(item),
            client_user_id=str(profile.id),
            display_name=await _profile_plaid_display_name(db, profile),
            account_selection_enabled=account_selection,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return ApplicationPlaidLinkTokenRead(link_token=token)


@router.post(
    "/{profile_id}/banks/{item_id}/update-complete",
    response_model=ApplicationBankConnectionRead,
)
async def complete_application_plaid_update(
    profile_id: UUID,
    item_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ApplicationBankConnectionRead:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_profile_bank_client(profile, user)
    item = await _profile_plaid_item(db, profile, item_id)
    try:
        await plaid_lifecycle.complete_update(item)
    except plaid_client.PlaidUnavailable as exc:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "plaid.update_mode.completed.client",
        "Client repaired the business bank connection",
        target_type="plaid_item",
        target_id=item.id,
    )
    await db.commit()
    return next(row for row in await profiles.bank_rows(db, profile) if row.id == item.id)


async def _make_primary(db: AsyncSession, profile: ApplicationProfile, item) -> None:
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = DealerPlaidItem.dealer_id == profile.dealer_id if profile.dealer_id else ApplicationPlaidItem.profile_id == profile.id
    await db.execute(sa_update(model).where(predicate, model.id != item.id).values(is_primary_operating=False))
    item.is_primary_operating = True


async def _profile_plaid_item(
    db: AsyncSession, profile: ApplicationProfile, item_id: UUID
) -> DealerPlaidItem | ApplicationPlaidItem:
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = (
        DealerPlaidItem.dealer_id == profile.dealer_id
        if profile.dealer_id
        else ApplicationPlaidItem.profile_id == profile.id
    )
    item = (
        await db.execute(select(model).where(model.id == item_id, predicate))
    ).scalar_one_or_none()
    if item is None or item.status == "removed":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank connection not found")
    return item


async def _profile_plaid_items(
    db: AsyncSession, profile: ApplicationProfile
) -> list[DealerPlaidItem | ApplicationPlaidItem]:
    model = DealerPlaidItem if profile.dealer_id else ApplicationPlaidItem
    predicate = (
        DealerPlaidItem.dealer_id == profile.dealer_id
        if profile.dealer_id
        else ApplicationPlaidItem.profile_id == profile.id
    )
    return list(
        (
            await db.execute(
                select(model).where(
                    predicate,
                    model.status != "removed",
                    model.environment == plaid_client.environment(),
                )
            )
        )
        .scalars()
        .all()
    )


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
        item.environment = plaid_client.environment()
        item.status = "active"
        item.error = None
        item.update_mode_reason = None
        item.update_mode_account_selection = False
    else:
        item = model(
            **({"dealer_id": profile.dealer_id} if profile.dealer_id else {"profile_id": profile.id}),
            item_id=plaid_item_id,
            institution_name=payload.institution_name,
            encrypted_access_token=plaid_client.encrypt_token(access_token),
            environment=plaid_client.environment(),
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
    item = await _profile_plaid_item(db, profile, item_id)
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
    item = await _profile_plaid_item(db, profile, item_id)
    if profile.dealer_id:
        from app.dealer_os.services.plaid_sync import sync_item
    else:
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
    profile = await profiles.load_profile(db, profile_id, user)
    if user.role != Role.SUPER_ADMIN:
        _require_profile_bank_client(profile, user)
    item = await _profile_plaid_item(db, profile, item_id)
    was_primary = item.is_primary_operating
    try:
        await plaid_lifecycle.disconnect_item(db, item)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if was_primary:
        replacement = next(
            (candidate for candidate in await _profile_plaid_items(db, profile) if candidate.id != item.id),
            None,
        )
        if replacement:
            await _make_primary(db, profile, replacement)
    action = (
        "plaid.disconnect.recovery"
        if user.role == Role.SUPER_ADMIN
        else "plaid.disconnect.client"
    )
    await profiles.log_profile_action(db, profile, user, action, "Disconnected bank; previously collected statements were retained", target_type="plaid_item", target_id=item.id)
    await db.commit()


def _require_asset_report_staff(user: User) -> None:
    if user.role in {Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only scoped underwriting staff may manage Asset Reports")


async def _profile_asset_report(
    db: AsyncSession, profile: ApplicationProfile, report_id: UUID
) -> PlaidAssetReport:
    predicate = (
        PlaidAssetReport.dealer_id == profile.dealer_id
        if profile.dealer_id
        else PlaidAssetReport.profile_id == profile.id
    )
    report = (
        await db.execute(
            select(PlaidAssetReport).where(PlaidAssetReport.id == report_id, predicate)
        )
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asset Report not found")
    return report


@router.post(
    "/{profile_id}/asset-reports",
    response_model=PlaidAssetReportRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_application_asset_report(
    profile_id: UUID,
    payload: PlaidAssetReportCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlaidAssetReport:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_asset_report_staff(user)
    consent = (
        await dealer_bank_consent.has_consent(db, profile.dealer_id)
        if profile.dealer_id
        else await _application_consent_granted(db, profile.id)
    )
    if not consent:
        raise HTTPException(status.HTTP_409_CONFLICT, "The client must accept the current bank disclosure first")
    try:
        report = await plaid_lifecycle.create_asset_report(
            db,
            items=await _profile_plaid_items(db, profile),
            dealer_id=profile.dealer_id,
            profile_id=None if profile.dealer_id else profile.id,
            days_requested=payload.days_requested,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "plaid.asset_report.requested",
        "Requested a lender-ready Plaid Asset Report",
        target_type="plaid_asset_report",
        target_id=report.id,
        metadata={"days_requested": report.days_requested},
    )
    await db.commit()
    await db.refresh(report)
    return report


@router.get("/{profile_id}/asset-reports/{report_id}/pdf")
async def download_application_asset_report(
    profile_id: UUID,
    report_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    profile = await profiles.load_profile(db, profile_id, user)
    _require_asset_report_staff(user)
    report = await _profile_asset_report(db, profile, report_id)
    if report.status != "ready" or not report.encrypted_asset_report_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "Asset Report is not ready")
    try:
        content = await plaid_client.asset_report_pdf(
            plaid_client.decrypt_token(report.encrypted_asset_report_token) or ""
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="asset-report-{report.id}.pdf"'},
    )


@router.delete(
    "/{profile_id}/asset-reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_application_asset_report(
    profile_id: UUID,
    report_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may remove an Asset Report")
    profile = await profiles.load_profile(db, profile_id, user)
    report = await _profile_asset_report(db, profile, report_id)
    try:
        await plaid_lifecycle.remove_asset_report(report)
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "plaid.asset_report.removed",
        "Removed a Plaid Asset Report because it was no longer needed",
        target_type="plaid_asset_report",
        target_id=report.id,
    )
    await db.commit()


@router.delete("/{profile_id}/banks", status_code=status.HTTP_204_NO_CONTENT)
async def purge_application_banks_on_offboarding(
    profile_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a super admin may purge bank connections")
    profile = await profiles.load_profile(db, profile_id, user)
    try:
        removed = await plaid_lifecycle.purge_owner_connections(
            db,
            dealer_id=profile.dealer_id,
            profile_id=None if profile.dealer_id else profile.id,
        )
    except plaid_client.PlaidUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "plaid.offboarding_purge",
        "Removed all live Plaid Items and Asset Reports during offboarding",
        metadata={"removed_items": removed},
    )
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
        "subindustry": profile.subindustry,
        "naics_code": profile.naics_code,
        "naics_label": profile.naics_label,
        "custom_industry": profile.custom_industry,
        "industry_entry_id": profile.industry_entry_id,
        "subindustry_entry_id": profile.subindustry_entry_id,
        "activity_entry_id": profile.activity_entry_id,
    }


async def _validated_classification(db: AsyncSession, profile: ApplicationProfile, payload: ClassificationPatch) -> dict:
    values = payload.model_dump()
    selected: dict[int, ApplicationTaxonomyEntry] = {}
    for level, field in ((2, "industry_entry_id"), (3, "subindustry_entry_id"), (6, "activity_entry_id")):
        entry_id = values.get(field)
        if entry_id is None:
            continue
        entry = await db.get(ApplicationTaxonomyEntry, entry_id)
        visible = entry and (entry.status in {"official", "approved"} or (entry.status == "pending" and entry.originating_profile_id == profile.id))
        if not visible or entry.level != level:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Select a valid level-{level} classification")
        selected[level] = entry
    if 3 in selected and selected[3].parent_id != values.get("industry_entry_id"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Subindustry does not belong to the selected industry")
    if 6 in selected and selected[6].parent_id != values.get("subindustry_entry_id"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "NAICS activity does not belong to the selected subindustry")
    if 2 in selected:
        values["industry"] = selected[2].label
    if 3 in selected:
        values["subindustry"] = selected[3].label
    if 6 in selected:
        values["naics_code"] = selected[6].code
        values["naics_label"] = selected[6].label
    return values


@router.post("/{profile_id}/classification/preview", response_model=ClassificationPreview)
async def preview_application_classification(
    profile_id: UUID,
    payload: ClassificationPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClassificationPreview:
    profile = await profiles.load_profile(db, profile_id, user)
    after = await _validated_classification(db, profile, payload)
    return ClassificationPreview(
        profile_id=profile.id,
        current_revision=profile.classification_revision,
        before=_classification_dict(profile),
        after=after,
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
    values = await _validated_classification(db, profile, ClassificationPatch(**payload.model_dump(exclude={"expected_revision"})))
    for key, value in values.items():
        setattr(profile, key, value)
    profile.classification_revision += 1
    profile.classified_at = datetime.now(UTC)
    profile.classified_by_user_id = user.id
    profile.backfill_needs_review = False
    entry_status = "canonical"
    for entry_id in (profile.industry_entry_id, profile.subindustry_entry_id, profile.activity_entry_id):
        if entry_id:
            entry = await db.get(ApplicationTaxonomyEntry, entry_id)
            if entry and entry.status == "pending":
                entry_status = "pending"
                break
    profile.classification_provenance = {
        "source": "operator_confirmed",
        "taxonomy_version": profile.taxonomy_version,
        "confirmed_at": profile.classified_at.isoformat(),
        "entry_status": entry_status,
    }
    profile.classification_state = {"analysis_status": "stale", "previous": before, "current": _classification_dict(profile)}
    if profile.intake_id:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake:
            intake.loan_purpose = profile.funding_category
            state = dict(intake.intake_state or {})
            detail = dict(state.get("main_street_details") or {})
            detail.update({"industry": profile.industry, "subindustry": profile.subindustry, "entity_type": profile.entity_type, "naics_code": profile.naics_code, "naics_label": profile.naics_label})
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
