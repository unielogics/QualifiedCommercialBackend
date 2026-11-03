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
import re
from collections.abc import Iterable
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


async def _activity_text(db: AsyncSession, profile: ApplicationProfile) -> str | None:
    rows = list((await db.execute(
        select(ApplicationExtractedFact)
        .where(ApplicationExtractedFact.profile_id == profile.id, ApplicationExtractedFact.field_key.in_(ACTIVITY_FACT_KEYS))
        .order_by(ApplicationExtractedFact.created_at.desc())
    )).scalars().all())
    for row in rows:
        if row.status in {"rejected"}:
            continue
        value = row.normalized_value or (row.value.get("value") if isinstance(row.value, dict) else None)
        if value and str(value).strip():
            return str(value).strip()
    return None


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
    activity = await _activity_text(db, profile)
    if not activity:
        _mark(profile, suggestion_outcome="no_activity_fact")
        return None
    entries = list((await db.execute(
        select(ApplicationTaxonomyEntry).where(
            ApplicationTaxonomyEntry.level == 6,
            ApplicationTaxonomyEntry.status.in_(["official", "approved"]),
        )
    )).scalars().all())
    ranked = rank_candidates(activity, entries)
    if not ranked:
        _mark(profile, suggestion_outcome="no_candidates", activity=activity[:200])
        return None
    by_code = {str(entry.code): entry for entry in ranked if entry.code}
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
        _mark(profile, suggestion_outcome="provider_error", activity=activity[:200])
        return None
    code = str((parsed or {}).get("code") or "").strip()
    try:
        confidence = float((parsed or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str((parsed or {}).get("reason") or "")[:300]
    entry = by_code.get(code)
    if entry is None or confidence < MIN_CONFIDENCE:
        _mark(profile, suggestion_outcome="low_confidence", suggested_code=code or None, confidence=confidence, reason=reason, activity=activity[:200])
        return None
    subindustry = await db.get(ApplicationTaxonomyEntry, entry.parent_id) if entry.parent_id else None
    industry = await db.get(ApplicationTaxonomyEntry, subindustry.parent_id) if subindustry and subindustry.parent_id else None
    if not _apply_extracted_taxonomy(profile, entry, subindustry, industry):
        _mark(profile, suggestion_outcome="not_applied", suggested_code=code, confidence=confidence, reason=reason, activity=activity[:200])
        return None
    _mark(profile, source=SUGGESTION_SOURCE, status="suggested", suggested_code=code, confidence=confidence, reason=reason, activity=activity[:200], suggestion_outcome="applied")
    await db.flush()
    return {"code": code, "label": entry.label, "confidence": confidence, "reason": reason}
