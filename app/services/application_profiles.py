from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.deps import resolve_dealer_scope
from app.dealer_os.models import (
    DealerAuditLog,
    DealerBusiness,
    DealerDocument,
    DealerOwner,
    DealerPlaidItem,
)
from app.enums import Role
from app.models.activity import Activity
from app.models.application_profile import (
    ApplicationOwner,
    ApplicationPlaidItem,
    ApplicationProfile,
)
from app.models.bucket import Bucket, BucketActivityLog, BucketFile
from app.models.client import Client
from app.models.deal import Deal
from app.models.loan import Loan
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_profile import (
    ApplicationBankConnectionRead,
    ApplicationEvidenceRead,
    ApplicationProfileRead,
    EvidenceFileRead,
    EvidenceSourceRead,
    FileOwnerRead,
    FileOwnerRequirementState,
    UnifiedAuditEvent,
)
from app.scoping import scope_client_query, scope_loan_query

MAX_OWNERS = 5
CREDIT_THRESHOLD = Decimal("20.00")


def now() -> datetime:
    return datetime.now(UTC)


def normalized_email(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    return value or None


def normalized_phone(value: str | None) -> str | None:
    from app.dealer_os.services.consent_delivery import normalize_phone

    return normalize_phone(value)


def profile_read(profile: ApplicationProfile) -> ApplicationProfileRead:
    return ApplicationProfileRead(
        id=profile.id,
        client_id=profile.client_id,
        deal_id=profile.deal_id,
        loan_id=profile.loan_id,
        intake_id=profile.intake_id,
        dealer_id=profile.dealer_id,
        primary_bucket_id=profile.primary_bucket_id,
        vertical=profile.vertical,
        funding_category=profile.funding_category,
        entity_type=profile.entity_type,
        industry=profile.industry,
        naics_code=profile.naics_code,
        naics_label=profile.naics_label,
        custom_industry=profile.custom_industry,
        classification_revision=profile.classification_revision,
        classification_state=profile.classification_state,
        classified_at=profile.classified_at,
        backfill_needs_review=profile.backfill_needs_review,
        owner_storage="dealer" if profile.dealer_id else "application",
    )


async def _profile_is_visible(
    db: AsyncSession, profile: ApplicationProfile, user: User
) -> bool:
    if user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        return True
    if user.role == Role.VENDOR:
        return False
    if profile.dealer_id:
        if user.role in (Role.DEALER, Role.FIELD_REP):
            try:
                await resolve_dealer_scope(db, user, profile.dealer_id)
                return True
            except HTTPException:
                return False
        if user.role == Role.DEALER_PARTNER and profile.intake_id:
            intake_owner = (
                await db.execute(
                    select(PublicUnderwritingIntake.broker_id).where(
                        PublicUnderwritingIntake.id == profile.intake_id
                    )
                )
            ).scalar_one_or_none()
            return intake_owner == user.id
    if profile.loan_id:
        visible = (
            await db.execute(
                scope_loan_query(user, select(Loan.id).where(Loan.id == profile.loan_id))
            )
        ).scalar_one_or_none()
        if visible is not None:
            return True
    if profile.client_id:
        visible = (
            await db.execute(
                scope_client_query(
                    user, select(Client.id).where(Client.id == profile.client_id)
                )
            )
        ).scalar_one_or_none()
        if visible is not None:
            return True
    if profile.intake_id and user.role == Role.DEALER_PARTNER:
        owner = (
            await db.execute(
                select(PublicUnderwritingIntake.broker_id).where(
                    PublicUnderwritingIntake.id == profile.intake_id
                )
            )
        ).scalar_one_or_none()
        return owner == user.id
    return False


async def load_profile(
    db: AsyncSession, profile_id: UUID, user: User
) -> ApplicationProfile:
    profile = await db.get(ApplicationProfile, profile_id)
    if profile is None or not await _profile_is_visible(db, profile, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application file not found")
    return profile


async def _load_source(
    db: AsyncSession, source_kind: str, source_id: UUID, user: User
) -> Deal | Loan | PublicUnderwritingIntake | DealerBusiness:
    if source_kind == "deal":
        source = await db.get(Deal, source_id)
        if source:
            visible = (
                await db.execute(
                    scope_client_query(
                        user, select(Client.id).where(Client.id == source.client_id)
                    )
                )
            ).scalar_one_or_none()
            if visible is None:
                source = None
    elif source_kind == "loan":
        source = (
            await db.execute(
                scope_loan_query(user, select(Loan).where(Loan.id == source_id))
            )
        ).scalar_one_or_none()
    elif source_kind == "intake":
        source = await db.get(PublicUnderwritingIntake, source_id)
        if source is not None and user.role not in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
            allowed = False
            if user.role == Role.DEALER_PARTNER:
                allowed = source.broker_id == user.id
            elif source.client_id:
                allowed = (
                    await db.execute(
                        scope_client_query(
                            user, select(Client.id).where(Client.id == source.client_id)
                        )
                    )
                ).scalar_one_or_none() is not None
            if not allowed:
                source = None
    elif source_kind == "dealer":
        try:
            source = await resolve_dealer_scope(db, user, source_id)
        except HTTPException:
            source = None
    else:
        source = None
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application source not found")
    return source


def _vertical_for_intake(intake: PublicUnderwritingIntake) -> str:
    variant = (intake.variant or "").lower()
    if "mca" in variant:
        return "mca"
    if "dealer" in variant:
        return "dealer"
    if "real_estate" in variant or "funding_review" in variant:
        return "real_estate"
    return "main_street"


def _split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(None, 1)
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


async def resolve_profile(
    db: AsyncSession, source_kind: str, source_id: UUID, user: User
) -> ApplicationProfile:
    source = await _load_source(db, source_kind, source_id, user)
    if (
        isinstance(source, PublicUnderwritingIntake)
        and source.client_id is None
        and user.role in (Role.SUPER_ADMIN, Role.LOAN_EXEC)
    ):
        client = Client(
            name=source.full_name,
            email=normalized_email(source.email),
            phone=source.phone,
            stage="lead",
            referral_source=source.referral_source,
            source_channel="ai_intake",
            client_experience_mode="guided",
            client_experience_mode_reason="ai_intake",
            client_experience_mode_locked_by="firm",
        )
        db.add(client)
        await db.flush()
        source.client_id = client.id
    column = getattr(ApplicationProfile, f"{source_kind}_id")
    profile = (
        await db.execute(select(ApplicationProfile).where(column == source_id))
    ).scalar_one_or_none()

    if profile is None and isinstance(source, Loan):
        if source.source_deal_id:
            profile = (
                await db.execute(
                    select(ApplicationProfile).where(
                        ApplicationProfile.deal_id == source.source_deal_id
                    )
                )
            ).scalar_one_or_none()
        if profile is None and source.source_intake_id:
            profile = (
                await db.execute(
                    select(ApplicationProfile).where(
                        ApplicationProfile.intake_id == source.source_intake_id
                    )
                )
            ).scalar_one_or_none()
        if profile is not None and profile.loan_id is None:
            profile.loan_id = source.id
    elif profile is None and isinstance(source, PublicUnderwritingIntake):
        dealer = (
            await db.execute(
                select(DealerBusiness).where(
                    DealerBusiness.handoff_intake_id == source.id
                )
            )
        ).scalar_one_or_none()
        if dealer:
            profile = (
                await db.execute(
                    select(ApplicationProfile).where(
                        ApplicationProfile.dealer_id == dealer.id
                    )
                )
            ).scalar_one_or_none()
            if profile and profile.intake_id is None:
                profile.intake_id = source.id
    elif profile is None and isinstance(source, DealerBusiness) and source.handoff_intake_id:
        profile = (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.intake_id == source.handoff_intake_id
                )
            )
        ).scalar_one_or_none()
        if profile and profile.dealer_id is None:
            profile.dealer_id = source.id

    if profile is None:
        profile = ApplicationProfile()
        if isinstance(source, Deal):
            profile.deal_id = source.id
            profile.client_id = source.client_id
            profile.vertical = "real_estate"
            profile.funding_category = source.deal_type
        elif isinstance(source, Loan):
            profile.loan_id = source.id
            profile.client_id = source.client_id
            profile.vertical = "mca" if "mca" in str(source.type).lower() else "real_estate"
            profile.funding_category = str(source.type)
            profile.entity_type = str(source.entity_type) if source.entity_type else None
        elif isinstance(source, PublicUnderwritingIntake):
            profile.intake_id = source.id
            profile.client_id = source.client_id
            profile.primary_bucket_id = source.bucket_id
            profile.vertical = _vertical_for_intake(source)
            profile.funding_category = source.loan_purpose
            detail = (source.intake_state or {}).get("main_street_details") or {}
            profile.industry = detail.get("industry")
        else:
            profile.dealer_id = source.id
            profile.intake_id = source.handoff_intake_id
            profile.primary_bucket_id = source.bucket_id
            profile.vertical = "dealer"
            profile.funding_category = source.funding_purpose
            profile.entity_type = source.entity_type
            profile.industry = source.industry
            profile.naics_code = source.naics_code
            profile.naics_label = source.naics_label
        db.add(profile)
        await db.flush()

    if profile.primary_bucket_id is None:
        client = await db.get(Client, profile.client_id) if profile.client_id else None
        if isinstance(source, DealerBusiness):
            label = source.legal_name or source.name
        elif isinstance(source, PublicUnderwritingIntake):
            label = source.business_name or source.full_name
        else:
            label = client.name if client else "Application"
        bucket = Bucket(
            name=f"{label} evidence",
            bucket_type="application_profile",
            client_name=label,
            purpose="Application evidence and verification",
            status="collecting_documents",
            created_by_id=user.id,
        )
        db.add(bucket)
        await db.flush()
        profile.primary_bucket_id = bucket.id
        if isinstance(source, DealerBusiness):
            source.bucket_id = bucket.id

    if profile.dealer_id is None:
        count = int(
            (
                await db.execute(
                    select(func.count()).select_from(ApplicationOwner).where(
                        ApplicationOwner.profile_id == profile.id
                    )
                )
            ).scalar_one()
        )
        if count == 0:
            client = await db.get(Client, profile.client_id) if profile.client_id else None
            intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
            name = (intake.full_name if intake else None) or (client.name if client else "")
            if name:
                first, last = _split_name(name)
                db.add(
                    ApplicationOwner(
                        profile_id=profile.id,
                        first_name=first or "Owner",
                        last_name=last or "Unknown",
                        email=normalized_email((intake.email if intake else None) or (client.email if client else None)),
                        phone=(intake.phone if intake else None) or (client.phone if client else None),
                        ownership_pct=Decimal("100.00"),
                        is_primary=True,
                        backfill_needs_review=True,
                    )
                )
                profile.backfill_needs_review = True
    await db.flush()
    return profile


