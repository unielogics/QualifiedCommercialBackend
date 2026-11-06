"""Retire only the accepted prior version's normalized projection.

The caller wraps retirement + new extraction in one savepoint. A failed,
quarantined, or identity-mismatched replacement rolls the entire projection
back, leaving the prior publication intact. Original files and extracted
summaries remain retained; removed normalized rows are snapshotted here.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import delete, exists, or_, select

from app.services.document_identity import document_identity_conflict, structured_document_identity

from ..models import (
    DealerAddback,
    DealerCashEvent,
    DealerDocument,
    DealerFinancialPeriod,
    DealerTaxFiling,
)
from .document_recovery import row_snapshot
from .normalize import rebuild_periods

ARCHIVE_KEY = "_superseded_document_projection"


def current_dealer_document_source(document_id):
    return or_(document_id.is_(None), exists(select(DealerDocument.id).where(
        DealerDocument.id == document_id,
        DealerDocument.status != "deleted",
        DealerDocument.is_current_document_version.is_(True), DealerDocument.document_version_status == "current",
    )))


def identity_conflict(new, old) -> str | None:
    def identity(doc):
        meta = doc.doc_meta if isinstance(doc.doc_meta, dict) else {}
        source = dict(meta.get("_document_identity_source") or {})
        if not source:
            source = {**meta, "months": (doc.extracted or {}).get("months") or []}
        value = structured_document_identity(source, doc.detected_kind or meta.get("doc_type"))
        if getattr(doc, "account_id", None):
            value["account_id"] = str(doc.account_id)
        return value
    return document_identity_conflict(identity(new), identity(old))


def _months(doc):
    values = (doc.extracted or {}).get("months") or []
    result = set()
    for raw in values:
        value = raw.get("month") or raw.get("period") if isinstance(raw, dict) else raw
        try:
            result.add(date.fromisoformat(str(value)[:7] + "-01"))
        except ValueError:
            continue
    return result


async def retire_previous_projection(db, doc) -> None:
    if not getattr(doc, "supersedes_document_id", None):
        return
    previous = list((await db.execute(select(DealerDocument).where(
        DealerDocument.dealer_id == doc.dealer_id,
        DealerDocument.document_family_id == doc.document_family_id,
        DealerDocument.id != doc.id,
        DealerDocument.is_current_document_version.is_(True),
    ))).scalars().all())
    old_ids = {row.id for row in previous}
    if not old_ids:
        return
    events = list((await db.execute(select(DealerCashEvent).where(
        DealerCashEvent.dealer_id == doc.dealer_id, DealerCashEvent.document_id.in_(old_ids),
    ))).scalars().all())
    filings = list((await db.execute(select(DealerTaxFiling).where(
        DealerTaxFiling.dealer_id == doc.dealer_id, DealerTaxFiling.document_id.in_(old_ids),
    ))).scalars().all())
    # Explicitly verified add-backs are the underwriter's decision, retained
    # independently of a later source document version.
    addbacks = list((await db.execute(select(DealerAddback).where(
        DealerAddback.dealer_id == doc.dealer_id, DealerAddback.document_id.in_(old_ids),
        DealerAddback.status != "verified",
    ))).scalars().all())
    scopes = {(event.account_id, event.period) for event in events}
    for previous_doc in previous:
        scopes.update((previous_doc.account_id, month) for month in _months(previous_doc))
    periods = list((await db.execute(select(DealerFinancialPeriod).where(
        DealerFinancialPeriod.dealer_id == doc.dealer_id,
        DealerFinancialPeriod.period.in_({period for _, period in scopes}),
    ))).scalars().all()) if scopes else []
    for previous_doc in previous:
        metadata = dict(previous_doc.doc_meta or {})
        metadata.setdefault(ARCHIVE_KEY, {
            "superseded_by_document_id": str(doc.id), "retired_at": datetime.now(UTC).isoformat(),
            "cash_events": [row_snapshot(row) for row in events if row.document_id == previous_doc.id],
            "tax_filings": [row_snapshot(row) for row in filings if row.document_id == previous_doc.id],
            "unverified_addbacks": [row_snapshot(row) for row in addbacks if row.document_id == previous_doc.id],
            "financial_periods": [row_snapshot(row) for row in periods if (row.account_id, row.period) in scopes],
        })
        previous_doc.doc_meta = metadata
    await db.execute(delete(DealerCashEvent).where(DealerCashEvent.dealer_id == doc.dealer_id, DealerCashEvent.document_id.in_(old_ids)))
    await db.execute(delete(DealerTaxFiling).where(DealerTaxFiling.dealer_id == doc.dealer_id, DealerTaxFiling.document_id.in_(old_ids)))
    await db.execute(delete(DealerAddback).where(
        DealerAddback.dealer_id == doc.dealer_id, DealerAddback.document_id.in_(old_ids), DealerAddback.status != "verified",
    ))
    remaining = list((await db.execute(select(DealerDocument).where(
        DealerDocument.dealer_id == doc.dealer_id, DealerDocument.id.not_in(old_ids | {doc.id}),
        DealerDocument.is_current_document_version.is_(True), DealerDocument.status == "extracted",
    ))).scalars().all())
    surviving_scopes = {(row.account_id, month) for row in remaining for month in _months(row)}
    for account_id, period in scopes:
        await rebuild_periods(db, doc.dealer_id, {period}, account_id=account_id)
    for row in periods:
        scope = (row.account_id, row.period)
        if scope not in scopes or scope in surviving_scopes or row.source == "manual":
            continue
        for key in ("starting_balance", "ending_balance", "low_balance", "avg_daily_balance"):
            setattr(row, key, None)
        row.nsf_count = 0
        if not any(event.account_id == row.account_id and event.period == row.period for event in events):
            continue
        # An absent source is unknown rather than a fabricated zero month.
        if not any(item.account_id == row.account_id and row.period in _months(item) for item in remaining):
            if row.deposits == 0:
                row.deposits = None
            if row.withdrawals == 0:
                row.withdrawals = None
    await db.flush()
