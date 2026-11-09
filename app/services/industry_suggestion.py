"""A NAICS suggestion from what the documents already said.

Program rules ask for a six-digit NAICS activity to check prohibited
industries. The file page already holds an extracted business activity
("health/wellness studio with online membership platform"), yet the
classification sat empty until a person typed it. The suggestion is made
once, from the catalog's own entries, with the model choosing among a short
list the words already narrowed; it is recorded as suggested, never as
confirmed, and a person's classification always wins.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import (
    ApplicationExtractedFact,
    ApplicationProfile,
    ApplicationTaxonomyEntry,
)

log = logging.getLogger(__name__)

ACTIVITY_FACT_KEYS = ("business_activity", "business_description", "industry", "industry_description")
SUGGESTION_SOURCE = "ai_activity_suggestion"
MIN_CONFIDENCE = 0.6
CANDIDATE_LIMIT = 20

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "and", "or", "the", "a", "an", "of", "for", "with", "in", "on", "to", "by", "at", "from", "as", "is",
    "are", "be", "its", "it", "that", "this", "which", "other", "all", "services", "service", "company",
    "business", "llc", "inc", "corp", "co", "ltd", "platform", "online", "studio", "shop", "store",
    "nec", "except", "including", "primarily", "engaged", "establishments", "industry",
})


def tokens(text: str | None) -> set[str]:
    return {word for word in _WORD.findall(str(text or "").casefold()) if word not in _STOP and len(word) > 2}


def rank_candidates(activity: str, entries: Iterable[ApplicationTaxonomyEntry], *, limit: int = CANDIDATE_LIMIT) -> list[ApplicationTaxonomyEntry]:
    """Catalog activities whose words overlap the description, best first."""
    wanted = tokens(activity)
    if not wanted:
        return []
    scored: list[tuple[float, str, ApplicationTaxonomyEntry]] = []
    for entry in entries:
        words = tokens(entry.label) | {word for alias in (entry.aliases or []) for word in tokens(str(alias))}
        overlap = len(wanted & words)
        if not overlap:
            continue
        scored.append((overlap / (len(words) ** 0.5 or 1), entry.label, entry))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [entry for _score, _label, entry in scored[:limit]]


async def _activity_snapshot(db: AsyncSession, profile: ApplicationProfile) -> tuple[str | None, str]:
    """Only a current, correctly associated source may describe this business."""
    from app.services import business_profile, document_associations

    rows = list((await db.execute(
        select(ApplicationExtractedFact)
        .where(ApplicationExtractedFact.profile_id == profile.id, ApplicationExtractedFact.field_key.in_(ACTIVITY_FACT_KEYS))
        .order_by(ApplicationExtractedFact.created_at.desc())
        .execution_options(populate_existing=True)
    )).scalars().all())
    owner = document_associations.owner_context(profile)
    files, analyses, associations = {}, {}, {}
    if any(getattr(row, "source_file_id", None) for row in rows):
        from app.services.application_programs import _evidence_inventory

        current_files, analyses, _ = await _evidence_inventory(db, profile, refresh=True)
        files = {file.id: file for file in current_files}
        associations = await document_associations.load_context(db, profile, current_files, analyses)
    for row in rows:
        if row.status in {"rejected"}:
            continue
        file_id, analysis_id = getattr(row, "source_file_id", None), getattr(row, "source_analysis_id", None)
        association = associations.get(str(file_id))
        if file_id:
            file, analysis = files.get(file_id), analyses.get(file_id)
            if (not file or not analysis or not file.content_hash
                    or analysis.status != "completed" or analysis.content_hash != file.content_hash
                    or not analysis_id or analysis.id != analysis_id
                    or not association or not association.get("allows_financial_use")):
                continue
        elif analysis_id or getattr(row, "extraction_method", None) == "document_ai":
            continue
        value = row.normalized_value or (row.value.get("value") if isinstance(row.value, dict) else None)
        if value and str(value).strip():
            text = str(value).strip()
            return text, document_associations.stamp({"owner": owner, "fact_id": str(getattr(row, "id", "")), "status": row.status, "text": text,
                "source_file_id": str(file_id) if file_id else None, "source_analysis_id": str(analysis_id) if analysis_id else None,
                "association_stamp": (association or {}).get("association_stamp")})
    # No document has described the business yet: use what the client said on
    # the first screen. A document's reading is more precise, so it goes first.
    if profile.intake_id:
        from app.models.public_underwriting_intake import PublicUnderwritingIntake
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if isinstance(db, AsyncSession) and intake is not None:
            await db.refresh(intake, attribute_names=["intake_state", "business_name"])
        declared = business_profile.of_intake(intake) if intake is not None else {}
        described = declared.get("description")
        industry = business_profile.MAIN_STREET_INDUSTRIES.get(declared.get("industry"), {}).get("en")
        if described or industry:
            text = " — ".join(value for value in (industry, described) if value)
            return text, document_associations.stamp({"owner": owner, "business_name": getattr(intake, "business_name", None), "declared": declared})
    return None, document_associations.stamp({"owner": owner, "activity": None})


async def _activity_text(db: AsyncSession, profile: ApplicationProfile) -> str | None:
    return (await _activity_snapshot(db, profile))[0]


def _classification_stamp(profile: ApplicationProfile) -> tuple:
    return tuple(str(getattr(profile, key, None) or "") for key in (
        "naics_code", "naics_label", "industry", "subindustry", "industry_entry_id", "subindustry_entry_id",
        "activity_entry_id", "classified_by_user_id", "classification_revision", "classification_provenance",
    ))


def _catalog_stamp(entry):
    return tuple(str(getattr(entry, key, None) or "") for key in ("id", "code", "label", "parent_id", "level", "status"))


async def _lock_current(db: AsyncSession, profile: ApplicationProfile) -> ApplicationProfile | None:
    with getattr(db, "no_autoflush", nullcontext()):
        return (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()


async def _current_activity_profile(db, profile, classification_stamp, activity, source_stamp):
    current = await _lock_current(db, profile)
    if current is None or current.naics_code or getattr(current, "classified_by_user_id", None) or _classification_stamp(current) != classification_stamp:
        return None
    current_activity, current_stamp = await _activity_snapshot(db, current)
    return current if current_activity == activity and current_stamp == source_stamp else None


def _mark(profile: ApplicationProfile, **fields: Any) -> None:
    previous = profile.classification_provenance if isinstance(profile.classification_provenance, dict) else {}
    profile.classification_provenance = {**previous, **fields, "suggestion_attempted_at": datetime.now(UTC).isoformat()}


async def suggest_for_profile(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any] | None:
    """Suggest and apply a NAICS activity for an unclassified profile, once.

    Returns the suggestion recorded (code, confidence, reason) or None when
    nothing could be suggested. Never overrides a person's classification.
    """
    from app.services.ai.bedrock_client import get_client, model_light
    from app.services.ai.usage import tracked_messages_create
    from app.services.application_profiles import _apply_extracted_taxonomy
    from app.services.bucket_ai import _json_or_fallback, _text_from_response

    if profile.naics_code or getattr(profile, "classified_by_user_id", None):
        return None
    classification_stamp = _classification_stamp(profile)
    activity, source_stamp = await _activity_snapshot(db, profile)
    if not activity:
        current = await _current_activity_profile(db, profile, classification_stamp, activity, source_stamp)
        if current is not None:
            _mark(current, suggestion_outcome="no_activity_fact")
        return None
    entries = list((await db.execute(
        select(ApplicationTaxonomyEntry).where(
            ApplicationTaxonomyEntry.level == 6,
            ApplicationTaxonomyEntry.status.in_(["official", "approved"]),
        )
    )).scalars().all())
    ranked = rank_candidates(activity, entries)
    if not ranked:
        current = await _current_activity_profile(db, profile, classification_stamp, activity, source_stamp)
        if current is not None:
            _mark(current, suggestion_outcome="no_candidates", activity=activity[:200])
        return None
    by_code = {str(entry.code): entry for entry in ranked if entry.code}
    candidate_stamps = {code: _catalog_stamp(entry) for code, entry in by_code.items()}
    prompt = {
        "business_activity": activity,
        "candidates": [{"code": entry.code, "label": entry.label} for entry in ranked],
        "instruction": (
            "Pick the single NAICS code from the candidates that best describes the business activity. "
            "Reply as JSON: {\"code\": \"######\" or null, \"confidence\": 0.0-1.0, \"reason\": \"one sentence\"}. "
            "Use null when no candidate fits."
        ),
    }
    try:
        response = await tracked_messages_create(
            db,
            feature="industry_suggestion",
            client=get_client(),
            model=model_light(),
            metadata={"profile_id": str(profile.id)},
            max_tokens=300,
            system="You classify businesses into NAICS activities. Choose only from the candidates given.",
            messages=[{"role": "user", "content": json.dumps(prompt)}],
        )
        parsed = _json_or_fallback(_text_from_response(response), "code")
    except Exception:  # noqa: BLE001 - a provider fault leaves the file unclassified, never misclassified
        log.exception("industry suggestion failed for profile %s", profile.id)
        current = await _current_activity_profile(db, profile, classification_stamp, activity, source_stamp)
        if current is not None:
            _mark(current, suggestion_outcome="provider_error", activity=activity[:200])
        return None
    code = str((parsed or {}).get("code") or "").strip()
    try:
        confidence = float((parsed or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if isinstance((parsed or {}).get("confidence"), bool) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        confidence = 0.0
    reason = str((parsed or {}).get("reason") or "")[:300]
    # The provider await releases control. A person may have classified the
    # file, changed its owner/activity, or denied/replaced its source meanwhile.
    # Refresh under the same row lock used by classification writes before
    # applying anything; do not even overwrite the staff provenance on a race.
    profile = await _current_activity_profile(db, profile, classification_stamp, activity, source_stamp)
    if profile is None:
        return None
    entry = by_code.get(code)
    if entry is None or confidence < MIN_CONFIDENCE:
        _mark(profile, suggestion_outcome="low_confidence", suggested_code=code or None, confidence=confidence, reason=reason, activity=activity[:200])
        return None
    entry = await db.get(ApplicationTaxonomyEntry, entry.id, populate_existing=True)
    if entry is None or _catalog_stamp(entry) != candidate_stamps.get(code):
        return None
    subindustry = await db.get(ApplicationTaxonomyEntry, entry.parent_id, populate_existing=True) if entry.parent_id else None
    industry = await db.get(ApplicationTaxonomyEntry, subindustry.parent_id, populate_existing=True) if subindustry and subindustry.parent_id else None
    profile = await _current_activity_profile(db, profile, classification_stamp, activity, source_stamp)
    if profile is None:
        return None
    if not _apply_extracted_taxonomy(profile, entry, subindustry, industry):
        _mark(profile, suggestion_outcome="not_applied", suggested_code=code, confidence=confidence, reason=reason, activity=activity[:200])
        return None
    _mark(profile, source=SUGGESTION_SOURCE, status="suggested", suggested_code=code, confidence=confidence, reason=reason, activity=activity[:200], suggestion_outcome="applied")
    await db.flush()
    return {"code": code, "label": entry.label, "confidence": confidence, "reason": reason}
