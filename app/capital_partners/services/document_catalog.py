"""Redacted inventory and explicit, scoped investor access to source originals."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_profile import ApplicationProfile

from .. import schemas as S
from ..models import CpListingDocumentGrant, CpMember
from . import listings as L
from . import members as M

CATEGORIES = {
    "bank_statement": "Bank statements",
    "business_tax_return": "Business tax returns",
    "profit_and_loss": "Profit and loss",
    "balance_sheet": "Balance sheets",
    "debt_schedule": "Debt schedules",
}
BANK_FIELDS = ("deposits", "withdrawals", "net")
TAX_FIELDS = (
    "gross_receipts",
    "income",
    "total_deductions",
    "gross_receipts_change_pct",
    "income_change_pct",
)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _year(value):
    text = str(value or "")
    return int(text) if re.fullmatch(r"(?:19|20)\d{2}", text) else None


def _currency(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def safe_document_summaries(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Allowlist aggregate numbers only; no identifiers, names or source refs."""
    source = value if isinstance(value, dict) else {}
    bank = source.get("bank_activity")
    tax = source.get("tax_years")
    if bank is None:
        bank = source["bank"].get("months") if isinstance(source.get("bank"), dict) else []
    if tax is None:
        tax = source["tax"].get("years") if isinstance(source.get("tax"), dict) else []
    bank_out, tax_out = [], []
    for row in bank if isinstance(bank, list) else []:
        if not isinstance(row, dict) or not re.fullmatch(
            r"(?:19|20)\d{2}-(?:0[1-9]|1[0-2])", str(row.get("month") or "")
        ):
            continue
        bank_out.append(
            {
                "month": row["month"],
                "currency": _currency(row.get("currency")),
                **{key: _number(row.get(key)) for key in BANK_FIELDS},
            }
        )
    for row in tax if isinstance(tax, list) else []:
        if not isinstance(row, dict) or (year := _year(row.get("year"))) is None:
            continue
        tax_out.append(
            {
                "year": year,
                "currency": _currency(row.get("currency")),
                **{key: _number(row.get(key)) for key in TAX_FIELDS},
            }
        )
    return {"bank_activity": bank_out[-24:], "tax_years": tax_out[-5:]}


