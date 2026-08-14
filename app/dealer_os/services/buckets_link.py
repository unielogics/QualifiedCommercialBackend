"""Dealer OS <-> Buckets bridge (Phase 2).

Every DealerBusiness gets one linked document Bucket:

  1. dealer.bucket_id already set -> use it (re-link lazily if the bucket was
     hard-deleted and the FK SET NULL'd / left a dangling id).
  2. else adopt the bucket of the NEWEST PublicUnderwritingIntake whose email
     matches dealer.email (case-insensitive) — the dealer's AI-underwriter
     lead and its already-uploaded statements become the audit workspace.
  3. else create a fresh audit Bucket ("Audit — {dealer.name}").

Push: a Dealer OS document upload mirrors a BucketFile row referencing the
SAME s3_key (no bytes are copied — both tables read the same archive object).
Pull: a BucketFile in the linked bucket is ingested into Dealer OS, reusing
the cached BucketFileAnalysis (content_hash + current version) when one
exists so no model tokens are re-spent; otherwise the raw bytes run through
the normal extract pipeline.

All functions flush, never commit — callers own the transaction. Existing
tables (buckets, bucket_files, public_underwriting_intakes) are only ever
read or appended to; no existing rows are mutated.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# READ-ONLY reuse of existing models (rows created, never schemas altered).
from app.models.bucket import Bucket, BucketFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake

from ..models import DealerBusiness, DealerDocument
from .extract import _parse_amount

logger = logging.getLogger(__name__)

_UPLOADED_BY = "Dealer OS"


async def ensure_bucket(db: AsyncSession, dealer: DealerBusiness) -> Bucket:
    """Resolve (and persist) the dealer's linked Bucket. Flushes, never commits."""
    if dealer.bucket_id is not None:
        bucket = await db.get(Bucket, dealer.bucket_id)
        if bucket is not None:
            return bucket
        # Dangling link (bucket hard-deleted) — fall through and re-link.
        dealer.bucket_id = None

    # Adopt the newest AI-underwriter intake bucket matched by email.
    if dealer.email:
        intake = (
            await db.execute(
                select(PublicUnderwritingIntake)
                .where(func.lower(PublicUnderwritingIntake.email) == dealer.email.strip().lower())
                .order_by(PublicUnderwritingIntake.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if intake is not None:
            bucket = await db.get(Bucket, intake.bucket_id)
            if bucket is not None:
                dealer.bucket_id = bucket.id
                await db.flush()
                logger.info(
                    "dealer-os: dealer %s adopted intake bucket %s (intake %s)",
                    dealer.id, bucket.id, intake.id,
                )
                return bucket

    # No intake to adopt — create a fresh audit bucket. Only `name` is
    # required on Bucket; bucket_type/purpose stay at their model defaults.
    bucket = Bucket(name=f"Audit — {dealer.name}"[:180], client_name=(dealer.name or "")[:180] or None)
    db.add(bucket)
    await db.flush()
    dealer.bucket_id = bucket.id
    await db.flush()
    logger.info("dealer-os: created audit bucket %s for dealer %s", bucket.id, dealer.id)
    return bucket


async def push_document(
    db: AsyncSession, dealer: DealerBusiness, doc: DealerDocument, raw_size: int
) -> BucketFile | None:
    """Mirror an uploaded Dealer OS document into the linked bucket as a
    BucketFile referencing the SAME s3_key. Returns None (and stays silent)
    when the bytes were never archived (doc.s3_key is None) — a bucket file
    must point at a real S3 object. Flushes, never commits."""
    if not doc.s3_key:
        return None
    if doc.bucket_file_id is not None:
        return await db.get(BucketFile, doc.bucket_file_id)
    bucket = await ensure_bucket(db, dealer)
    bucket_file = BucketFile(
        bucket_id=bucket.id,
        file_name=doc.filename[:255],
        s3_key=doc.s3_key,
        content_type=(doc.content_type or "application/octet-stream")[:160],
        size_bytes=int(raw_size),
        uploaded_by_name=_UPLOADED_BY,
    )
    db.add(bucket_file)
    await db.flush()
    doc.bucket_file_id = bucket_file.id
    await db.flush()
    return bucket_file


# --- cached-analysis adapter (pure, unit-testable) ---------------------------

_PERIOD_MONTH_RE = re.compile(r"(\d{4})[-/](\d{1,2})")


def _month_key(period: Any) -> str | None:
    """'2026-01-01 to 2026-01-31' (or any text leading with YYYY-MM) -> '2026-01'."""
    m = _PERIOD_MONTH_RE.search(str(period or ""))
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{m.group(1)}-{month:02d}"


def _abs_num(raw: Any) -> float | None:
    v = _parse_amount(raw)
    return abs(v) if v is not None else None


def adapt_analysis_to_extraction(analysis: dict[str, Any]) -> dict[str, Any]:
    """Adapt one BucketFileAnalysis.analysis JSON (the bank-statement
    key_facts shape read by public_underwriting_packet_pdf.extract_bank_months:
    statement_period / total_deposits_and_credits / total_withdrawals_and_debits
    / ending_balance / average_ledger_balance / low_daily_balance /
    nsf_or_overdraft_count, either flat or as a key_facts.months[] list) into
    the exact canonical dict services.extract.apply_extraction expects:

        {"months": [{"month": "YYYY-MM", "total_deposits", "total_withdrawals",
                     "ending_balance", "average_ledger_balance",
                     "low_daily_balance", "nsf_count"}],
         "transactions": []}

    The analysis cache carries month summaries only (no transaction lines),
    so transactions is always []. Pure — no DB, no IO."""
    key_facts = analysis.get("key_facts") if isinstance(analysis, dict) else None
    if not isinstance(key_facts, dict):
        key_facts = {}
    sources: list[dict[str, Any]] = []
    raw_months = key_facts.get("months")
    if isinstance(raw_months, list) and raw_months:
        sources = [m for m in raw_months if isinstance(m, dict)]
    elif key_facts:
        sources = [key_facts]

    months: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        month = _month_key(src.get("statement_period") or src.get("month"))
        if month is None or month in seen:
            continue
        seen.add(month)
        months.append(
            {
                "month": month,
                "total_deposits": _parse_amount(src.get("total_deposits_and_credits")),
                # canonical shape wants withdrawals as a positive magnitude
                "total_withdrawals": _abs_num(src.get("total_withdrawals_and_debits")),
                "ending_balance": _parse_amount(src.get("ending_balance")),
                "average_ledger_balance": _parse_amount(src.get("average_ledger_balance")),
                "low_daily_balance": _parse_amount(src.get("low_daily_balance")),
                "nsf_count": src.get("nsf_or_overdraft_count", src.get("nsf_count")),
            }
        )
    return {"months": months, "transactions": []}


def guess_document_kind(file_name: str | None) -> str:
    """'statement' when the name looks like a bank statement, else 'other'."""
    lower = (file_name or "").lower()
    if any(k in lower for k in ("statement", "stmt", "bank", "checking", "chase", "wells", "boa")):
        return "statement"
    return "other"
