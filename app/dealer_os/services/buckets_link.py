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

_UPLOADED_BY = "Capital OS"


def audit_bucket_name(dealer: DealerBusiness) -> str:
    """Bucket naming convention: '{dealer} — {City}, {ST}' (location parts
    dropped when absent). Instantly identifiable in the shared bucket console."""
    loc = ", ".join(p for p in ((dealer.city or "").strip(), (dealer.state or "").strip()) if p)
    base = (dealer.name or "Client").strip()
    return (f"{base} — {loc}" if loc else base)[:180]


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
    bucket = Bucket(name=audit_bucket_name(dealer), client_name=(dealer.name or "")[:180] or None)
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


# --- Cached-analysis classification -------------------------------------------
#
# The intake pipeline's analysis JSON has no doc_type field — its shape is
# {baseline_categories_supported, key_facts, limitations, red_flags, supports}.
# So classification reads the key_facts SHAPE first (structural and reliable:
# a tax return carries tax_year/form_type, a statement carries
# statement_period/ending_balance) and falls back to keyword-matching the
# free-text baseline categories. Both are pure and deterministic, which keeps
# the cache path's guarantee that it never costs a model call.

_TAX_FACT_KEYS = ("tax_year", "form_type", "gross_receipts", "ordinary_business_income")
_STATEMENT_FACT_KEYS = (
    "statement_period",
    "ending_balance",
    "average_ledger_balance",
    "total_deposits_and_credits",
)


def _facts_of(analysis: dict[str, Any]) -> dict[str, Any]:
    facts = analysis.get("key_facts") if isinstance(analysis, dict) else None
    return facts if isinstance(facts, dict) else {}


def _baseline_text(analysis: dict[str, Any]) -> str:
    """All baseline-category labels lowercased into one searchable string."""
    raw = analysis.get("baseline_categories_supported") if isinstance(analysis, dict) else None
    if isinstance(raw, list):
        return " ".join(str(x) for x in raw).lower()
    return str(raw or "").lower()


def classify_cached_analysis(analysis: dict[str, Any]) -> str | None:
    """Best-effort document type for a cached BucketFileAnalysis.

    Returns one of extract._normalize_doc_type's labels ('bank_statement',
    'tax_return', 'profit_and_loss', 'balance_sheet', 'debt_schedule') or None
    when the analysis carries no recognizable signal — in which case the
    caller should fall back to a full re-extract rather than declaring the
    document failed."""
    facts = _facts_of(analysis)
    if any(k in facts for k in _STATEMENT_FACT_KEYS):
        return "bank_statement"
    if any(k in facts for k in _TAX_FACT_KEYS):
        return "tax_return"

    text = _baseline_text(analysis)
    if "bank statement" in text or "bank_statement" in text:
        return "bank_statement"
    if "tax return" in text or "tax_return" in text or "k-1" in text:
        return "tax_return"
    if "profit" in text and "loss" in text:
        return "profit_and_loss"
    if "balance sheet" in text:
        return "balance_sheet"
    if "debt schedule" in text:
        return "debt_schedule"
    return None


_BUSINESS_FORMS = ("1120", "1065", "1040-c", "schedule c")
_PERSONAL_FORMS = ("1040",)


def is_business_return(analysis: dict[str, Any]) -> bool:
    """True when a tax-return analysis is the BUSINESS's own return.

    dos_tax_filings is UNIQUE per (dealer, year) and exists to reconcile the
    business's reported revenue against its observed deposits. An owner's
    personal 1040 for the same year would collide with the business return and
    — since merge_tax_filings fills only NULLs — could land the owner's
    personal income as the company's revenue. A K-1 is likewise excluded: it
    reports a shareholder's share, not the entity's return, even though it
    names the entity.

    The discriminator is entity_name (business returns carry it, personal ones
    don't), with form_type as a cross-check."""
    facts = _facts_of(analysis)
    form = str(facts.get("form_type") or facts.get("form") or "").lower()
    if "k-1" in form or "k1" in form:
        return False
    if any(f in form for f in _BUSINESS_FORMS):
        return True
    if form and any(f in form for f in _PERSONAL_FORMS):
        return False
    return bool(str(facts.get("entity_name") or "").strip())


def adapt_analysis_to_tax_years(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapt a cached tax-return analysis into extract._route_tax_years' shape:
    [{"year": int, "revenue": float|None}].

    Revenue is the filing's top line — gross receipts for an 1120/1120-S/1065 —
    because the IRS module reconciles *reported revenue* against observed bank
    deposits. total_income/total_revenue are accepted as fallbacks."""
    facts = _facts_of(analysis)
    raw_year = facts.get("tax_year") or facts.get("year")
    try:
        year = int(float(str(raw_year).strip()[:4]))
    except (TypeError, ValueError):
        return []
    if not 1990 <= year <= 2100:
        return []
    revenue = None
    for key in ("gross_receipts", "total_revenue", "total_income", "revenue"):
        revenue = _parse_amount(facts.get(key))
        if revenue is not None:
            break
    return [{"year": year, "revenue": revenue}]
