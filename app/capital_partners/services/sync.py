"""What the marketplace carries against what the file says now.

A listing is cut from a file and then the file keeps moving: the desk
changes the plan, records terms, refreshes the package. The listing holds a
stamp of what was last pushed (the fingerprint of each section) so every
screen can say "published", "up to date" or "these changed since": the
figures, the plan, the proposed terms, the programs and the package PDFs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile

from ..models import CpListing

SECTION_LABELS: dict[str, str] = {
    "figures": "Financial figures",
    "plan": "The desk's plan",
    "terms": "Proposed terms",
    "programs": "Programs",
    "package": "Package PDFs",
}


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def proposed_terms(cockpit: dict[str, Any]) -> dict[str, Any] | None:
    """The terms the package and the listing speak of: the recorded term
    sheet first, else the loan in the desk's plan, with the schedule and the
    principal at risk (financial_cockpit.proposed_terms_block)."""
    from app.services import financial_cockpit

    if "proposed_terms" in cockpit:
        return cockpit.get("proposed_terms")
    return financial_cockpit.proposed_terms_block(cockpit)


def file_sections(
    cockpit: dict[str, Any], *, program_keys: list[str], package: dict[str, Any] | None
) -> dict[str, Any]:
    """The comparable sections of a file, free of timestamps."""
    from app.services import financial_cockpit

    figures = {
        k: v
        for k, v in financial_cockpit.listing_summary(cockpit).items()
        if k not in {"generated_at", "plan"}
    }
    plan = financial_cockpit.plan_summary(cockpit)
    return {
        "figures": figures,
        "plan": {
            "active": plan["active"],
            "modifications": plan["modifications"],
            "impact": plan["impact"],
            "note": plan["note"],
        },
        "terms": {
            "requested_amount": cockpit.get("requested_amount"),
            "proposed": proposed_terms(cockpit),
        },
        "programs": sorted(program_keys),
        "package": package,
    }


def fingerprints(sections: dict[str, Any]) -> dict[str, str]:
    return {key: _fingerprint(value) for key, value in sections.items()}


def sync_state(listing: CpListing, current: dict[str, str]) -> dict[str, Any]:
    """Published or not, up to date or not, and which sections moved since."""
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    published = snapshot.get("published") if isinstance(snapshot.get("published"), dict) else None
    stamped: dict[str, str] = dict((published or {}).get("fingerprints") or {})
    changed = [
        {"key": key, "label": SECTION_LABELS.get(key, key)}
        for key in SECTION_LABELS
        if key in current
        and stamped.get(key) != current[key]
        and not (key == "package" and current[key] == _fingerprint(None) and key not in stamped)
    ]
    return {
        "published": published is not None,
        "published_at": (published or {}).get("at"),
        "published_by": (published or {}).get("by"),
        "package": (published or {}).get("package"),
        "up_to_date": published is not None and not changed,
        "changed": changed,
        "listing_status": listing.status,
    }


def mark_published(
    listing: CpListing,
    current: dict[str, str],
    *,
    by: uuid.UUID | None,
    keys: list[str] | None = None,
    package: dict[str, Any] | None = None,
) -> None:
    """Stamp the listing with what was just pushed: every section, or the
    named ones, keeping the stamp of the rest."""
    snapshot = dict(listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {})
    previous = snapshot.get("published") if isinstance(snapshot.get("published"), dict) else {}
    stamped = dict((previous or {}).get("fingerprints") or {})
    for key in keys or list(current):
        if key in current:
            stamped[key] = current[key]
    snapshot["published"] = {
        "at": datetime.now(UTC).isoformat(),
        "by": str(by) if by else None,
        "fingerprints": stamped,
        "package": package if package is not None else (previous or {}).get("package"),
    }
    listing.source_snapshot = snapshot


async def current_package(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any] | None:
    """The file's current package pair, by generation, or None."""
    if not getattr(profile, "intake_id", None):
        return None
    from app.routers.dealer_ai_intake import _artifact_package_metadata, _latest_artifact

    summary = await _latest_artifact(db, profile.intake_id, "executive_summary")
    packet = await _latest_artifact(db, profile.intake_id, "lender_packet")
    if summary is None or packet is None:
        return None
    s_meta, p_meta = _artifact_package_metadata(summary), _artifact_package_metadata(packet)
    if not s_meta.get("generation_id") or s_meta.get("generation_id") != p_meta.get(
        "generation_id"
    ):
        return None
    return {
        "generation_id": str(s_meta.get("generation_id")),
        "summary_artifact_id": str(summary.id),
        "packet_artifact_id": str(packet.id),
    }


async def current_fingerprints(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """(fingerprints, sections, cockpit) for the file as it stands."""
    from app.services import application_programs as programs
    from app.services import financial_cockpit

    cockpit = await financial_cockpit.load_cockpit(db, profile)
    selections = await programs.active_selections(db, profile.id)
    sections = file_sections(
        cockpit,
        program_keys=[s.program_key for s in selections],
        package=await current_package(db, profile),
    )
    return fingerprints(sections), sections, cockpit


async def sync_for_listing(db: AsyncSession, listing: CpListing) -> dict[str, Any] | None:
    if listing.profile_id is None:
        return None
    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        return None
    current, _sections, _cockpit = await current_fingerprints(db, profile)
    return sync_state(listing, current)
