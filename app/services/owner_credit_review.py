"""Explicit staff decisions for imported owners and their already linked reports."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError

from app.dealer_os.models import DealerOwner
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import (
    ApplicationOwner,
    ApplicationProfile,
    ApplicationProgramSelection,
    ApplicationRequirementState,
)
from app.models.bucket import Bucket
from app.models.credit_pull import CreditPull
from app.models.funding_program import ApplicationEvidencePolicySelection
from app.services import application_profiles as profiles
from app.services import application_programs, credit_requirement_sync, requirement_lifecycle


async def lock_review_sources(db, profile):
    """Use readiness's bucket → profile order, then refresh the actual source rows."""
    if profile.primary_bucket_id is not None:
        await db.execute(select(Bucket.id).where(Bucket.id == profile.primary_bucket_id).with_for_update())
    (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
                     .with_for_update().execution_options(populate_existing=True))).scalar_one()
    model = DealerOwner if profile.dealer_id else ApplicationOwner
    predicate = DealerOwner.dealer_id == profile.dealer_id if profile.dealer_id else ApplicationOwner.profile_id == profile.id
    owner_query = select(model).where(predicate, *([] if profile.dealer_id else [ApplicationOwner.archived_at.is_(None)]))
    owner_query = owner_query.order_by(model.id).execution_options(populate_existing=True)
    owners = list((await db.execute(owner_query)).scalars().all())
    ids = [owner.credit_pull_id for owner in owners if owner.credit_pull_id]
    if ids:
        (await db.execute(select(CreditPull).where(CreditPull.id.in_(ids)).order_by(CreditPull.id)
                         .with_for_update().execution_options(populate_existing=True))).scalars().all()
    # Report importers lock a pull before updating its linked owners. Keep the
    # same pull → owner order, and reject a link changed outside the profile lock.
    owners = list((await db.execute(owner_query.with_for_update())).scalars().all())
    if {owner.credit_pull_id for owner in owners if owner.credit_pull_id}.difference(ids):
        raise HTTPException(409, "The linked owner reports changed. Reload the current evidence before reviewing.")
    return owners


async def confirm_owner(db, profile, owner_id, payload, user):
    owners = await lock_review_sources(db, profile)
    owner = next((row for row in owners if row.id == owner_id), None)
    if owner is None:
        raise HTTPException(404, "Owner not found")
    fingerprint = credit_requirement_sync.owner_review_fingerprint(owner)
    if fingerprint != payload.expected_fingerprint:
        raise HTTPException(409, "The owner details changed. Reload and review the current name, ownership and linked report before confirming.")
    blockers = credit_requirement_sync.owner_review_blockers(owner, owners)
    if blockers:
        raise HTTPException(422, " ".join(blockers))
    if not getattr(owner, "backfill_needs_review", False):
        await db.commit()
        return await profiles.owner_read_current(db, profile, owner)
    owner.backfill_needs_review = False
    await profiles.log_profile_action(db, profile, user, "owner.import_reviewed", f"Confirmed imported owner {owner.full_name}",
        target_type="owner", target_id=owner.id,
        metadata={"reason": payload.reason, "review_fingerprint": fingerprint, "credit_qualification_approved": False})
    await db.flush()
    await application_programs.get_program_readiness(db, profile)
    await db.commit()
    return await profiles.owner_read_current(db, profile, owner)


