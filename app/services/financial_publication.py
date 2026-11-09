"""Refresh financial consumers without paying for another model interpretation.

Cache invalidation belongs to the publication transaction. Advisory metrics
run in savepoints so a policy/configuration problem cannot lose a saved PDF.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketAIReview, BucketFile
from app.models.financial_form_version import FinancialFormFamily, FinancialFormVersion
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services.loan_progress import loan_progress_fields

log = logging.getLogger(__name__)


def review_is_financially_stale(review: Any) -> bool:
    context = getattr(review, "context_snapshot", None)
    return isinstance(context, dict) and (context.get("financial_versions_stale") is True or context.get("funding_request_stale") is True or context.get("document_versions_stale") is True)


async def latest_publication_at(db: AsyncSession, profile_id: UUID) -> datetime | None:
    if not isinstance(getattr(db, "info", None), dict):
        return None
    return (await db.execute(select(func.max(FinancialFormVersion.created_at)).join(FinancialFormFamily, FinancialFormFamily.current_version_id == FinancialFormVersion.id).where(FinancialFormFamily.profile_id == profile_id))).scalar_one_or_none()


def snapshot_matches_publication(snapshot: Any, published_at: datetime | None) -> bool:
    if published_at is None:
        return True
    stamp = getattr(snapshot, "as_of", None) or getattr(snapshot, "created_at", None)
    if not isinstance(stamp, datetime):
        return False
    return stamp.replace(tzinfo=stamp.tzinfo or UTC) >= published_at.replace(tzinfo=published_at.tzinfo or UTC)


def debt_row_identity_context(body: dict, *, visible_file_ids: set[UUID] | None = None) -> list[dict]:
    """Stable associations and read-time balance reduction from the accepted row."""
    from app.services.debt_package_evidence import reviewed_loan_reference
    from app.services.financial_version_selection import active_debt_schedule_rows

    visible = {str(value) for value in visible_file_ids} if visible_file_ids is not None else None
    result = []
    has_metadata = False
    for ordinal, row in enumerate(active_debt_schedule_rows(body), 1):
        documents = []
        seen = set()
        links = row.get("evidence_links") if "evidence_links" in row else row.get("documents")
        for link in links or []:
            if not isinstance(link, dict) or link.get("role") not in {"agreement", "payoff", "supporting"}:
                continue
            try:
                file_id = str(UUID(str(link.get("file_id"))))
            except (ValueError, TypeError):
                continue
            key = (file_id, link["role"])
            if (visible is not None and file_id not in visible) or key in seen:
                continue
            seen.add(key)
            documents.append({"file_id": file_id, "role": link["role"]})
        reference = reviewed_loan_reference(row)
        progress = loan_progress_fields(row)
        has_progress = any(value is not None for value in progress.values())
        has_metadata |= bool(reference or documents or has_progress)
        result.append({"ordinal": ordinal, "row_id": str(row["id"]) if row.get("id") else None,
                       "lender": row.get("lender"), "loan_reference": reference, "documents": documents,
                       **(progress if has_progress else {})})
    # Avoid changing older contexts which contain only aggregate debt facts.
    return result if has_metadata else []


async def current_version_context(db: AsyncSession, *, profile_id: UUID | None = None, bucket_id: UUID | None = None, visible_file_ids: set[UUID] | None = None, client_safe: bool = False) -> list[dict]:
    """Only pointer-selected versions inside the caller's demonstrated scope."""
    if not isinstance(getattr(db, "info", None), dict):
        return []
    if profile_id is None and bucket_id is None and visible_file_ids is None:
        return []
    query = select(FinancialFormFamily, FinancialFormVersion).join(FinancialFormVersion, FinancialFormVersion.id == FinancialFormFamily.current_version_id)
    if profile_id is not None:
        query = query.where(FinancialFormFamily.profile_id == profile_id)
    if bucket_id is not None:
        query = query.where(FinancialFormFamily.profile_id.in_(select(ApplicationProfile.id).where(ApplicationProfile.primary_bucket_id == bucket_id)))
    if visible_file_ids is not None:
        if not visible_file_ids:
            return []
        query = query.where(func.coalesce(FinancialFormVersion.pdf_file_id, FinancialFormVersion.source_file_id).in_(visible_file_ids))
    if client_safe:
        # The shared business room does not prove personal-statement ownership.
        # Explicitly scoped staff/share views may include their selected PFS.
        query = query.where(FinancialFormFamily.kind.notin_(["pfs", "real_estate_schedule"]))
    rows = (await db.execute(query.order_by(FinancialFormVersion.created_at.desc()))).all()
    from app.services.financial_version_selection import (
        debt_schedule_completion,
        debt_schedule_monthly,
        is_accepted_debt_schedule_version,
    )

    rows = [(family, version) for family, version in rows
            if (visible_file_ids is None or (version.pdf_file_id or version.source_file_id) in visible_file_ids)
            and not (client_safe and family.kind in {"pfs", "real_estate_schedule"})]
    accepted = {}
    for family, version in rows:
        if family.kind == "debt_schedule" and is_accepted_debt_schedule_version(version):
            accepted.setdefault(getattr(family, "profile_id", None) or profile_id or family.id, version.id)
    context = []
    association_contexts = {}
    for family, version in rows:
        scope = getattr(family, "profile_id", None) or profile_id or family.id
        if family.kind == "debt_schedule" and scope in accepted and version.id != accepted[scope]:
            continue
        item = {"kind": family.kind, "family_id": str(family.id), "label": family.label, "version_id": str(version.id), "version_number": version.version_number, "saved_at": version.created_at.isoformat(), "source_file_id": str(version.pdf_file_id or version.source_file_id), "key_facts": version.key_facts}
        item["original_source_file_id"] = str(version.source_file_id) if version.source_file_id else None
        if family.kind == "debt_schedule":
            item["authoritative"] = is_accepted_debt_schedule_version(version)
            item["completion"] = debt_schedule_completion(getattr(version, "body", None))
            if item["authoritative"]:
                monthly = debt_schedule_monthly(version.body)
                item["debt_service_for_dscr"] = float(monthly) if monthly is not None else None
                item["business_name"] = version.body.get("business_name") or (version.key_facts or {}).get("business_name")
                item["currency"] = version.body.get("currency") or (version.key_facts or {}).get("currency")
                item["source"] = {"kind": "accepted_financial_version", "file_id": item["source_file_id"], "version_id": item["version_id"], "version_number": version.version_number}
                identities = debt_row_identity_context(version.body, visible_file_ids=visible_file_ids)
                if identities:
                    item["debt_rows"] = identities
        if isinstance(db, AsyncSession) and getattr(family, "profile_id", None):
            from app.services import document_associations

            if family.profile_id not in association_contexts:
                profile = await db.get(ApplicationProfile, family.profile_id)
                sources = {source_id for source_family, source_version in rows if source_family.profile_id == family.profile_id
                           for source_id in (source_version.pdf_file_id, source_version.source_file_id) if source_id}
                association_contexts[family.profile_id] = await document_associations.additional_context(db, profile, sources) if profile is not None and any(
                    event.get("identity_file_id") for event in (profile.financial_metric_review or {}).get("events", [])) else {}
            item = document_associations.version_projection(item, association_contexts[family.profile_id])
        context.append(item)
    return context


