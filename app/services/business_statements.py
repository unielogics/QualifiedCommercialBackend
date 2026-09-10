"""Saving a profit-and-loss statement or a balance sheet, and filing it.

The two business statements follow the PFS pattern — a JSONB body with derived
columns written on every save, a hashed link that opens the form, a PDF filed
on the checklist at submit — with one difference: a link of either kind
carries no statement id and resolves the latest row of its kind on the file,
because there is one live row per (profile, kind).

**"Uploaded" means recognised, not "the slot has a file".** Main Street asks
for the P&L and the balance sheet on one checklist row, so a slot with a file
in it says nothing about which of the two arrived. The router asks the
extractors instead; this module only knows how to find and create the slot.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile, BucketRequestedDocument
from app.models.business_financial_statement import BusinessFinancialStatement
from app.services import business_statement_schema as bss
from app.services import dealer_forms_pdf, drafted_forms, file_events
from app.services.bucket_evidence import classifications_for_requested_doc

log = logging.getLogger(__name__)

#: Where a slot created here goes on the checklist — the same category as
#: the tax returns and the combined Main Street row.
SLOT_CATEGORY = "Financials"

_RENDERERS = {
    "p_and_l": lambda body: dealer_forms_pdf.render_p_and_l_pdf(body=body),
    "balance_sheet": lambda body: dealer_forms_pdf.render_balance_sheet_pdf(body=body),
}


def _kind_or_raise(kind: str) -> bss.StatementSchema:
    if kind not in bss.SCHEMA_FOR:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown form")
    return bss.SCHEMA_FOR[kind]


def _iso(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


async def latest_for_profile(
    db: AsyncSession, profile_id: UUID, kind: str
) -> BusinessFinancialStatement | None:
    """The live row of this kind on the file. Newest wins."""
    return (
        await db.execute(
            select(BusinessFinancialStatement)
            .where(
                BusinessFinancialStatement.profile_id == profile_id,
                BusinessFinancialStatement.kind == kind,
            )
            .order_by(BusinessFinancialStatement.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def save(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    kind: str,
    body: dict[str, Any],
    status: str = "draft",
    actor_user_id: UUID | None = None,
    statement: BusinessFinancialStatement | None = None,
) -> BusinessFinancialStatement:
    """Create or update the file's statement of this kind.

    Derived columns come from `totals()` on every save so the panel and the
    extractors never re-walk JSONB. Status moves draft → submitted and stays
    submitted on later edits — the PFS PATCH rule — and `submitted_by_user_id`
    is set once, when it becomes submitted, to whoever did it: null for a
    borrower on their own link.
    """
    schema = _kind_or_raise(kind)
    if statement is None:
        statement = await latest_for_profile(db, profile.id, kind)
    if statement is None:
        statement = BusinessFinancialStatement(
            profile_id=profile.id, kind=kind, created_by_user_id=actor_user_id
        )
        db.add(statement)

    body = dict(body or {})
    body.setdefault("schema_version", schema.schema_version)
    totals = schema.totals(body)
    header = body.get("header") or {}

    statement.body = body
    statement.schema_version = str(body.get("schema_version") or schema.schema_version)[:16]
    statement.notes = str(body.get("notes") or "").strip() or None
    if kind == "p_and_l":
        statement.period_start = _iso(header.get("period_start"))
        statement.period_end = _iso(header.get("period_end"))
        statement.gross_revenue = bss._amount((body.get("sections") or {}).get("revenue", {}).get("gross_revenue"))
        statement.net_income = totals["net_income"]
        statement.ebitda = totals["ebitda"]
    else:
        statement.as_of_date = _iso(header.get("as_of_date"))
        statement.total_assets = totals["total_assets"]
        statement.total_liabilities = totals["total_liabilities"]
        statement.total_equity = totals["total_equity"]

    if status == "submitted" or statement.status == "submitted":
        if statement.submitted_at is None:
            statement.submitted_at = datetime.now(UTC)
            statement.submitted_by_user_id = actor_user_id
        statement.status = "submitted"
    else:
        statement.status = "draft"
    await db.flush()
    return statement


def seed_business_name(body: dict[str, Any], prefill: dict[str, Any]) -> dict[str, Any]:
    """Fill the business name from the file, without ever overwriting. Blanks
    only: a borrower who corrected it must not find it reverted."""
    header = dict(body.get("header") or {})
    if not str(header.get("business_name") or "").strip() and prefill.get("business_name"):
        header["business_name"] = prefill["business_name"]
    return {**body, "header": header}


async def body_for_profile(
    db: AsyncSession, profile: ApplicationProfile, kind: str, prefill: dict[str, Any] | None = None
) -> tuple[dict[str, Any], BusinessFinancialStatement | None]:
    """The latest body of this kind, or a seeded empty one, plus the row."""
    schema = _kind_or_raise(kind)
    statement = await latest_for_profile(db, profile.id, kind)
    body = dict(statement.body or {}) if statement else schema.empty_body()
    if not body:
        body = schema.empty_body()
    return seed_business_name(body, prefill or {}), statement


async def _bucket_slots(db: AsyncSession, bucket_id: UUID) -> list[BucketRequestedDocument]:
    return list(
        (
            await db.execute(
                select(BucketRequestedDocument)
                .where(BucketRequestedDocument.bucket_id == bucket_id)
                .order_by(BucketRequestedDocument.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


def _slot_matches(slot: BucketRequestedDocument, kind: str) -> tuple[bool, bool]:
    """(dedicated, combined): whether this checklist row asks for exactly this
    document, or for it among others (the Main Street "P&L and balance sheet"
    row). A row named exactly as our form is dedicated even before the
    evidence vocabulary knows the word."""
    schema = bss.SCHEMA_FOR[kind]
    classes = classifications_for_requested_doc(slot.name or "", slot.category)
    named = (slot.name or "").strip().casefold() == schema.label.casefold()
    if named or classes == {schema.classification}:
        return True, True
    return False, schema.classification in classes


async def slot_for_kind(
    db: AsyncSession, profile: ApplicationProfile, kind: str
) -> BucketRequestedDocument | None:
    """The requested document this form files into, or None.

    Prefers a row that asks only for this document over the combined Main
    Street row, so a file with both never files the balance sheet under the
    P&L's own row. None without a document room.
    """
    _kind_or_raise(kind)
    if profile.primary_bucket_id is None:
        return None
    combined: BucketRequestedDocument | None = None
    for slot in await _bucket_slots(db, profile.primary_bucket_id):
        dedicated, matches = _slot_matches(slot, kind)
        if dedicated:
            return slot
        if matches and combined is None:
            combined = slot
    return combined


async def ensure_slot(
    db: AsyncSession, profile: ApplicationProfile, kind: str, *, required: bool
) -> BucketRequestedDocument:
    """The slot, created if the checklist has no row for this document.

    Idempotent. 409s without a document room, in the words the PFS uses, so a
    borrower on a link is told the same thing the desk would be.
    """
    schema = _kind_or_raise(kind)
    if profile.primary_bucket_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This file has no document room to file the statement in"
        )
    existing = await slot_for_kind(db, profile, kind)
    if existing is not None:
        return existing
    slot = BucketRequestedDocument(
        bucket_id=profile.primary_bucket_id,
        name=schema.label,
        category=SLOT_CATEGORY,
        description="Fill this in online or upload your own — either satisfies the request.",
        required=required,
        status="requested",
    )
    db.add(slot)
    await db.flush()
    return slot


def file_label(kind: str, body: dict[str, Any]) -> str:
    """"Profit and loss statement · Jan–Jun 2026" — the period is on the label
    so two PDFs on the one Main Street row are telling apart in the room."""
    label = bss.SCHEMA_FOR[kind].label
    period = bss.period_label(kind, body)
    return f"{label} · {period}" if period else label


async def file_pdf(
    db: AsyncSession,
    profile: ApplicationProfile,
    statement: BusinessFinancialStatement,
    *,
    slot: BucketRequestedDocument,
    actor_user_id: UUID | None,
    actor_name: str,
    actor_email: str,
    actor: Any = None,
) -> BucketFile:
    """Render the statement, file it on the slot, and tell the timeline.

    One function for both ways a statement gets filed — the borrower pressing
    Save on their own link and the desk completing one for them. `actor_user_id`
    is null for a borrower, and the summary says which it was.
    """
    kind = statement.kind
    schema = _kind_or_raise(kind)
    body = statement.body or {}
    label = file_label(kind, body)
    summary = (
        f"{label} submitted by the borrower through their own link."
        if actor_user_id is None
        else f"{label} completed by {actor_name} on the borrower's behalf."
    )
    stored = await drafted_forms.store_form_pdf(
        db,
        bucket_id=profile.primary_bucket_id,
        upload_link_id=None,
        requested_document=slot,
        pdf_bytes=_RENDERERS[kind](body),
        file_label=label,
        classification=schema.classification,
        key_facts=schema.key_facts(body),
        actor_name=actor_name,
        actor_email=actor_email,
        summary=summary,
    )
    statement.bucket_file_id = stored.id
    await file_events.emit(
        db,
        profile=profile,
        kind="document.received",
        visibility=file_events.VISIBILITY_CLIENT,
        title=f"{schema.label} was submitted",
        actor=actor,
        target_type="business_financial_statement",
        target_id=statement.id,
        meta={"kind": kind},
    )
    return stored