async def lock_credit_policy(db, profile):
    """Refresh and pin only the currently selected credit policy and its version."""
    selections = list((await db.execute(select(ApplicationProgramSelection).where(
        ApplicationProgramSelection.profile_id == profile.id, ApplicationProgramSelection.removed_at.is_(None),
    ).order_by(ApplicationProgramSelection.id).execution_options(populate_existing=True))).scalars().all())
    policies = list((await db.execute(select(ApplicationEvidencePolicySelection).where(
        ApplicationEvidencePolicySelection.profile_id == profile.id, ApplicationEvidencePolicySelection.replaced_at.is_(None),
    ).order_by(ApplicationEvidencePolicySelection.id).execution_options(populate_existing=True))).scalars().all())
    ids = {row.playbook_id for row in [*selections, *policies]}
    if ids:
        try:
            (await db.execute(select(AIPlaybookTemplate).where(AIPlaybookTemplate.id.in_(ids)).order_by(AIPlaybookTemplate.id)
                .with_for_update(nowait=True).execution_options(populate_existing=True))).scalars().all()
            (await db.execute(select(AICollectionRequirement).where(AICollectionRequirement.playbook_id.in_(ids), or_(
                AICollectionRequirement.requirement_key == credit_requirement_sync.REQUIREMENT_KEY,
                AICollectionRequirement.canonical_key == credit_requirement_sync.REQUIREMENT_KEY,
            )).order_by(AICollectionRequirement.id).with_for_update(nowait=True).execution_options(populate_existing=True))).scalars().all()
        except DBAPIError as exc:
            if (getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)) == "55P03":
                raise HTTPException(409, "The published credit policy is being updated. Reload before recording this review.") from exc
            raise


async def review_credit_requirement(db, profile, requirement_key, payload, user):
    await lock_review_sources(db, profile)
    readiness = await application_programs.get_program_readiness(db, profile)
    await lock_credit_policy(db, profile)
    # The first read may have auto-selected a program. Materialize again from
    # the consumed, refreshed policy rows while their locks are held.
    readiness = await application_programs.get_program_readiness(db, profile)
    resolved = requirement_lifecycle.resolve_requirement_key(readiness.requirements, requirement_key)
    if resolved is None:
        raise HTTPException(409, "This credit requirement is no longer active. Reload the current selected program criteria.")
    requirement_key = resolved
    state = (await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id,
        ApplicationRequirementState.requirement_key == requirement_key,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    snapshot = ((state.provenance or {}).get("credit_collection") if state is not None else None)
    if state is not None and (state.provenance or {}).get("inactive_definition"):
        raise HTTPException(409, "This credit requirement is no longer active. Reload the current selected program criteria.")
    if not isinstance(snapshot, dict) or snapshot.get("source") != "linked_owner_credit_pulls":
        raise HTTPException(409, "This requirement has no current linked-owner credit review. Reload the file and review its evidence links.")
    if state.status in {"waived", "not_applicable"}:
        raise HTTPException(409, "Restore this requirement before recording a credit review")
    if snapshot.get("fingerprint") != payload.expected_fingerprint:
        raise HTTPException(409, "The owners, linked report or selected program criteria changed. Reload and review the current credit evidence.")
    if payload.decision == "met" and not snapshot.get("can_mark_met"):
        raise HTTPException(422, " ".join(snapshot.get("review_blockers") or ["The linked report does not establish the published credit requirement"]))
    previous = (state.provenance or {}).get("credit_review")
    if isinstance(previous, dict) and all(previous.get(key) == value for key, value in {
        "fingerprint": payload.expected_fingerprint, "decision": payload.decision,
        "reason": payload.reason, "actor_user_id": str(user.id),
    }.items()):
        await db.commit()
        return readiness
    timestamp = datetime.now(UTC)
    review = {"decision": payload.decision, "reason": payload.reason, "reviewed_at": timestamp.isoformat(),
              "actor_user_id": str(user.id), "fingerprint": payload.expected_fingerprint}
    state.provenance = {**(state.provenance or {}), "credit_review": review}
    state.status = {"met": "verified", "incomplete": "received_unverified", "failed": "failed"}[payload.decision]
    state.state_reason = payload.reason
    state.verified_by_user_id = user.id if payload.decision == "met" else None
    state.verified_at = timestamp if payload.decision == "met" else None
    await profiles.log_profile_action(db, profile, user, f"requirement.credit_review.{payload.decision}",
        f"Reviewed linked owner credit: {payload.decision}", target_type="application_requirement", target_id=state.id,
        metadata={"requirement_key": requirement_key, **review})
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, profile)
    await db.commit()
    return readiness
