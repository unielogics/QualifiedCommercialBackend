"""Plaid statement sync — pull PDFs into the normal document pipeline.

The whole integration is "fetch statement PDFs we haven't seen and drop them
into the same pipeline an upload uses": extract_document does everything else
(classification, account matching by mask, periods, events, recurrence,
snapshot). Idempotency is the partial-unique (dealer_id, plaid_statement_id)
index — a statement ingests exactly once, so refresh is always safe to run.

Plaid-pulled documents extract IMMEDIATELY regardless of who connected the
bank: the content comes from the bank, not the uploader, so the dealer
pending_review quarantine deliberately does not apply.

Flushes; callers own the commit. refresh_due() is the scheduler entrypoint
and manages its own sessions (one per item — one bank's failure never poisons
the sweep).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerDocument, DealerPlaidItem
from . import plaid_client
from .audit import log_action
from .extract import extract_document, store_document_bytes

logger = logging.getLogger(__name__)

# Bound per sweep tick so one giant backlog can't monopolize a worker.
MAX_ITEMS_PER_TICK = 20
# Bound per item per sync — first pulls can reach ~24 statements x N accounts.
MAX_STATEMENTS_PER_SYNC = 60


def _now() -> datetime:
    return datetime.now(UTC)


async def _bookkeep(db: AsyncSession, item_pk, *, error: str | None, retry_days: int) -> None:
    """Stamp the item's sync outcome in its OWN short transaction, immune to
    whatever state the sync loop left the session in."""
    await db.rollback()  # clear any in-flight state first
    item = (
        await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.id == item_pk))
    ).scalar_one_or_none()
    if item is None or item.status == "removed":
        return
    item.last_pulled_at = _now()
    item.next_refresh_at = _now() + timedelta(days=retry_days)
    item.status = "error" if error else "active"
    item.error = (error or None) and error[:500]
    await log_action(
        db,
        item.dealer_id,
        None,
        "plaid.sync",
        "plaid_item",
        entity_id=item.id,
        after={"status": item.status, "error": item.error, "retry_days": retry_days},
    )
    await db.commit()


async def sync_item(db: AsyncSession, item: DealerPlaidItem) -> dict:
    """Pull any not-yet-ingested statements for one connected bank.

    Each statement is COMMITTED individually — one bad PDF (or a unique-index
    race with a concurrent sync) never discards the others' work or poisons
    the session, and extract_document's internal failure rollback can only
    affect the statement being processed. Returns {pulled, skipped, failed}.
    Bookkeeping: next_refresh_at +30 days on success, +1 day on listing
    failure so transient errors retry daily instead of stalling a month.
    """
    if item.status == "removed" or item.environment != plaid_client.environment():
        return {"pulled": 0, "skipped": 0, "failed": 0}
    item_pk = item.id
    dealer_id = item.dealer_id
    item_label = item.item_id[:12]

    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        await _bookkeep(
            db, item_pk,
            error="Stored bank credentials could not be read — reconnect the bank.",
            retry_days=1,
        )
        return {"pulled": 0, "skipped": 0, "failed": 1}

    try:
        listing = await plaid_client.statements_list(token)
    except plaid_client.PlaidUnavailable as exc:
        await _bookkeep(db, item_pk, error=str(exc), retry_days=1)
        return {"pulled": 0, "skipped": 0, "failed": 1}

    institution = listing.get("institution_name")
    if institution and not item.institution_name:
        item.institution_name = str(institution)[:160]
    if not item.accounts_label:
        try:
            accts = await plaid_client.accounts_get(token)
            parts = [
                " ".join(x for x in (a.get("name"), "··" + a["mask"] if a.get("mask") else None) if x)
                for a in accts
                if a.get("name") or a.get("mask")
            ]
            if parts:
                item.accounts_label = (" · ".join(parts))[:200]
        except plaid_client.PlaidUnavailable:
            pass  # label is cosmetic — never fail a sync over it
    await db.commit()
    label = item.institution_name or "Bank"

    existing = set(
        (
            await db.execute(
                select(DealerDocument.plaid_statement_id).where(
                    DealerDocument.dealer_id == dealer_id,
                    DealerDocument.plaid_statement_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    # Dedupe FIRST, cap the not-yet-ingested remainder — already-pulled
    # statements must never consume cap slots or the tail past the cap
    # would stay unreachable forever.
    fresh = [st for st in listing["statements"] if st["statement_id"] not in existing]
    skipped = len(listing["statements"]) - len(fresh)
    capped = fresh[:MAX_STATEMENTS_PER_SYNC]
    if len(fresh) > len(capped):
        logger.info(
            "dealer-os plaid: item %s capping sync at %d of %d new statements (rest next run)",
            item_label, len(capped), len(fresh),
        )

    pulled = failed = 0
    for st in capped:
        sid = st["statement_id"]
        try:
            raw = await plaid_client.statements_download(token, sid)
            if not raw:
                failed += 1
                continue
            period = (
                f"{st['year']}-{int(st['month']):02d}"
                if st.get("year") and st.get("month")
                else sid[:8]
            )
            acct = f" {st['account_name']}" if st.get("account_name") else ""
            doc = await store_document_bytes(
                db,
                dealer_id,
                raw,
                f"{label}{acct} statement {period}.pdf",
                "application/pdf",
                kind="statement",
                plaid_statement_id=sid,
                plaid_item_id=item_pk,
            )
            await extract_document(db, doc, raw)
            note = f"Pulled from Plaid · {label} · statement period {period}"
            if isinstance(doc.extracted, dict):
                notes = list(doc.extracted.get("notes") or [])
                doc.extracted = {**doc.extracted, "notes": ([note] + notes)[:50]}
            await db.commit()  # each statement durable on its own
            pulled += 1
        except Exception:
            logger.exception(
                "dealer-os plaid: statement %s failed for item %s", sid[:12], item_label
            )
            await db.rollback()  # reset for the next statement
            failed += 1

    await _bookkeep(db, item_pk, error=None, retry_days=plaid_client.REFRESH_EVERY_DAYS)
    logger.info(
        "dealer-os plaid: item %s pulled=%d skipped=%d failed=%d",
        item_label, pulled, skipped, failed,
    )
    return {"pulled": pulled, "skipped": skipped, "failed": failed}


async def refresh_due() -> dict:
    """Scheduler entrypoint: sync every active item whose next_refresh_at is
    due. One session per item — a bank outage never poisons the sweep. The
    30-day cadence lives in next_refresh_at, so the daily tick is cheap."""
    from app.db import SessionLocal  # local import: scheduler runs at app scope

    if not plaid_client.enabled():
        return {"items": 0}
    async with SessionLocal() as db:
        due_ids = (
            (
                await db.execute(
                    select(DealerPlaidItem.id)
                    .where(
                        # 'error' rows stay in the sweep — a transient bank
                        # blip retries daily instead of silently ending the
                        # 30-day auto-refresh forever. Only 'removed' is out.
                        DealerPlaidItem.status.in_(("active", "error")),
                        DealerPlaidItem.environment == plaid_client.environment(),
                        DealerPlaidItem.auto_refresh.is_(True),
                        DealerPlaidItem.next_refresh_at.is_not(None),
                        DealerPlaidItem.next_refresh_at <= _now(),
                    )
                    .limit(MAX_ITEMS_PER_TICK)
                )
            )
            .scalars()
            .all()
        )
    synced = 0
    for item_id in due_ids:
        try:
            async with SessionLocal() as db:
                item = (
                    await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.id == item_id))
                ).scalar_one_or_none()
                if item is None:
                    continue
                await sync_item(db, item)
                await db.commit()
                synced += 1
        except Exception:
            logger.exception("dealer-os plaid: refresh failed for item %s", item_id)
    return {"items": synced}