async def invalidate_financial_review_caches(db: AsyncSession, profile: ApplicationProfile, version: FinancialFormVersion | None = None, *, correction_revision: int | None = None) -> None:
    if profile.primary_bucket_id is None:
        return
    from app.services.financial_calculation_policy import CALCULATION_POLICY_VERSION

    marker = {"financial_versions_stale": True, "financial_calculation_policy_version": CALCULATION_POLICY_VERSION}
    if version is not None:
        marker.update(financial_version_updated_at=datetime.now(UTC).isoformat(), financial_version_id=str(version.id))
    else:
        assert correction_revision is not None
        marker.update(financial_calculation_updated_at=datetime.now(UTC).isoformat(), financial_calculation_revision=correction_revision)
    await db.execute(update(BucketAIReview).where(BucketAIReview.bucket_id == profile.primary_bucket_id).values(context_snapshot=func.coalesce(BucketAIReview.context_snapshot, literal({}, type_=JSONB)).op("||")(literal(marker, type_=JSONB))).execution_options(synchronize_session=False))
    # The intake's copy otherwise bypasses latest_review's stale filter. Clear
    # the relationship too, including a lead already loaded by a legacy route.
    intakes = list((await db.execute(select(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id == profile.primary_bucket_id))).scalars().all())
    for intake in intakes:
        intake.latest_review_id = None
        intake.latest_review = None
        intake.result_snapshot = None
        intake.intake_state = {**(intake.intake_state or {}), **marker}
    # Existing ORM review instances in the caller's identity map must agree
    # with the SQL update before the response is serialized.
    for row in getattr(db, "identity_map", {}).values():
        if isinstance(row, BucketAIReview) and row.bucket_id == profile.primary_bucket_id:
            row.context_snapshot = {**(row.context_snapshot or {}), **marker}
    await db.flush()


