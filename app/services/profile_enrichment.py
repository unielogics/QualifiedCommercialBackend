"""Fill in what the AI can fill in, on a timer, so a person only corrects.

Two things used to wait for a click. The grounded document checks ran only
when the desk pressed "Run AI review", so a requirement with real lender
checks sat at "received" after every upload. And an unclassified file stayed
unclassified although the documents had already said what the business
does. Both now run on their own, bounded per tick so the provider is never
flooded; a person's classification or verification always wins.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile, ApplicationRequirementState

log = logging.getLogger(__name__)


async def suggest_industries(db: AsyncSession, *, limit: int = 5) -> int:
    from app.services.industry_suggestion import suggest_for_profile

    profiles = list((await db.execute(
        select(ApplicationProfile)
        .where(ApplicationProfile.naics_code.is_(None), ApplicationProfile.classified_by_user_id.is_(None))
        .order_by(ApplicationProfile.updated_at.desc())
        .limit(60)
    )).scalars().all())
    done = 0
    for profile in profiles:
        provenance = profile.classification_provenance if isinstance(profile.classification_provenance, dict) else {}
        if provenance.get("suggestion_attempted_at"):
            continue
        try:
            await suggest_for_profile(db, profile)
            await db.commit()
        except Exception:  # noqa: BLE001 - one file must not stop the pass
            log.exception("industry suggestion pass failed for profile %s", profile.id)
            await db.rollback()
        done += 1
        if done >= limit:
            break
    return done


async def review_pending_checks(db: AsyncSession, *, limit: int = 3) -> int:
    """Run the grounded document checks for requirements that are waiting on them."""
    from app.services.document_qualification_review import review_profile_checks

    states = list((await db.execute(
        select(ApplicationRequirementState)
        .where(
            ApplicationRequirementState.status == "received_unverified",
            ApplicationRequirementState.provenance["review_checks_status"].astext == "pending",
        )
        .order_by(ApplicationRequirementState.updated_at.asc())
        .limit(40)
    )).scalars().all())
    by_profile: dict[UUID, list[str]] = defaultdict(list)
    for state in states:
        coverage = (state.provenance or {}).get("coverage") if isinstance(state.provenance, dict) else None
        if not isinstance(coverage, dict) or not coverage.get("complete"):
            continue
        by_profile[state.profile_id].append(state.requirement_key)
    done = 0
    for profile_id, keys in by_profile.items():
        profile = await db.get(ApplicationProfile, profile_id)
        if profile is None:
            continue
        try:
            await review_profile_checks(db, profile, requirement_keys=keys)
            await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("document check review pass failed for profile %s", profile_id)
            await db.rollback()
        done += 1
        if done >= limit:
            break
    return done


async def enrich_profiles(db: AsyncSession) -> dict[str, int]:
    return {"industries": await suggest_industries(db), "checks": await review_pending_checks(db)}
