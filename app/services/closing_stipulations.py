"""Stipulations to close: what must be in hand before funding.

The client accepts an offer on the Underwriting tab (the accepted amount is
the acceptance); from then on the desk keeps the list of stipulations: the
conditions on the accepted terms seed it, the desk adds its own, and each
one is open, received or waived. The estimated close date sits beside the
list. The list lives on the profile as JSON; nothing is deleted, a
stipulation the desk no longer wants is waived with a note.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile

STATUSES: tuple[str, ...] = ("open", "received", "waived")
SOURCES: tuple[str, ...] = ("term_sheet", "desk")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_item(
    raw: dict[str, Any], *, by: str | None, existing: dict[str, Any] | None
) -> dict[str, Any] | None:
    """One stipulation as stored: a stable id, the text, where it came from,
    its state and who last touched it. None when there is no text."""
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    status = str(raw.get("status") or "open")
    if status not in STATUSES:
        status = "open"
    source = str(raw.get("source") or (existing or {}).get("source") or "desk")
    if source not in SOURCES:
        source = "desk"
    note = str(raw.get("note") or "").strip() or None
    changed = (
        existing is None
        or existing.get("text") != text
        or existing.get("status") != status
        or existing.get("note") != note
    )
    return {
        "id": str(raw.get("id") or (existing or {}).get("id") or uuid.uuid4()),
        "text": text,
        "source": source,
        "status": status,
        "note": note,
        "updated_at": _now() if changed else (existing or {}).get("updated_at") or _now(),
        "updated_by": by if changed else (existing or {}).get("updated_by"),
        "created_at": (existing or {}).get("created_at") or _now(),
    }


def merge_items(
    current: list[dict[str, Any]], incoming: list[dict[str, Any]], *, by: str | None
) -> list[dict[str, Any]]:
    """The list as the desk sent it, keeping each existing item's history by
    id; an item left out is kept as waived rather than dropped, so nothing the
    terms asked for disappears silently."""
    by_id = {
        str(item.get("id")): item for item in current if isinstance(item, dict) and item.get("id")
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        existing = by_id.get(str(raw.get("id") or ""))
        item = normalize_item(raw, by=by, existing=existing)
        if item is None:
            continue
        seen.add(item["id"])
        out.append(item)
    for item_id, item in by_id.items():
        if item_id not in seen:
            kept = dict(item)
            if kept.get("status") != "waived":
                kept.update({"status": "waived", "updated_at": _now(), "updated_by": by})
            out.append(kept)
    return out


def seed_items(
    current: list[dict[str, Any]], conditions: list[str], *, by: str | None
) -> list[dict[str, Any]]:
    """The terms' conditions added as open stipulations, once each; the ones
    already listed (same words) are left as they are."""
    have = {
        str(item.get("text") or "").strip().lower() for item in current if isinstance(item, dict)
    }
    out = [item for item in current if isinstance(item, dict)]
    for text in conditions:
        clean = str(text or "").strip()
        if not clean or clean.lower() in have:
            continue
        have.add(clean.lower())
        out.append(
            normalize_item(
                {"text": clean, "source": "term_sheet", "status": "open"}, by=by, existing=None
            )
        )
    return out


def conditions_text_lines(value: Any) -> list[str]:
    """Conditions as a list of lines, whether stored as a list or as text."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str):
        return [line.strip(" -•\t") for line in value.splitlines() if line.strip(" -•\t")]
    return []


async def terms_conditions(db: AsyncSession, profile: ApplicationProfile) -> list[str]:
    """The conditions on the terms in force: the dealer's production term
    sheet when there is one, else the current client terms."""
    try:
        from app.services.production_term_sheets import (
            current_term_sheet as current_production_sheet,
        )

        sheet = await current_production_sheet(db, profile.id)
    except Exception:  # noqa: BLE001 - one source among two
        sheet = None
    if sheet is not None:
        return conditions_text_lines(getattr(sheet, "conditions", None))
    try:
        from app.services.application_terms import current_term_sheet as current_client_terms

        row = await current_client_terms(db, profile.id)
    except Exception:  # noqa: BLE001
        row = None
    return conditions_text_lines(getattr(row, "conditions", None)) if row is not None else []


def read_block(profile: ApplicationProfile, conditions: list[str]) -> dict[str, Any]:
    items = [
        item
        for item in (getattr(profile, "closing_stipulations", None) or [])
        if isinstance(item, dict)
    ]
    accepted_amount = getattr(profile, "underwriting_accepted_amount", None)
    close_date = getattr(profile, "estimated_close_date", None)
    open_count = sum(1 for i in items if i.get("status") == "open")
    return {
        "profile_id": str(profile.id),
        "accepted": accepted_amount is not None,
        "accepted_amount": float(accepted_amount) if accepted_amount is not None else None,
        "estimated_close_date": close_date.isoformat() if close_date else None,
        "items": items,
        "open_count": open_count,
        "received_count": sum(1 for i in items if i.get("status") == "received"),
        "waived_count": sum(1 for i in items if i.get("status") == "waived"),
        "terms_conditions": conditions,
        "ready_to_close": accepted_amount is not None and bool(items) and open_count == 0,
    }


async def read(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    return read_block(profile, await terms_conditions(db, profile))


async def write(
    db: AsyncSession, profile: ApplicationProfile, items: list[dict[str, Any]], *, by: str | None
) -> dict[str, Any]:
    profile.closing_stipulations = merge_items(
        list(profile.closing_stipulations or []), items, by=by
    )
    await db.flush()
    return await read(db, profile)


async def seed(db: AsyncSession, profile: ApplicationProfile, *, by: str | None) -> dict[str, Any]:
    conditions = await terms_conditions(db, profile)
    profile.closing_stipulations = seed_items(
        list(profile.closing_stipulations or []), conditions, by=by
    )
    await db.flush()
    return read_block(profile, conditions)
