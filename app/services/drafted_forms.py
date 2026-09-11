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

**A save no longer renders on the spot.** `enqueue_form_refresh` puts the form
on `form_pdf_refresh_queue` with a deadline 120 seconds out, every further save
pushes that deadline forward, and the scheduler's `job_form_pdf_refresh` calls
`refresh_saved_form` once the typing has stopped. A submit is untouched: it
still files synchronously, and clears the pending row on its way through.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.models.form_pdf_refresh import FormPdfRefresh
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
        # A submit has just filed this document synchronously. Any redraw still
        # waiting out its 120 seconds would land a minute later and put an
        # identical render over the top of it — work with no reader, and a
        # `updated_at` on the file that lies about when it was filed. Dropped
        # inside the submit's own transaction, so a submit that rolls back
        # keeps its place in the queue.
        await _clear_pending_refresh(db, bucket_id=bucket_id, classification=classification)
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


# ---------------------------------------------------------------------------
# Waiting for the typing to stop.
#
# Everything above renders when it is called. That was right when a save was a
# person filling in a page of boxes and pressing Save; it is wrong on the live
# worksheet, where a save is a cell and the render is a document rewritten on
# every keystroke.
#
# The owner's instruction: *"a simple adjustment to make is that we will wait
# 120 seconds before we generate PDF with updates. This way we prevent
# excessive mistakes from happening and documents being updated for no
# reason."* Two reasons, and both are about the reader rather than the CPU: a
# document rewritten on every keystroke is noise in a room somebody has to
# read, and a half-typed figure briefly filed as fact is worse than a document
# two minutes behind — because everything downstream reads `key_facts` and
# never asks how finished the number was.
#
# What makes this a debounce and not a rate limit is that a save pushes the
# deadline *forward* rather than claiming a slot. A rate limit fires on the
# first edit of a burst and drops the rest, so the last thing typed never
# reaches the PDF — exactly the figure that mattered. Here the render happens
# 120 seconds after the last edit, and it renders the finished number.
# ---------------------------------------------------------------------------

#: How long a form is left alone before its PDF is redrawn. The owner's number.
#: A document rewritten on every keystroke is noise, and a half-typed figure
#: briefly filed as fact is worse than a document two minutes behind.
REFRESH_DELAY_SECONDS = 120

#: How many due forms one tick draws. A render is WeasyPrint plus an S3 put, so
#: this bounds a tick rather than letting one backlog hold the scheduler's
#: event loop. Anything left over is still due and goes on the next tick.
REFRESH_DRAIN_LIMIT = 25

#: The analyser classification each form is filed under, read backwards. Only
#: used to find the queue row a submit should clear: the submit paths speak in
#: classifications (they are filing a document), the queue speaks in kinds.
_KIND_FOR_CLASSIFICATION = {
    "current_p_and_l": "p_and_l",
    "balance_sheet": "balance_sheet",
    "debt_schedule": "debt_schedule",
    "personal_financial_statement": "pfs",
}


async def enqueue_form_refresh(
    db: AsyncSession,
    profile: Any,
    kind: str,
    *,
    actor_name: str | None = None,
    actor_email: str | None = None,
    delay_seconds: int = REFRESH_DELAY_SECONDS,
) -> datetime | None:
    """Ask for this form's PDF to be redrawn once the typing stops.

    **Every save pushes `due_at` further out.** That is the whole mechanism: an
    upsert on (profile, kind) whose conflict branch moves the deadline rather
    than leaving the first one standing, so a burst of saves settles 120
    seconds after the *last* one instead of firing on the first and dropping
    everything after it.

    Nothing about what was typed is stored. The drain reads the body back off
    the file when it renders, so a row that waited through six more edits
    still draws today's figures — which is also why there is no `body`
    parameter here and no way for a stale payload to reach a PDF.

    `actor_name`/`actor_email` are carried so the refreshed document is
    attributed to whoever last typed rather than to the cron actor that drew
    it, and are coalesced on conflict: a later save with no name does not
    erase the name the earlier one had.

    **Call it after the save is committed**, like the render it replaces, and
    for the same reason — and it commits its own write, since the caller's
    commit has already happened. Swallows everything: a save that is already
    durable must never be reported as failed because a queue row would not go
    in. The cost of that is a PDF that stays behind until the next save, which
    is the same cost the old immediate render paid when WeasyPrint threw.
    """
    if profile is None or kind not in _KINDS:
        return None
    try:
        profile_id = profile.id
        has_room = profile.primary_bucket_id is not None
    except Exception:  # noqa: BLE001 - an expired ORM row, mid-loop
        # Reading an attribute off a row a rollback has expired is a lazy load
        # with no greenlet under it. That can happen here: the caller committed
        # before calling, and something between may have rolled back — the
        # per-kind loop in `sheets.refresh_touched_pdfs`, for one. The redraw is
        # lost, which costs a stale PDF until the next save; raising would cost
        # a 500 on a save that is already durable.
        log.exception("drafted_forms.enqueue_form_refresh could not read the file kind=%s", kind)
        return None
    if profile_id is None or not has_room:
        # No document room means there is nothing to file into, so there is
        # nothing to hold back either.
        return None

    now = datetime.now(UTC)
    due_at = now + timedelta(seconds=max(0, int(delay_seconds)))
    table = FormPdfRefresh.__table__
    insert = pg_insert(table).values(
        id=uuid4(),
        profile_id=profile_id,
        kind=kind,
        due_at=due_at,
        actor_name=actor_name,
        actor_email=actor_email,
        created_at=now,
        updated_at=now,
    )
    statement = insert.on_conflict_do_update(
        constraint="uq_form_pdf_refresh_queue_profile_kind",
        set_={
            # The push. Not `greatest(...)`: a later save always wins, because
            # the point is to wait for the person who is still typing.
            "due_at": insert.excluded.due_at,
            "actor_name": func.coalesce(insert.excluded.actor_name, table.c.actor_name),
            "actor_email": func.coalesce(insert.excluded.actor_email, table.c.actor_email),
            "updated_at": insert.excluded.updated_at,
        },
    )
    try:
        await db.execute(statement)
        await db.commit()
    except Exception:  # noqa: BLE001 - a queued redraw must never fail a save
        log.exception(
            "drafted_forms.enqueue_form_refresh failed kind=%s profile=%s", kind, profile_id
        )
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            log.exception("drafted_forms.enqueue_form_refresh could not roll back")
        return None
    return due_at