def owner_read(owner: ApplicationOwner | DealerOwner) -> FileOwnerRead:
    return FileOwnerRead(
        id=owner.id,
        full_name=owner.full_name,
        first_name=owner.first_name,
        last_name=owner.last_name,
        email=owner.email,
        phone=owner.phone,
        ownership_pct=float(owner.ownership_pct) if owner.ownership_pct is not None else None,
        is_primary=bool(owner.is_primary),
        is_guarantor=bool(owner.is_guarantor),
        dob=owner.dob,
        street=owner.street,
        city=owner.city,
        state=owner.state,
        zip=owner.zip,
        invite_sent_at=owner.invite_sent_at,
        invite_opened_at=owner.invite_opened_at,
        has_invite=owner.has_invite,
        credit_score=owner.credit_score,
        credit_tier=owner.credit_tier,
        credit_pulled_at=owner.credit_pulled_at,
        credit_required=owner.credit_required,
        credit_complete=owner.credit_complete,
        credit_contact_complete=owner.credit_contact_complete,
        backfill_needs_review=getattr(owner, "backfill_needs_review", False),
        source="dealer" if isinstance(owner, DealerOwner) else "application",
    )


async def owner_rows(db: AsyncSession, profile: ApplicationProfile) -> list[ApplicationOwner | DealerOwner]:
    model = DealerOwner if profile.dealer_id else ApplicationOwner
    predicate = (
        DealerOwner.dealer_id == profile.dealer_id
        if profile.dealer_id
        else ApplicationOwner.profile_id == profile.id
    )
    return list(
        (
            await db.execute(
                select(model).where(predicate).order_by(model.created_at.asc(), model.id.asc())
            )
        ).scalars().all()
    )


