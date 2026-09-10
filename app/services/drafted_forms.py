"""Filing a form we generated as though the borrower had uploaded it.

A form typed on screen satisfies a checklist item the same way a real upload
does: the PDF becomes an ordinary `BucketFile` linked by `requested_document_id`,
which alone flips the requested document to `uploaded`, and a `BucketFileAnalysis`
is written straight from the structured input rather than asking a model to read
numbers back out of a picture we just drew. Everything downstream reads
`classification` and `key_facts` and never asks where they came from.

That behaviour already existed inside the public intake router. It lives here so
the staff side can file a statement the same way without a second copy of the
rules — the two must agree about what satisfies a slot, and the surest way to
keep them agreeing is for there to be one of them.

Deliberately no audit log and no commit. The actor differs (a borrower on a
public link, a staff member filling one in on their behalf) and so does the
transaction the write belongs to, so both stay with the caller.

`store_form_pdf` is the submit path and files a new document. `refresh_saved_form`
is the save path: it keeps that one document's PDF and its analysis current so
the AI reads today's figures rather than the ones typed on the day it was first
filed. A save never satisfies the checklist row — only a submit does.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION

log = logging.getLogger(__name__)


async def store_form_pdf(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    upload_link_id: UUID | None,
    requested_document: BucketRequestedDocument,
    pdf_bytes: bytes,
    file_label: str,
    classification: str,
    key_facts: dict[str, Any],
    actor_name: str,
    actor_email: str,
    summary: str | None = None,
) -> BucketFile:
    """Store the PDF against the slot and record what it says.

    Flushes so the caller has the file's id; does not commit.
    """
    from app.routers.buckets import _bucket_storage_config
    from app.routers.dealer_ai_intake import _put_bucket_object

    _, prefix, _kms = _bucket_storage_config()
    file_id = uuid4()
    s3_key = f"{prefix}/drafted-forms/{bucket_id}/{file_id}.pdf"
    _put_bucket_object(s3_key, "application/pdf", pdf_bytes)

    result_file = BucketFile(
        id=file_id,
        bucket_id=bucket_id,
        requested_document_id=requested_document.id,
        upload_link_id=upload_link_id,
        file_name=f"{file_label}.pdf"[:255],
        s3_key=s3_key,
        content_type="application/pdf",
        size_bytes=len(pdf_bytes),
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploaded",
    )
    db.add(result_file)
    requested_document.status = "uploaded"
    await db.flush()

    db.add(
        BucketFileAnalysis(
            bucket_file_id=result_file.id,
            bucket_id=bucket_id,
            content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
            analysis_version=CURRENT_FILE_ANALYSIS_VERSION,
            provider="drafted_form",
            status="completed",
            classification=classification,
            confidence="high",
            summary=summary or f"{file_label} submitted via the on-screen drafting form.",
            analysis={"key_facts": key_facts},
            analyzed_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return result_file


# ---------------------------------------------------------------------------
# Keeping a filed form current.
#
# `store_form_pdf` above is the *submit* path: it files a new document and
# satisfies the checklist row. Everything below is the *save* path. A form is
# typed over days, and the figures the AI reads are the ones in the analysis
# row — so a save that leaves the last PDF and the last key_facts in place
# leaves the desk reading last week's numbers.
#
# The rule that keeps this from turning the room into a pile of near-identical
# PDFs: one drafted-form document per (bucket, checklist row, classification).
# A save overwrites that document's S3 object and its analysis in place — same
# file id, same key, new bytes, new key_facts, new content_hash. Accumulating
# instead would also make `_slot_analyses`'s "newest analysis per file"
# meaningless, because every file would have exactly one and there would be
# dozens of files.
# ---------------------------------------------------------------------------

#: The analysis provider that marks a document as one of ours rather than one a
#: model read. Matching on it is what stops a refresh from overwriting a PDF the
#: borrower actually uploaded.
DRAFTED_FORM_PROVIDER = "drafted_form"

#: The four forms this module can refresh. Anything else — a worksheet link,
#: say — is not a form with a PDF behind it and is a no-op rather than an error.
_KINDS = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")


async def _existing_drafted_form(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    requested_document_id: UUID,
    classification: str,
) -> tuple[BucketFile, BucketFileAnalysis] | None:
    """The drafted-form document already filed on this slot for this form.

    Keyed on the classification as well as the slot because Main Street asks
    for the P&L and the balance sheet on one checklist row: two documents, one
    slot, and a refresh of either must not overwrite the other.

    A soft-deleted document is not a candidate — somebody removed it on
    purpose, and reviving it under the same id would undo that. The next save
    files a fresh one instead.
    """
    row = (
        await db.execute(
            select(BucketFile, BucketFileAnalysis)
            .join(BucketFileAnalysis, BucketFileAnalysis.bucket_file_id == BucketFile.id)
            .where(
                BucketFile.bucket_id == bucket_id,
                BucketFile.requested_document_id == requested_document_id,
                BucketFile.deleted_at.is_(None),
                BucketFileAnalysis.provider == DRAFTED_FORM_PROVIDER,
                BucketFileAnalysis.classification == classification,
            )
            .order_by(BucketFile.created_at.desc(), BucketFileAnalysis.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], row[1]


async def refresh_form_pdf(
    db: AsyncSession,
    *,
    bucket_id: UUID,
    requested_document: BucketRequestedDocument,
    pdf_bytes: bytes,
    file_label: str,
    classification: str,
    key_facts: dict[str, Any],
    upload_link_id: UUID | None = None,
    actor_name: str | None = None,
    actor_email: str | None = None,
    summary: str | None = None,
    mark_uploaded: bool = False,
) -> BucketFile:
    """Put today's figures behind this form's document, without adding one.

    Finds the drafted-form document for (bucket, requested_document,
    classification) and overwrites it: the same `BucketFile` row, the same S3
    key, new bytes, and the same `BucketFileAnalysis` row carrying the new
    `key_facts` and a new `content_hash`. One is created only when none exists,
    on the same key shape `store_form_pdf` uses. Flushes; does not commit.

    **A draft save must not flip the checklist row.** `mark_uploaded` defaults
    to False and only a submit passes True. "Filled in" and "uploaded" are
    different states the desk reads: a half-typed form has current figures for
    the AI to read and has still not satisfied the requirement. Nothing here
    ever moves the row back either — a refresh after a submit leaves
    `uploaded` alone.

    The analysis is rewritten rather than appended for the same reason the file
    is: `_slot_analyses` takes the newest analysis per file, so a second row
    would be dead weight, and its unique key is
    (file, content_hash, version) — the same row with a new hash is exactly
    what the constraint is shaped for.
    """
    from app.routers.buckets import _bucket_storage_config
    from app.routers.dealer_ai_intake import _put_bucket_object

    _, prefix, _kms = _bucket_storage_config()
    existing = await _existing_drafted_form(
        db,
        bucket_id=bucket_id,
        requested_document_id=requested_document.id,
        classification=classification,
    )
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    summary = summary or f"{file_label} as it stands on the on-screen drafting form."
    now = datetime.now(UTC)

    if existing is None:
        file_id = uuid4()
        s3_key = f"{prefix}/drafted-forms/{bucket_id}/{file_id}.pdf"
        # The object first: a put that fails leaves no row claiming figures
        # whose PDF was never written.
        _put_bucket_object(s3_key, "application/pdf", pdf_bytes)
        result_file = BucketFile(
            id=file_id,
            bucket_id=bucket_id,
            requested_document_id=requested_document.id,
            upload_link_id=upload_link_id,
            file_name=f"{file_label}.pdf"[:255],
            s3_key=s3_key,
            content_type="application/pdf",
            size_bytes=len(pdf_bytes),
            uploaded_by_name=actor_name,
            uploaded_by_email=actor_email,
            status="uploaded",
        )
        db.add(result_file)
        await db.flush()
        db.add(
            BucketFileAnalysis(
                bucket_file_id=result_file.id,
                bucket_id=bucket_id,
                content_hash=content_hash,
                analysis_version=CURRENT_FILE_ANALYSIS_VERSION,
                provider=DRAFTED_FORM_PROVIDER,
                status="completed",
                classification=classification,
                confidence="high",
                summary=summary,
                analysis={"key_facts": key_facts},
                analyzed_at=now,
            )
        )
    else:
        result_file, analysis = existing
        _put_bucket_object(result_file.s3_key, "application/pdf", pdf_bytes)
        # The label carries the period, so it changes when the period does.
        result_file.file_name = f"{file_label}.pdf"[:255]
        result_file.size_bytes = len(pdf_bytes)
        result_file.status = "uploaded"
        if actor_name:
            result_file.uploaded_by_name = actor_name
        if actor_email:
            result_file.uploaded_by_email = actor_email
        analysis.content_hash = content_hash
        analysis.analysis_version = CURRENT_FILE_ANALYSIS_VERSION
        analysis.provider = DRAFTED_FORM_PROVIDER
        analysis.status = "completed"
        analysis.classification = classification
        analysis.confidence = "high"
        analysis.summary = summary
        analysis.analysis = {"key_facts": key_facts}
        analysis.analyzed_at = now
        analysis.error = None

    if mark_uploaded:
        requested_document.status = "uploaded"
    await db.flush()
    return result_file


def _render_business_statement(kind: str, body: dict[str, Any]) -> bytes:
    from app.services import dealer_forms_pdf

    if kind == "p_and_l":
        return dealer_forms_pdf.render_p_and_l_pdf(body=body)
    return dealer_forms_pdf.render_balance_sheet_pdf(body=body)


async def _plan_refresh(
    db: AsyncSession,
    profile: Any,
    kind: str,
    *,
    body: dict[str, Any] | None,
    statement: Any,
) -> dict[str, Any] | None:
    """What to file for this form, or None when there is nothing to file.

    Reads only, and renders; no writes, so the caller opens its savepoint round
    the write alone. Returns None — quietly — when the file has no checklist
    row for the form or nothing has been typed into it yet.
    """
    from app.routers.application_profiles import _requested_slot
    from app.services import (
        business_statement_schema,
        business_statements,
        dealer_forms_pdf,
        financial_statements,
        pfs_schema,
    )

    if kind not in _KINDS:
        return None
    slot = await _requested_slot(db, profile, kind)
    if slot is None:
        return None

    if kind in business_statement_schema.KINDS:
        if body is None:
            statement = statement or await business_statements.latest_for_profile(
                db, profile.id, kind
            )
            body = statement.body if statement else None
        if not body:
            return None
        schema = business_statement_schema.SCHEMA_FOR[kind]
        return {
            "requested_document": slot,
            "pdf_bytes": _render_business_statement(kind, body),
            "file_label": business_statements.file_label(kind, body),
            "classification": schema.classification,
            "key_facts": schema.key_facts(body),
        }

    if kind == "debt_schedule":
        rows = financial_statements.debt_rows_from_body(body) if body is not None else None
        if rows is None:
            # No schedule in hand — an editor that never finished loading, or a
            # caller with nothing to pass. Fall back to what the file holds
            # rather than filing a PDF that says this borrower owes nobody.
            rows = financial_statements.debt_rows_from_body(
                await financial_statements.debt_body_for_profile(db, profile)
            )
        if rows is None:
            return None
        facts = financial_statements.debt_key_facts(rows)
        return {
            "requested_document": slot,
            "pdf_bytes": dealer_forms_pdf.render_debt_schedule_pdf(
                business_name=str((body or {}).get("business_name") or "").strip()
                or "the business",
                rows=rows,
                total_balance=facts["total_outstanding_balance"],
                total_monthly=facts["total_monthly_debt_service"],
            ),
            "file_label": "Business Debt Schedule",
            "classification": "debt_schedule",
            "key_facts": facts,
        }

    statement = statement or await financial_statements.latest_for_profile(db, profile.id)
    if body is None:
        body = statement.body if statement else None
    if not body:
        return None
    statement_date = (
        statement.statement_date.isoformat()
        if statement is not None and statement.statement_date
        else "not stated"
    )
    applicant = body.get("applicant") or {}
    label = f"Personal Financial Statement — {applicant.get('name') or 'applicant'}"
    return {
        "requested_document": slot,
        "pdf_bytes": dealer_forms_pdf.render_pfs_413_pdf(body=body, statement_date=statement_date),
        "file_label": label,
        "classification": "personal_financial_statement",
        "key_facts": pfs_schema.key_facts(body, statement_date=statement_date),
    }


async def refresh_saved_form(
    db: AsyncSession,
    profile: Any,
    kind: str,
    *,
    body: dict[str, Any] | None = None,
    statement: Any = None,
    actor_name: str | None = None,
    actor_email: str | None = None,
) -> BucketFile | None:
    """A save keeps the form's PDF current. The one call a save path makes.

    `kind` is one of p_and_l, balance_sheet, debt_schedule, pfs. `body` is what
    was just saved; without it the current state is read back off the file.

    **Call it after the save is committed.** Rendering is WeasyPrint and the
    put is a network round trip, so it does not belong inside the transaction
    the borrower is waiting on — and, more to the point, a form that saved
    must stay saved whether or not we managed to draw a picture of it. Wrapped
    exactly the way `file_events.emit` is: the write goes in its own savepoint,
    every failure is logged and swallowed, nothing raises, and the caller's
    transaction is never poisoned. Commits its own write, since the caller's
    commit has already happened.

    Never flips the checklist row to "uploaded" — see `refresh_form_pdf`. This
    is the save path; only a submit files.

    Returns the document, or None when there was nothing to do (no document
    room, no checklist row for this form, nothing typed) or when it failed.
    """
    if profile is None or getattr(profile, "primary_bucket_id", None) is None:
        return None
    try:
        plan = await _plan_refresh(db, profile, kind, body=body, statement=statement)
    except Exception:  # noqa: BLE001
        # Reading and rendering only. Nothing was written, so there is nothing
        # to roll back — a renderer that throws must leave the session exactly
        # as it found it.
        log.exception(
            "drafted_forms.refresh_saved_form could not draw kind=%s profile=%s",
            kind,
            getattr(profile, "id", None),
        )
        return None
    if plan is None:
        return None
    try:
        async with db.begin_nested():
            stored = await refresh_form_pdf(
                db,
                bucket_id=profile.primary_bucket_id,
                actor_name=actor_name,
                actor_email=actor_email,
                mark_uploaded=False,
                **plan,
            )
        await db.commit()
        return stored
    except Exception:  # noqa: BLE001
        log.exception(
            "drafted_forms.refresh_saved_form failed kind=%s profile=%s",
            kind,
            getattr(profile, "id", None),
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            log.exception("drafted_forms.refresh_saved_form could not roll back")
        return None
