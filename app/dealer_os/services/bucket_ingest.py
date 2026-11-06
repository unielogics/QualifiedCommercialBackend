"""Reusable Dealer OS bucket ingestion bridge.

Bucket uploads can arrive from the audit bucket UI, client room links, manual
bucket linking, or explicit "ingest all" actions. All of those paths should
feed the same Dealer OS document pipeline instead of each route carrying its
own copy of the sweep logic.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy import select

from app.db import SessionLocal
from app.models.bucket import BucketFile
from app.services.document_version_selection import latest_document_file_clause

from ..models import DealerBusiness, DealerDocument

logger = logging.getLogger(__name__)

MAX_AUTO_INGEST = 60


async def prepare_ingested_document(db, doc: DealerDocument, source: BucketFile) -> None:
    """Carry only explicit bucket replacement lineage into the dealer store.

    The stores have separate family roots. Never copy a bucket family UUID
    into DealerDocument, or infer related loans from a filename/lender name.
    A missing intermediate mirror can still resolve to the last mirrored
    member of this explicitly versioned bucket family.
    """
    doc.id = doc.id or uuid4()
    doc.document_family_id = doc.id
    doc.document_version_number = 1
    doc.document_version_status = "current"
    doc.is_current_document_version = True
    doc.content_hash = source.content_hash
    if not source.supersedes_file_id:
        return
    # Accepted source classification is not approval to replace an already
    # published dealer ledger. The normal team Approve action publishes it.
    doc.status = "pending_review"
    doc.document_version_status = "pending"
    doc.is_current_document_version = False
    previous = (await db.execute(
        select(DealerDocument).join(BucketFile, DealerDocument.bucket_file_id == BucketFile.id)
        .where(
            DealerDocument.dealer_id == doc.dealer_id,
            BucketFile.bucket_id == source.bucket_id,
            BucketFile.document_family_id == source.document_family_id,
            BucketFile.document_version_number < source.document_version_number,
        ).order_by(BucketFile.document_version_number.desc(), DealerDocument.document_version_number.desc()).limit(1)
    )).scalar_one_or_none()
    if previous is None:
        return
    members = list((await db.execute(select(DealerDocument).where(
        DealerDocument.dealer_id == doc.dealer_id,
        DealerDocument.document_family_id == previous.document_family_id,
    ))).scalars().all())
    doc.document_family_id = previous.document_family_id
    doc.document_version_number = max((row.document_version_number or 1 for row in members), default=previous.document_version_number or 1) + 1
    doc.supersedes_document_id = previous.id


async def auto_ingest_dealer_bucket_files(dealer_id: UUID) -> None:
    """Auto-ingest not-yet-ingested files from one dealer's linked bucket.

    Own session per file keeps one bad document from cancelling the whole
    sweep. The import of `_ingest_bucket_file_core` is deliberately lazy to
    avoid a service/router import cycle at module load time.
    """
    try:
        async with SessionLocal() as db:
            dealer = await db.get(DealerBusiness, dealer_id)
            if dealer is None or dealer.bucket_id is None:
                return
            ingested = set(
                (
                    await db.execute(
                        select(DealerDocument.bucket_file_id).where(
                            DealerDocument.dealer_id == dealer_id,
                            DealerDocument.bucket_file_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            file_ids = [
                fid
                for fid in (
                    await db.execute(
                        select(BucketFile.id)
                        .where(
                            BucketFile.bucket_id == dealer.bucket_id,
                            BucketFile.deleted_at.is_(None),
                            BucketFile.status == "uploaded",
                            latest_document_file_clause(BucketFile.id),
                        )
                        .order_by(BucketFile.created_at.asc())
                    )
                )
                .scalars()
                .all()
                if fid not in ingested
            ][:MAX_AUTO_INGEST]

        from app.dealer_os.router import _ingest_bucket_file_core

        for fid in file_ids:
            async with SessionLocal() as db:
                try:
                    dealer = await db.get(DealerBusiness, dealer_id)
                    if dealer is None:
                        return
                    await _ingest_bucket_file_core(db, dealer, fid)
                    await db.commit()
                except Exception:
                    logger.exception(
                        "dealer-os: auto-ingest failed for bucket file %s (dealer %s)",
                        fid,
                        dealer_id,
                    )
    except Exception:
        logger.exception("dealer-os: auto-ingest sweep failed for dealer %s", dealer_id)


async def auto_ingest_bucket_files_for_bucket(bucket_id: UUID) -> None:
    """Schedule Dealer OS ingestion for every linked dealer on a bucket."""
    try:
        async with SessionLocal() as db:
            dealer_ids = (
                (
                    await db.execute(
                        select(DealerBusiness.id).where(DealerBusiness.bucket_id == bucket_id)
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("dealer-os: could not resolve dealers for bucket %s", bucket_id)
        return

    for dealer_id in dealer_ids:
        await auto_ingest_dealer_bucket_files(dealer_id)