async def verification_state(
    db: AsyncSession, profile: ApplicationProfile
) -> FileOwnerRequirementState:
    owners = await owner_rows(db, profile)
    total = round(sum(float(owner.ownership_pct or 0) for owner in owners), 2)
    ownership_complete = bool(owners) and abs(total - 100.0) < 0.005
    required = [owner for owner in owners if owner.credit_required]
    missing_contact = [owner for owner in required if not owner.credit_contact_complete]
    pending = [owner for owner in required if not owner.credit_complete]
    banks = await bank_rows(db, profile)
    months = sorted({month for bank in banks for month in bank.statement_months})
    blockers: list[str] = []
    if not owners:
        blockers.append("Add at least one owner")
    if not ownership_complete:
        blockers.append(f"Ownership must total 100.00% (currently {total:.2f}%)")
    if missing_contact:
        blockers.append("Every 20%+ owner needs a personal email and valid phone")
    ready_step_2 = ownership_complete and not missing_contact
    if not banks:
        blockers.append("Connect at least one business bank")
    if pending:
        blockers.append(f"{len(pending)} required owner credit authorization(s) pending")
    return FileOwnerRequirementState(
        ownership_total=total,
        ownership_complete=ownership_complete,
        owner_contact_complete=not missing_contact,
        owner_count=len(owners),
        required_credit_owner_count=len(required),
        completed_credit_owner_count=len(required) - len(pending),
        pending_credit_owner_ids=[owner.id for owner in pending],
        missing_credit_contact_owner_ids=[owner.id for owner in missing_contact],
        bank_linked=bool(banks),
        bank_connection_count=len(banks),
        bank_statement_months=len(months),
        credit_returned=bool(required) and not pending,
        ready_for_step_2=ready_step_2,
        unlocked=ready_step_2 and bool(banks) and not pending,
        blockers=blockers,
    )