def file_stamp(file) -> str:
    fields = {
        key: getattr(file, key, None)
        for key in ("id", "content_hash", "s3_version_id", "document_version_number")
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


async def source_inventory(db, listing):
    if not listing.profile_id:
        return []
    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None:
        return []
    from app.services import application_programs as programs
    from app.services import document_associations, locked_file_requests

    files, analyses, _classes = await programs._evidence_inventory(db, profile)
    accepted = await programs.accepted_evidence_file_ids(db, profile, files, analyses)
    identities = await document_associations.load_context(
        db, profile, files, analyses, accepted_ids=accepted
    )
    rows = []
    for file in files:
        analysis = analyses.get(file.id)
        category = getattr(analysis, "classification", None)
        if category not in CATEGORIES or getattr(analysis, "status", None) != "completed":
            continue
        facts = (
            ((analysis.analysis or {}).get("key_facts") or {})
            if isinstance(analysis.analysis, dict)
            else {}
        )
        period = str(getattr(file, "statement_period", None) or "")
        year = (
            _year(period[:4])
            if re.fullmatch(r"\d{4}-\d{2}", period)
            else _year(facts.get("tax_year") or facts.get("year"))
        )
        protected = locked_file_requests.is_password_protected_file(file, analysis)
        matched = (identities.get(str(file.id)) or {}).get("allows_financial_use") is True
        bound_bytes = bool(
            getattr(file, "content_hash", None) or getattr(file, "s3_version_id", None)
        )
        releasable = file.id in accepted and matched and bound_bytes and not protected
        rows.append(
            {
                "file": file,
                "category": category,
                "year": year,
                "releasable": releasable,
                "previewable": not protected,
            }
        )
    return rows


async def member_grant(db, listing_id, member_id):
    return (
        await db.execute(
            select(CpListingDocumentGrant)
            .where(
                CpListingDocumentGrant.listing_id == listing_id,
                CpListingDocumentGrant.member_id == member_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def grant_out(grant):
    return S.DocumentGrantOut(
        id=grant.id,
        listing_id=grant.listing_id,
        member_id=grant.member_id,
        status=grant.status,
        file_count=len(grant.file_ids or []),
        granted_by_id=grant.granted_by_id,
        granted_at=grant.granted_at,
        reason=grant.reason,
        revoked_by_id=grant.revoked_by_id,
        revoked_at=grant.revoked_at,
        revocation_reason=grant.revocation_reason,
    )


async def set_grant(db, listing, member_id: UUID, payload: S.DocumentGrantIn, actor_id: UUID):
    await db.refresh(listing, attribute_names=["status"], with_for_update=True)
    member = await db.get(CpMember, member_id)
    if member is None:
        raise HTTPException(404, "Investor not found.")
    grant = await member_grant(db, listing.id, member_id)
    now = datetime.now(UTC)
    if payload.status == "revoked":
        if grant is None:
            raise HTTPException(404, "No document release exists for this investor and listing.")
        grant.status, grant.revoked_by_id, grant.revoked_at, grant.revocation_reason = (
            "revoked",
            actor_id,
            now,
            payload.reason.strip(),
        )
    else:
        if not M.is_cleared(member):
            raise HTTPException(
                409,
                "Approve the investor and complete the waiting period before releasing sensitive originals.",
            )
        if listing.status in {"withdrawn", "repaid"}:
            raise HTTPException(409, "This listing is closed to new document releases.")
        inventory = await source_inventory(db, listing)
        eligible = {str(row["file"].id): row["file"] for row in inventory if row["releasable"]}
        selected = (
            {str(key) for key in payload.file_ids}
            if payload.file_ids is not None
            else set(eligible)
        )
        if not selected or not selected.issubset(eligible):
            raise HTTPException(
                422,
                "Select current, accepted business financial documents with confirmed identity and source bytes.",
            )
        if grant is None:
            grant = CpListingDocumentGrant(listing_id=listing.id, member_id=member_id)
            db.add(grant)
        grant.status, grant.file_ids = "active", sorted(selected)
        grant.file_stamps = {key: file_stamp(eligible[key]) for key in selected}
        grant.granted_by_id, grant.granted_at, grant.reason = actor_id, now, payload.reason.strip()
        grant.revoked_by_id = grant.revoked_at = grant.revocation_reason = None
    await db.flush()
    await L.log_event(
        db,
        listing.id,
        "source_documents.granted" if grant.status == "active" else "source_documents.revoked",
        actor_user_id=actor_id,
        detail={
            "grant_id": str(grant.id),
            "member_id": str(member_id),
            "file_ids": grant.file_ids,
            "file_stamps": grant.file_stamps,
            "reason": payload.reason.strip(),
            "engagement_confirmed": payload.engagement_confirmed,
        },
    )
    return grant


def released(row, grant) -> bool:
    file = row["file"]
    return bool(
        grant
        and grant.status == "active"
        and row["releasable"]
        and str(file.id) in (grant.file_ids or [])
        and (grant.file_stamps or {}).get(str(file.id)) == file_stamp(file)
    )


async def catalog(db, listing, *, member_id=None, staff=False):
    inventory = await source_inventory(db, listing)
    grant = (
        await member_grant(db, listing.id, member_id) if member_id and listing.profile_id else None
    )
    groups = defaultdict(lambda: {"file_count": 0, "releasable_count": 0})
    originals = []
    for row in inventory:
        counts = groups[(row["category"], row["year"])]
        counts["file_count"] += 1
        counts["releasable_count"] += int(row["releasable"])
        if not staff and not released(row, grant):
            continue
        file = row["file"]
        originals.append(
            {
                "file_id": str(file.id),
                "file_name": file.file_name,
                "category": row["category"],
                "year": row["year"],
                "version": file.document_version_number,
                "content_type": file.content_type,
                "size_bytes": file.size_bytes,
                "releasable": row["releasable"],
                "previewable": row["previewable"],
            }
        )
    snapshot = listing.source_snapshot if isinstance(listing.source_snapshot, dict) else {}
    financials = snapshot.get("financials") if isinstance(snapshot.get("financials"), dict) else {}
    return {
        "access": {
            "state": "staff_preview"
            if staff
            else "granted"
            if grant and grant.status == "active"
            else "locked"
        },
        "groups": [
            {"category": category, "label": CATEGORIES[category], "year": year, **counts}
            for (category, year), counts in sorted(
                groups.items(), key=lambda item: (item[0][0], item[0][1] or 0)
            )
        ],
        "originals": originals,
        "summaries": safe_document_summaries(financials.get("document_summaries")),
    }


async def authorized_original(db, listing, file_id, *, member_id=None, staff=False):
    grant = await member_grant(db, listing.id, member_id) if member_id else None
    for row in await source_inventory(db, listing):
        if row["file"].id == file_id:
            if (staff and row["previewable"]) or (not staff and released(row, grant)):
                return row["file"]
            break
    raise HTTPException(
        403,
        "This original has not been released to you for this listing, or its reviewed source changed.",
    )