async def _clear_pending_refresh(
    db: AsyncSession, *, bucket_id: UUID, classification: str
) -> None:
    """Drop the queued redraw a submit has just made unnecessary.

    Keyed off the classification and the document room rather than taking a
    profile and a kind, so every submit path gets this by filing through
    `refresh_form_pdf` — the staff routes, the borrower's own link, the packet
    children and `business_statements.file_pdf` alike — without each of them
    having to remember. One statement; no read first, because a delete of
    nothing is already a no-op.

    Part of the caller's transaction, deliberately. A submit that rolls back
    has not filed anything, and its form should still be redrawn on schedule.
    """
    kind = _KIND_FOR_CLASSIFICATION.get(classification)
    if kind is None:
        return
    await db.execute(
        delete(FormPdfRefresh).where(
            FormPdfRefresh.kind == kind,
            FormPdfRefresh.profile_id.in_(
                select(ApplicationProfile.id).where(
                    ApplicationProfile.primary_bucket_id == bucket_id
                )
            ),
        )
    )


async def drain_form_refresh_queue(
    db: AsyncSession, *, limit: int = REFRESH_DRAIN_LIMIT
) -> int:
    """Redraw every form whose deadline has passed. The scheduler's half.

    Returns how many queue rows were cleared, which is not how many PDFs were
    written: a row whose form has no checklist slot, nothing typed into it yet
    or no document room is dropped rather than kept, because none of those
    resolve by waiting and a row that can never succeed would be redrawn every
    thirty seconds forever.

    **A failure must not wedge the queue.** One row is one transaction: the
    render, then the delete, then a commit. If anything raises, the session is
    rolled back and that row's `due_at` is pushed out by the delay, so it is
    retried on a later tick instead of being retried immediately, forever, in
    front of everything behind it. The other rows in the batch are unaffected.

    The columns are read as plain values rather than as ORM rows because
    `refresh_saved_form` commits, which would expire an ORM row mid-loop and
    turn the next attribute read into a lazy load with no greenlet under it.
    """
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(
                FormPdfRefresh.id,
                FormPdfRefresh.profile_id,
                FormPdfRefresh.kind,
                FormPdfRefresh.actor_name,
                FormPdfRefresh.actor_email,
            )
            .where(FormPdfRefresh.due_at <= now)
            .order_by(FormPdfRefresh.due_at)
            .limit(limit)
        )
    ).all()
    if not rows:
        return 0

    cleared = 0
    for row_id, profile_id, kind, actor_name, actor_email in rows:
        try:
            profile = await db.get(ApplicationProfile, profile_id)
            if profile is not None:
                # Reads the body off the file itself — so what lands in the PDF
                # is the state at this moment, not the state at the save that
                # first queued it. Swallows its own failures and commits its
                # own write.
                await refresh_saved_form(
                    db,
                    profile,
                    kind,
                    actor_name=actor_name,
                    actor_email=actor_email,
                )
            await db.execute(delete(FormPdfRefresh).where(FormPdfRefresh.id == row_id))
            await db.commit()
            cleared += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the drain
            log.exception(
                "drafted_forms.drain_form_refresh_queue failed kind=%s profile=%s",
                kind,
                profile_id,
            )
            try:
                await db.rollback()
                await db.execute(
                    update(FormPdfRefresh)
                    .where(FormPdfRefresh.id == row_id)
                    .values(
                        due_at=datetime.now(UTC) + timedelta(seconds=REFRESH_DELAY_SECONDS),
                        updated_at=datetime.now(UTC),
                    )
                )
                await db.commit()
            except Exception:  # noqa: BLE001
                log.exception(
                    "drafted_forms.drain_form_refresh_queue could not defer kind=%s profile=%s",
                    kind,
                    profile_id,
                )
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    log.exception("drafted_forms.drain_form_refresh_queue could not roll back")
    return cleared