async def bank_rows(
    db: AsyncSession, profile: ApplicationProfile
) -> list[ApplicationBankConnectionRead]:
    if profile.dealer_id:
        items = list(
            (
                await db.execute(
                    select(DealerPlaidItem).where(
                        DealerPlaidItem.dealer_id == profile.dealer_id,
                        DealerPlaidItem.status != "removed",
                    ).order_by(DealerPlaidItem.created_at.asc())
                )
            ).scalars().all()
        )
        docs = list(
            (
                await db.execute(
                    select(DealerDocument.plaid_item_id, DealerDocument.extracted).where(
                        DealerDocument.dealer_id == profile.dealer_id,
                        DealerDocument.plaid_item_id.is_not(None),
                    )
                )
            ).all()
        )
        coverage: dict[UUID, set[str]] = {}
        for item_id, extracted in docs:
            for month in (extracted or {}).get("months") or []:
                value = month.get("month") if isinstance(month, dict) else month
                if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value):
                    coverage.setdefault(item_id, set()).add(value)
        return [
            ApplicationBankConnectionRead(
                id=item.id,
                institution_name=item.institution_name,
                accounts_label=item.accounts_label,
                status=item.status,
                error=item.error,
                auto_refresh=item.auto_refresh,
                is_primary_operating=item.is_primary_operating,
                last_pulled_at=item.last_pulled_at,
                next_refresh_at=item.next_refresh_at,
                statement_months=sorted(coverage.get(item.id, set())),
                source="dealer",
            )
            for item in items
        ]
    items = list(
        (
            await db.execute(
                select(ApplicationPlaidItem).where(
                    ApplicationPlaidItem.profile_id == profile.id,
                    ApplicationPlaidItem.status != "removed",
                ).order_by(ApplicationPlaidItem.created_at.asc())
            )
        ).scalars().all()
    )
    coverage_rows = list(
        (
            await db.execute(
                select(BucketFile.application_plaid_item_id, BucketFile.statement_period).where(
                    BucketFile.application_plaid_item_id.in_([item.id for item in items]) if items else False,
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
            )
        ).all()
    )
    coverage: dict[UUID, set[str]] = {}
    for item_id, period in coverage_rows:
        if item_id and period:
            coverage.setdefault(item_id, set()).add(period)
    return [
        ApplicationBankConnectionRead(
            id=item.id,
            institution_name=item.institution_name,
            accounts_label=item.accounts_label,
            status=item.status,
            error=item.error,
            auto_refresh=item.auto_refresh,
            is_primary_operating=item.is_primary_operating,
            last_pulled_at=item.last_pulled_at,
            next_refresh_at=item.next_refresh_at,
            statement_months=sorted(coverage.get(item.id, set())),
        )
        for item in items
    ]


async def log_profile_action(
    db: AsyncSession,
    profile: ApplicationProfile,
    user: User | None,
    action: str,
    detail: str,
    *,
    target_type: str | None = None,
    target_id: UUID | None = None,
    metadata: dict | None = None,
) -> None:
    if profile.dealer_id:
        from app.dealer_os.services.audit import log_action

        await log_action(
            db,
            profile.dealer_id,
            user,
            action[:48],
            (target_type or "profile")[:24],
            entity_id=target_id,
            after=metadata or {"detail": detail},
        )
    if profile.primary_bucket_id:
        db.add(
            BucketActivityLog(
                bucket_id=profile.primary_bucket_id,
                actor_user_id=user.id if user else None,
                actor_name=(user.name or user.email) if user else "system",
                actor_email=user.email if user else None,
                actor_role=str(user.role) if user else "system",
                action=action[:80],
                target_type=target_type,
                target_id=str(target_id) if target_id else None,
                detail=detail,
                created_at=now(),
            )
        )
        await db.flush()


async def evidence_state(
    db: AsyncSession, profile: ApplicationProfile
) -> ApplicationEvidenceRead:
    sources: list[EvidenceSourceRead] = []
    files: list[EvidenceFileRead] = []
    primary_id = profile.primary_bucket_id
    if primary_id:
        bucket = await db.get(Bucket, primary_id)
        primary_files = list(
            (
                await db.execute(
                    select(BucketFile).where(
                        BucketFile.bucket_id == primary_id,
                        BucketFile.status == "uploaded",
                        BucketFile.deleted_at.is_(None),
                    ).order_by(BucketFile.created_at.desc())
                )
            ).scalars().all()
        )
        source_id = f"bucket:{primary_id}"
        sources.append(
            EvidenceSourceRead(
                id=source_id,
                kind="bucket",
                relationship="primary",
                label=bucket.name if bucket else "Primary evidence bucket",
                bucket_id=primary_id,
                active_file_count=len(primary_files),
                selected_file_count=len(primary_files),
                accessible_file_count=len(primary_files),
            )
        )
        files.extend(
            EvidenceFileRead(
                id=file.id,
                source_id=source_id,
                bucket_id=file.bucket_id,
                file_name=file.file_name,
                content_type=file.content_type,
                size_bytes=file.size_bytes,
                created_at=file.created_at,
            )
            for file in primary_files
        )
    if profile.intake_id:
        links = list(
            (
                await db.execute(
                    select(BucketIntakeLink).where(
                        BucketIntakeLink.intake_id == profile.intake_id,
                        BucketIntakeLink.unlinked_at.is_(None),
                    ).order_by(BucketIntakeLink.created_at.asc())
                )
            ).scalars().all()
        )
        for link in links:
            bucket = await db.get(Bucket, link.bucket_id)
            rows = list(
                (
                    await db.execute(
                        select(BucketFile)
                        .join(BucketIntakeLinkFile, BucketIntakeLinkFile.bucket_file_id == BucketFile.id)
                        .where(
                            BucketIntakeLinkFile.link_id == link.id,
                            BucketIntakeLinkFile.removed_at.is_(None),
                            BucketFile.deleted_at.is_(None),
                            BucketFile.status == "uploaded",
                        )
                        .order_by(BucketFile.created_at.desc())
                    )
                ).scalars().all()
            )
            active_count = int(
                (
                    await db.execute(
                        select(func.count()).select_from(BucketFile).where(
                            BucketFile.bucket_id == link.bucket_id,
                            BucketFile.deleted_at.is_(None),
                            BucketFile.status == "uploaded",
                        )
                    )
                ).scalar_one()
            )
            source_id = f"link:{link.id}"
            sources.append(
                EvidenceSourceRead(
                    id=source_id,
                    kind="bucket_link",
                    relationship=link.relationship,
                    label=bucket.name if bucket else "Linked evidence bucket",
                    bucket_id=link.bucket_id,
                    active_file_count=active_count,
                    selected_file_count=len(rows),
                    accessible_file_count=len(rows),
                )
            )
            files.extend(
                EvidenceFileRead(
                    id=file.id,
                    source_id=source_id,
                    bucket_id=file.bucket_id,
                    file_name=file.file_name,
                    content_type=file.content_type,
                    size_bytes=file.size_bytes,
                    selected=True,
                    included_in_review=True,
                    created_at=file.created_at,
                )
                for file in rows
            )
    by_id = {file.id: file for file in files}
    blockers = [] if by_id else ["Add or link evidence before running AI review"]
    return ApplicationEvidenceRead(
        profile_id=profile.id,
        sources=sources,
        files=list(by_id.values()),
        total_files=len(by_id),
        review_file_count=len(by_id),
        blockers=blockers,
    )


async def audit_events(
    db: AsyncSession, profile: ApplicationProfile, limit: int = 250
) -> list[UnifiedAuditEvent]:
    events: list[UnifiedAuditEvent] = []
    if profile.primary_bucket_id:
        rows = list(
            (
                await db.execute(
                    select(BucketActivityLog).where(
                        BucketActivityLog.bucket_id == profile.primary_bucket_id
                    ).order_by(BucketActivityLog.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        )
        events.extend(
            UnifiedAuditEvent(
                id=f"bucket:{row.id}",
                occurred_at=row.created_at,
                action=row.action,
                summary=row.detail or row.action.replace("_", " "),
                actor_name=row.actor_name,
                actor_role=row.actor_role,
                source="evidence",
                metadata={"target_type": row.target_type, "target_id": row.target_id},
            )
            for row in rows
        )
    activity_filter = []
    if profile.loan_id:
        activity_filter.append(Activity.loan_id == profile.loan_id)
    if profile.client_id:
        activity_filter.append(Activity.client_id == profile.client_id)
    if activity_filter:
        rows = list(
            (
                await db.execute(
                    select(Activity).where(or_(*activity_filter)).order_by(Activity.occurred_at.desc()).limit(limit)
                )
            ).scalars().all()
        )
        events.extend(
            UnifiedAuditEvent(
                id=f"activity:{row.id}",
                occurred_at=row.occurred_at,
                action=row.kind,
                summary=row.summary,
                actor_role=row.actor_label,
                source="funding",
                metadata=row.payload or {},
            )
            for row in rows
        )
    if profile.dealer_id:
        rows = list(
            (
                await db.execute(
                    select(DealerAuditLog).where(
                        DealerAuditLog.dealer_id == profile.dealer_id
                    ).order_by(DealerAuditLog.created_at.desc()).limit(limit)
                )
            ).scalars().all()
        )
        events.extend(
            UnifiedAuditEvent(
                id=f"dealer:{row.id}",
                occurred_at=row.created_at,
                action=row.action,
                summary=row.action.replace(".", " "),
                actor_name=row.actor_name,
                source="dealer_os",
                metadata={"before": row.before, "after": row.after},
            )
            for row in rows
        )
    events.sort(key=lambda item: item.occurred_at, reverse=True)
    return events[:limit]
