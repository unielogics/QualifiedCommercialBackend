"""One-shot backfill that materializes calendar events from existing
lifecycle rows.

Run via:
    docker exec qcbackend python -m app.scripts.backfill_calendar

Idempotent: every emit goes through `calendar_emitter.upsert_event`
which does INSERT ... ON CONFLICT (external_ref_kind, external_ref_id)
DO UPDATE. Re-running the script either no-ops or refreshes existing
rows in place; never duplicates.

What it covers:
  - Loans currently in CLOSING stage  → emit_for_loan_close
  - Credit pulls with expires_at      → emit_for_credit_pull
  - Documents with status='requested' → emit_for_document_request
  - Prequal requests with status='approved' AND expected_closing_date
                                      → emit_for_prequal_approval

Documents that are PENDING (have an s3_key but the borrower hasn't
finished uploading) are skipped — the calendar reminder is for
*outstanding* docs, not in-flight uploads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.enums import DocStatus, LoanStage
from app.models.credit_pull import CreditPull
from app.models.document import Document
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.services import calendar_emitter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_calendar")


async def main() -> None:
    started = datetime.now(timezone.utc)
    counts = {"loan_close": 0, "credit_expiry": 0, "document_due": 0, "prequal_close": 0}

    async with SessionLocal() as db:
        # Loans in CLOSING — emit (or refresh) close milestone
        loans = (
            await db.execute(select(Loan).where(Loan.stage == LoanStage.CLOSING))
        ).scalars().all()
        for loan in loans:
            await calendar_emitter.emit_for_loan_close(db, loan)
            counts["loan_close"] += 1

        # Active credit pulls with an expires_at
        pulls = (
            await db.execute(
                select(CreditPull).where(CreditPull.expires_at.is_not(None))
            )
        ).scalars().all()
        for pull in pulls:
            await calendar_emitter.emit_for_credit_pull(db, pull)
            counts["credit_expiry"] += 1

        # Outstanding requested documents
        docs = (
            await db.execute(
                select(Document).where(
                    Document.status == DocStatus.REQUESTED,
                    Document.requested_on.is_not(None),
                )
            )
        ).scalars().all()
        for doc in docs:
            await calendar_emitter.emit_for_document_request(db, doc)
            counts["document_due"] += 1

        # Approved prequals with a target close date
        reqs = (
            await db.execute(
                select(PrequalRequest).where(
                    PrequalRequest.status.in_(["approved", "offer_accepted"]),
                    PrequalRequest.expected_closing_date.is_not(None),
                )
            )
        ).scalars().all()
        for req in reqs:
            await calendar_emitter.emit_for_prequal_approval(db, req)
            counts["prequal_close"] += 1

        await db.commit()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("backfill complete in %.2fs: %s", elapsed, counts)


if __name__ == "__main__":
    asyncio.run(main())
