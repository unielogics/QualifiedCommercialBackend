"""Canonical NAICS hierarchy validation shared by Field Desk workflows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationTaxonomyEntry


async def canonicalize_selection(
    db: AsyncSession,
    values: dict[str, Any],
    *,
    required: bool = True,
) -> dict[str, Any]:
    """Resolve browser-supplied IDs and return server-owned labels/codes."""

    raw_ids = (
        values.get("industry_entry_id"),
        values.get("subindustry_entry_id"),
        values.get("activity_entry_id"),
    )
    if not any(raw_ids):
        if required:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Select the complete NAICS category, subcategory, and business activity.",
            )
        return {"taxonomy_status": "unclassified"}
    if not all(raw_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Select the complete NAICS category, subcategory, and business activity.",
        )
    try:
        entry_ids = tuple(UUID(str(value)) for value in raw_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "One or more NAICS selections are invalid.",
        ) from exc

    rows = list(
        (
            await db.execute(
                select(ApplicationTaxonomyEntry).where(
                    ApplicationTaxonomyEntry.id.in_(entry_ids)
                )
            )
        ).scalars().all()
    )
    by_id = {row.id: row for row in rows}
    industry, subindustry, activity = (by_id.get(entry_id) for entry_id in entry_ids)
    if not industry or not subindustry or not activity:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "One or more NAICS selections are no longer available.",
        )
    if (
        industry.level != 2
        or subindustry.level != 3
        or activity.level != 6
        or subindustry.parent_id != industry.id
        or activity.parent_id != subindustry.id
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Select a valid 2-digit, 3-digit, and 6-digit NAICS hierarchy.",
        )
    statuses = {industry.status, subindustry.status, activity.status}
    if not statuses.issubset({"official", "approved", "pending"}):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The selected NAICS classification is not available for screening.",
        )
    return {
        "industry_entry_id": industry.id,
        "industry": industry.code or "other",
        "industry_label": industry.label,
        "subindustry_entry_id": subindustry.id,
        "subindustry": subindustry.code,
        "subindustry_label": subindustry.label,
        "activity_entry_id": activity.id,
        "naics_code": activity.code,
        "naics_label": activity.label,
        "taxonomy_status": "pending" if "pending" in statuses else "official",
    }
