"""Current requested funding: a reviewed correction, never issued loan terms.

The profile is canonical. The bounded intake/bucket mirrors support existing
synchronous readers; neither original submissions nor accepted terms are edited.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketAIReview
from app.models.funding_request import FundingRequestChange
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.funding_request import FundingRequestPatch, FundingRequestRead
from app.services.financial_snapshot_objects import snapshot_object, sql_mark_snapshot

EDIT_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def require_editor(user: User) -> None:
    if user.role not in EDIT_ROLES:
        raise HTTPException(403, "Only a super admin or underwriter can change the requested amount")


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 and result == result.quantize(Decimal("0.01")) else None
    except (InvalidOperation, ValueError):
        return None


def current_for_profile(profile: Any) -> Decimal | None:
    state = getattr(profile, "current_funding_request", None)
    return _amount(state.get("requested_amount")) if isinstance(state, dict) else None


def current_for_intake_snapshot(intake: Any) -> Decimal | None:
    state = getattr(intake, "intake_state", None) or {}
    current = state.get("current_funding_request") if isinstance(state, dict) else None
    return _amount(current.get("requested_amount")) if isinstance(current, dict) else None


def intake_requested_amount(intake: Any) -> Decimal | None:
    return current_for_intake_snapshot(intake) or _amount(getattr(intake, "requested_loan_amount", None))


def accept_initial_amount_fact(intake: Any, value: Any, *, source: str) -> bool:
    """Chat can supply a missing initial amount; corrections require review."""
    proposed = _amount(value)
    current = intake_requested_amount(intake)
    if proposed is None:
        return False
    if current is None:
        return True
    if proposed != current:
        intake.intake_state = {
            **(getattr(intake, "intake_state", None) or {}),
            "requested_amount_change_proposal": {
                "requested_amount": str(proposed), "source": source,
                "proposed_at": datetime.now(UTC).isoformat(), "requires_underwriter_review": True,
            },
        }
    return False


def require_initial_or_unchanged_amount(intake: Any, value: Any) -> None:
    current = intake_requested_amount(intake)
    if current is not None and _amount(value) != current:
        raise HTTPException(403, "Change the submitted requested amount through the underwriter funding-request controls with a complete allocation and reason")


def project_request_context(context: dict, current: dict | None) -> dict:
    """Overlay reviewed request facts without altering historical source data."""
    amount = _amount((current or {}).get("requested_amount"))
    if amount is None:
        return dict(context)
    bounded = {
        "requested_amount": str(amount), "revision": int((current or {}).get("revision", 0)),
        "payoff_only_alternative": bool((current or {}).get("payoff_only_alternative", False)),
    }
    projected = {**context, "current_funding_request": bounded, "requested_loan_amount": float(amount)}
    for key, amount_key in (
        ("dealer_details", "requested_loan_amount"), ("funding_review_details", "requested_amount"),
        ("funding_review_basics", "requested_amount"), ("main_street_details", "requested_amount"),
        ("mca_details", "requested_amount"), ("mca_qualification", "requested_amount"),
        ("equipment_financing_details", "requested_amount"),
    ):
        if isinstance(context.get(key), dict):
            projected[key] = {**context[key], amount_key: float(amount)}
    return projected


async def lock_review_revision(db: AsyncSession, profile: ApplicationProfile, revision: int, budget_revision: int | None = None) -> bool:
    # A completed model call can leave the review row dirty. Take the same
    # profile-first lock as corrections before flushing that review row.
    with getattr(db, "no_autoflush", nullcontext()):
        current = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one()
    return int((current.current_funding_request or {}).get("revision", 0)) == revision and (budget_revision is None or (current.use_of_funds_revision or 0) == budget_revision)


async def invalidate_request_reviews(db: AsyncSession, profile: ApplicationProfile) -> None:
    """Preserve old review evidence while withdrawing superseded decisions."""
    if not getattr(profile, "primary_bucket_id", None):
        return
    marker = {
        "funding_request_stale": True,
        "funding_request_revision": int((getattr(profile, "current_funding_request", None) or {}).get("revision", 0)),
        "use_of_funds_revision": profile.use_of_funds_revision or 0,
    }
    await db.execute(update(BucketAIReview).where(BucketAIReview.bucket_id == profile.primary_bucket_id).values(
        context_snapshot=sql_mark_snapshot(BucketAIReview.context_snapshot, marker),
    ).execution_options(synchronize_session=False))
    await db.execute(update(PublicUnderwritingIntake).where(PublicUnderwritingIntake.bucket_id == profile.primary_bucket_id).values(
        latest_review_id=None,
        result_snapshot=sql_mark_snapshot(PublicUnderwritingIntake.result_snapshot, {"_funding_request_stale": True}),
    ).execution_options(synchronize_session=False))
    for row in getattr(db, "identity_map", {}).values():
        if isinstance(row, BucketAIReview) and row.bucket_id == profile.primary_bucket_id:
            row.context_snapshot = {**snapshot_object(row.context_snapshot), **marker}
        elif isinstance(row, PublicUnderwritingIntake) and row.bucket_id == profile.primary_bucket_id:
            row.latest_review_id = None
            row.latest_review = None
            row.result_snapshot = {**snapshot_object(row.result_snapshot), "_funding_request_stale": True}


async def current_for_intake(db: AsyncSession, intake: Any) -> Decimal | None:
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id))).scalar_one_or_none()
    return current_for_profile(profile) if profile is not None else None


def requires_exact_allocation(profile: Any) -> bool:
    # Existing unsubmitted drafts may remain partial; a submitted financial file
    # and every reviewed correction must balance before it can be saved.
    return current_for_profile(profile) is not None or not bool(getattr(profile, "is_draft", False))


def validate_allocation(amount: Decimal | None, items: list, *, exact: bool) -> Decimal:
    total = sum((item.amount for item in items), Decimal(0))
    if amount is not None and ((exact and total != amount) or total > amount):
        detail = "Use-of-funds total must equal the requested amount exactly" if exact else "Use-of-funds total cannot exceed the requested amount"
        raise HTTPException(422, detail)
    return total


async def read_request(db: AsyncSession, profile: ApplicationProfile, *, user: User | None = None) -> FundingRequestRead:
    from app.services.use_of_funds import read_budget

    budget = await read_budget(db, profile)
    current = getattr(profile, "current_funding_request", None) or {}
    original = current.get("original") or {}
    can_edit = user is not None and user.role in EDIT_ROLES
    return FundingRequestRead(
        profile_id=profile.id, requested_amount=budget.requested_amount,
        requested_amount_source=budget.requested_amount_source,
        original_requested_amount=float(original["requested_amount"]) if original.get("requested_amount") is not None else budget.requested_amount,
        revision=int(current.get("revision", 0)), use_of_funds_revision=budget.revision,
        items=budget.items, complete=budget.complete, can_edit=can_edit,
        reason=current.get("reason") if can_edit else None, updated_at=current.get("updated_at"),
        updated_by_user_id=current.get("updated_by_user_id") if can_edit else None,
        payoff_only_alternative=bool(current.get("payoff_only_alternative", False)),
    )


def payload_hash(payload: FundingRequestPatch) -> str:
    # Expected revisions are transport concurrency guards, not request identity.
    body = payload.model_dump(mode="json", exclude={"expected_revision", "expected_use_of_funds_revision", "idempotency_key"})
    body["requested_amount"] = str(payload.requested_amount.quantize(Decimal("0.01")))
    for row, item in zip(body["items"], payload.items, strict=True):
        row["amount"] = str(item.amount.quantize(Decimal("0.01")))
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def update_request(
    db: AsyncSession, profile: ApplicationProfile, payload: FundingRequestPatch, user: User,
) -> FundingRequestRead:
    require_editor(user)
    validate_allocation(payload.requested_amount, payload.items, exact=True)
    locked = (await db.execute(
        select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    fingerprint = payload_hash(payload)
    receipt = (await db.execute(select(FundingRequestChange).where(
        FundingRequestChange.profile_id == locked.id,
        FundingRequestChange.idempotency_key == payload.idempotency_key,
    ))).scalar_one_or_none()
    if receipt is not None:
        if receipt.payload_hash != fingerprint:
            raise HTTPException(409, "This retry key was already used for a different funding request")
        return await read_request(db, locked, user=user)
    before = dict(getattr(locked, "current_funding_request", None) or {})
    revision = int(before.get("revision", 0))
    if revision != payload.expected_revision or (locked.use_of_funds_revision or 0) != payload.expected_use_of_funds_revision:
        raise HTTPException(409, "The funding request or use of funds changed; reload before saving")

    from app.services import use_of_funds
    from app.services.application_profiles import log_profile_action

    original_amount, original_source, purpose, _dealer = await use_of_funds.source_funding_data(db, locked)
    original = before.get("original") or {
        "requested_amount": str(original_amount) if original_amount is not None else None,
        "source": original_source, "purpose": purpose,
        "use_of_funds": list(locked.use_of_funds or []),
    }
    now = datetime.now(UTC)
    current = {
        "requested_amount": str(payload.requested_amount), "revision": revision + 1,
        "reason": payload.reason, "updated_at": now.isoformat(), "updated_by_user_id": str(user.id),
        "original": original, "payoff_only_alternative": payload.payoff_only_alternative,
    }
    locked.current_funding_request = current
    locked.use_of_funds = [item.model_dump(mode="json") for item in payload.items]
    locked.use_of_funds_revision = (locked.use_of_funds_revision or 0) + 1
    locked.use_of_funds_updated_at = now
    locked.use_of_funds_updated_by_user_id = user.id
    snapshot = {**current, "items": locked.use_of_funds, "use_of_funds_revision": locked.use_of_funds_revision}
    db.add(FundingRequestChange(
        profile_id=locked.id, revision=revision + 1,
        idempotency_key=payload.idempotency_key, payload_hash=fingerprint, snapshot=snapshot,
    ))
    # Mirrors contain only current request facts. Original submission columns,
    # accepted evidence, financial versions and issued loan terms stay intact.
    mirror = {key: current[key] for key in ("requested_amount", "revision", "payoff_only_alternative")}
    if locked.intake_id:
        intake = (await db.execute(select(PublicUnderwritingIntake).where(
            PublicUnderwritingIntake.id == locked.intake_id,
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if intake is not None:
            state = {**(intake.intake_state or {}), "current_funding_request": mirror}
            state.pop("loan_program_fit", None)
            state.pop("loan_program_fit_request_revision", None)
            intake.intake_state = state
            # Historical review rows remain available; their old decision is no
            # longer presented as the current request's underwriting result.
            intake.latest_review_id = None
            intake.latest_review = None
            if intake.result_snapshot:
                intake.result_snapshot = {**intake.result_snapshot, "_funding_request_stale": True}
    if locked.primary_bucket_id:
        bucket = (await db.execute(select(Bucket).where(Bucket.id == locked.primary_bucket_id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if bucket is not None:
            budget = use_of_funds.summarize(locked, payload.requested_amount, "profile.current_funding_request")
            bucket.ai_context = {
                **(bucket.ai_context or {}), "current_funding_request": mirror,
                "requested_loan_amount": float(payload.requested_amount),
                "use_of_funds": use_of_funds.ai_context(budget),
            }
            bucket.ai_context = {key: value for key, value in bucket.ai_context.items() if key not in {"loan_program_fit", "program_fit"}}
        await invalidate_request_reviews(db, locked)
    await log_profile_action(
        db, locked, user, "funding_request.correct", "Corrected the current requested amount and complete use-of-funds budget",
        metadata={"before": before, "after": snapshot, "reason": payload.reason, "idempotency_key": str(payload.idempotency_key)},
    )
    await db.flush()
    return await read_request(db, locked, user=user)