async def refresh_advisory_projections(db: AsyncSession, profile: ApplicationProfile, *, version_id: UUID | None = None, event_key: str | None = None) -> None:
    from app.services import application_programs, capital_readiness

    profile_id = profile.id
    dealer_id = profile.dealer_id
    assert version_id is not None or event_key
    event_key = event_key or f"financial-version:{version_id}"
    try:
        async with db.begin_nested():
            await application_programs.get_program_readiness(db, profile)
    except Exception:  # noqa: BLE001 - accepted version remains authoritative
        log.exception("Financial requirement refresh failed profile=%s version=%s", profile_id, version_id)
        await db.refresh(profile)
    await capital_readiness.advisory_recompute_profiles(db, [profile], event_key=event_key)
    if dealer_id:
        from app.dealer_os.services.engines import recompute_snapshot

        try:
            async with db.begin_nested():
                await recompute_snapshot(db, dealer_id)
        except Exception:  # noqa: BLE001
            log.exception("Financial dealer refresh failed profile=%s version=%s", profile_id, version_id)
            await db.refresh(profile)


async def record_staff_debt_publication_review(db: AsyncSession, profile: ApplicationProfile, *, family: FinancialFormFamily, version: FinancialFormVersion, file: BucketFile, actor: Any = None) -> None:
    """An explicit staff save is a content-bound human evidence review.

    Use the same append-only decision hook as an underwriting override. Never
    infer human verification from an uploaded original, borrower, or AI draft.
    This runs before advisory refresh, in the publication transaction.
    """
    from app.enums import Role
    from app.services.financial_version_selection import is_accepted_debt_schedule_version

    staff_roles = {Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.BROKER, Role.LOAN_EXEC, Role.FIELD_REP}
    actor_id = getattr(actor, "id", None)
    if not (family.kind == "debt_schedule" and actor_id is not None and getattr(actor, "role", None) in staff_roles
            and getattr(version, "source", None) == "form" and is_accepted_debt_schedule_version(version)
            and getattr(version, "actor_user_id", None) == actor_id
            and getattr(family, "current_version_id", None) == version.id
            and getattr(version, "pdf_file_id", None) == file.id):
        return
    from app.models.application_profile import (
        ApplicationRequirementEvidence,
        ApplicationRequirementState,
    )
    from app.schemas.application_profile import EvidenceDecisionOverride
    from app.services import application_programs

    # First-use requirements need their canonical evidence links before the
    # standard staff decision can bind this immutable generated PDF to them.
    await application_programs.get_program_readiness(db, profile)
    states = (await db.execute(select(ApplicationRequirementState).join(
        ApplicationRequirementEvidence,
        ApplicationRequirementEvidence.requirement_state_id == ApplicationRequirementState.id,
    ).where(ApplicationRequirementState.profile_id == profile.id,
            ApplicationRequirementEvidence.file_id == file.id,
            ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all()
    for state in states:
        await application_programs.override_evidence_decision(
            db, profile=profile, requirement_key=state.requirement_key, file_id=file.id,
            payload=EvidenceDecisionOverride(decision="accepted", reason_code="other", confirmed=True,
                reason=f"Staff reviewed and explicitly saved the complete business debt schedule as immutable version {version.version_number} ({version.id})."),
            user=actor,
        )


async def after_publication(db: AsyncSession, profile: ApplicationProfile, *, family: FinancialFormFamily, version: FinancialFormVersion, file: BucketFile, actor: Any = None, submit: bool = False) -> None:
    if family.kind == "real_estate_schedule":
        from app.services.real_estate_mortgage_requests import sync_published_mortgage_requests

        await sync_published_mortgage_requests(db, profile=profile, family=family, version=version, actor=actor)
    await invalidate_financial_review_caches(db, profile, version)
    await record_staff_debt_publication_review(db, profile, family=family, version=version, file=file, actor=actor)
    loan_id = getattr(profile, "loan_id", None)
    if loan_id:
        from app.services.activity_log import mark_loan_dirty

        await mark_loan_dirty(db, loan_id)
    try:
        async with db.begin_nested():
            await refresh_advisory_projections(db, profile, version_id=version.id)
    except Exception:  # noqa: BLE001 - a saved version cannot depend on advisory caches
        log.exception("Financial advisory refresh failed version=%s", version.id)
        try:
            await db.refresh(profile)
        except Exception:  # noqa: BLE001
            log.exception("Could not refresh financial profile after advisory failure")
    from app.services import file_events

    await file_events.emit(db, profile=profile, kind="document.received" if submit else "financial_form.version_saved", visibility=file_events.VISIBILITY_CLIENT if submit else file_events.VISIBILITY_DESK, title=f"{family.label} v{version.version_number} {'filed' if submit else 'saved'}", actor=actor, target_type="financial_version", target_id=version.id, meta={"kind": family.kind, "family_id": str(family.id), "version_number": version.version_number, "file_id": str(file.id), "submitted": submit})
