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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerDocument, DealerPlaidItem
from . import plaid_client
from .extract import extract_document, store_document_bytes

logger = logging.getLogger(__name__)

# Bound per sweep tick so one giant backlog can't monopolize a worker.
MAX_ITEMS_PER_TICK = 20
# Bound per item per sync — first pulls can reach ~24 statements x N accounts.
MAX_STATEMENTS_PER_SYNC = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def sync_item(db: AsyncSession, item: DealerPlaidItem) -> dict:
    """Pull any not-yet-ingested statements for one connected bank.

    Returns {pulled, skipped, failed}. Updates the item's bookkeeping in
    place: last_pulled_at, next_refresh_at (+30 days on success, +1 day on
    error so a transient failure retries daily instead of stalling a month).
    """
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        item.status = "error"
        item.error = "Stored bank credentials could not be read — reconnect the bank."
        item.next_refresh_at = _now() + timedelta(days=1)
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}

    try:
        listing = await plaid_client.statements_list(token)
    except plaid_client.PlaidUnavailable as exc:
        item.status = "error"
        item.error = str(exc)[:500]
        item.next_refresh_at = _now() + timedelta(days=1)
        await db.flush()
        return {"pulled": 0, "skipped": 0, "failed": 1}

    if listing.get("institution_name") and not item.institution_name:
        item.institution_name = str(listing["institution_name"])[:160]

    existing = set(
        (
            await db.execute(
                select(DealerDocument.plaid_statement_id).where(
                    DealerDocument.dealer_id == item.dealer_id,
                    DealerDocument.plaid_statement_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    pulled = skipped = failed = 0
    for st in listing["statements"][:MAX_STATEMENTS_PER_SYNC]:
        sid = st["statement_id"]
        if sid in existing:
            skipped += 1
            continue
        try:
            raw = await plaid_client.statements_download(token, sid)
            if not raw:
                failed += 1
                continue
            label = item.institution_name or "Bank"
            period = (
                f"{st['year']}-{int(st['month']):02d}"
                if st.get("year") and st.get("month")
                else sid[:8]
            )
            acct = f" {st['account_name']}" if st.get("account_name") else ""
            doc = await store_document_bytes(
                db,
                item.dealer_id,
                raw,
                f"{label}{acct} statement {period}.pdf",
                "application/pdf",
                kind="statement",
                plaid_statement_id=sid,
            )
            await extract_document(db, doc, raw)
            note = f"Pulled from Plaid · {label} · statement period {period}"
            if isinstance(doc.extracted, dict):
                notes = list(doc.extracted.get("notes") or [])
                doc.extracted = {**doc.extracted, "notes": ([note] + notes)[:50]}
            existing.add(sid)
            pulled += 1
        except Exception:
            logger.exception(
                "dealer-os plaid: statement %s failed for item %s", sid[:12], item.item_id[:12]
            )
            failed += 1

    item.last_pulled_at = _now()
    item.next_refresh_at = _now() + timedelta(days=plaid_client.REFRESH_EVERY_DAYS)
    if item.status == "error" or item.error:
        item.status, item.error = "active", None
    await db.flush()
    logger.info(
        "dealer-os plaid: item %s pulled=%d skipped=%d failed=%d",
        item.item_id[:12], pulled, skipped, failed,
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
                        DealerPlaidItem.status == "active",
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
