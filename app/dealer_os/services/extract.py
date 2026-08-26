"""Document extraction — the source-agnostic ingestion contract.

Every document (CSV/XLSX parsed in pure python; PDF/image read by the SAME
Bedrock vision model the intake pipeline uses) is reduced to one canonical
extraction dict:

    {
      "months": [{"month": "YYYY-MM", "total_deposits", "total_withdrawals",
                  "ending_balance", "average_ledger_balance",
                  "low_daily_balance", "nsf_count", "negative_balance_dates"}],
      "transactions": [{"date": "YYYY-MM-DD", "description", "amount"}],
    }

and then normalized through the EXISTING engine: transactions become
DealerCashEvent rows via classify_event (categorized_by='ai',
source='document'), month summaries upsert DealerFinancialPeriod
(source='document', never clobbering non-null fields on a source='manual'
row — mirroring rebuild_periods' manual-wins rule), rebuild_periods runs for
the months that received events (the event ledger is truth for
deposits/withdrawals there), and engines.recompute_snapshot refreshes metrics.

parse_csv_bytes / parse_xlsx_bytes / apply_extraction are pure (no DB, no IO)
so they are unit-testable; extract_document owns status transitions on the
DealerDocument row. Flushes but never commits — callers own the transaction.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# READ-ONLY reuse of the intake pipeline's Bedrock client + usage tracking.
from app.services.ai.bedrock_client import get_client, model_heavy
from app.services.ai.usage import tracked_messages_create

from ..models import DealerCashEvent, DealerDebt, DealerDocument, DealerFinancialPeriod, DealerTaxFiling
from .accounts import match_or_create_account
from .engines import recompute_snapshot
from .normalize import classify_with_rules, load_active_rules, period_of, rebuild_periods
from .recurrence import stamp_recurrence
from .refinance import FREQUENCY_MONTHLY_MULT, key_matches
from .vendors import normalize_vendor
from . import storage

logger = logging.getLogger(__name__)

MAX_TRANSACTIONS = 5000


# --- pure parsing helpers (mirrors the frontend ledger importer) -------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_US_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


def _parse_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw or "").strip()
    m = _ISO_RE.match(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        m = _US_RE.match(s)
        if not m:
            return None
        mo, d, y = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_amount(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw or "").strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = re.sub(r"[$,\s]", "", s)
    if s.startswith("-"):
        neg = True
        s = s[1:]
    if not re.fullmatch(r"\d*\.?\d+", s):
        return None
    n = float(s)
    return -n if neg else n


def _num(raw: Any) -> float | None:
    """Defensive numeric coercion for model-produced month fields."""
    v = _parse_amount(raw)
    return round(v, 2) if v is not None else None


def _header_map(cells: list[str]) -> tuple[dict[str, int], bool]:
    """Column mapping: header names if present, else positional
    date|description|amount — same regexes as the frontend importer."""
    head = [str(c or "").lower() for c in cells]
    h_date = next((i for i, h in enumerate(head) if re.search(r"date|posted", h)), -1)
    h_amt = next((i for i, h in enumerate(head) if re.search(r"amount|amt", h)), -1)
    h_desc = next((i for i, h in enumerate(head) if re.search(r"desc|memo|payee|detail|narrative", h)), -1)
    if h_date >= 0 and h_amt >= 0:
        if h_desc < 0:
            h_desc = next((i for i in range(len(head)) if i not in (h_date, h_amt)), 1)
        return {"date": h_date, "desc": h_desc, "amount": h_amt}, True
    return {"date": 0, "desc": 1, "amount": 2}, False


def _rows_from_table(raw_rows: list[list[Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    raw_rows = [r for r in raw_rows if any(str(c or "").strip() for c in r)]
    if not raw_rows:
        return [], ["File is empty."]
    idx, has_header = _header_map([str(c or "") for c in raw_rows[0]])
    start = 1 if has_header else 0
    if not has_header and _parse_date(raw_rows[0][0] if raw_rows[0] else "") is None:
        start = 1  # not a data row and not a recognizable header — skip anyway
    for i in range(start, len(raw_rows)):
        cells = raw_rows[i]

        def cell(j: int) -> Any:
            return cells[j] if j < len(cells) else ""

        d = _parse_date(cell(idx["date"]))
        amount = _parse_amount(cell(idx["amount"]))
        description = str(cell(idx["desc"]) or "").strip()
        if d is None:
            errors.append(f'Line {i + 1}: unrecognized date "{cell(idx["date"])}"')
            continue
        if amount is None:
            errors.append(f'Line {i + 1}: unrecognized amount "{cell(idx["amount"])}"')
            continue
        rows.append({"date": d.isoformat(), "description": description, "amount": amount})
    return rows, errors


def parse_csv_bytes(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Pure CSV -> transactions [{date, description, amount}], errors.
    Header-flexible (date/posted, amount/amt, desc/memo/payee/detail/narrative)
    with positional fallback — same contract as the frontend importer."""
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return _rows_from_table([[c.strip() for c in row] for row in reader])


def parse_xlsx_bytes(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """XLSX (first worksheet) -> transactions, via openpyxl (already in venv)."""
    try:
        import openpyxl  # noqa: PLC0415 — optional-at-runtime dependency
    except ImportError as exc:  # pragma: no cover — openpyxl is in the venv
        raise ValueError("XLSX parsing unavailable (openpyxl not installed) — upload a CSV export.") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"Could not read XLSX workbook: {exc}") from exc
    try:
        ws = wb.worksheets[0]
        table = [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _rows_from_table(table)


# --- pure normalization plan (db-free, unit-testable) ------------------------


def apply_extraction(extraction: dict[str, Any], rules: list[dict] | None = None) -> dict[str, Any]:
    """Transform one canonical extraction dict into the exact rows the engine
    ingests. Pure — returns a plan, touches no DB:

    {
      "events":  [{occurred_on, period, description, amount, category, flags,
                   categorized_by}],
      "period_upserts": {date(period): {deposits?, withdrawals?, ending_balance?,
                                        low_balance?, avg_daily_balance?, nsf_count?}},
      "event_periods": set[date],   # months whose ledger changed -> rebuild_periods
      "months": [...],              # normalized month summaries for doc.extracted
      "notes": [...],
    }

    ``rules`` (Phase 3) is the dealer's pre-loaded dos_category_rules list —
    a matching rule wins over the heuristics and marks the event
    categorized_by='rule' (otherwise 'ai').

    deposits/withdrawals from the month summary are only planned for months
    with NO inserted transactions — where events exist, rebuild_periods
    recomputes them from the ledger (event ledger is truth).
    """
    notes: list[str] = []
    events: list[dict[str, Any]] = []
    event_periods: set[date] = set()

    txns = extraction.get("transactions")
    if isinstance(txns, list):
        for t in txns[:MAX_TRANSACTIONS]:
            if not isinstance(t, dict):
                continue
            d = _parse_date(t.get("date"))
            amount = _parse_amount(t.get("amount"))
            if d is None or amount is None:
                notes.append(f"Skipped transaction with unparseable date/amount: {t!r:.120}")
                continue
            description = str(t.get("description") or "").strip()[:320] or "(no description)"
            category, flags, rule_matched = classify_with_rules(rules or [], description, amount)
            period = period_of(d)
            event_periods.add(period)
            events.append(
                {
                    "occurred_on": d,
                    "period": period,
                    "description": description,
                    "amount": round(amount, 2),
                    "category": category,
                    "flags": flags,
                    "categorized_by": "rule" if rule_matched else "ai",
                }
            )
        if isinstance(txns, list) and len(txns) > MAX_TRANSACTIONS:
            notes.append(f"Transaction list truncated at {MAX_TRANSACTIONS} rows.")

    months_out: list[dict[str, Any]] = []
    period_upserts: dict[date, dict[str, Any]] = {}
    raw_months = extraction.get("months")
    if isinstance(raw_months, list):
        for m in raw_months:
            if not isinstance(m, dict):
                continue
            match = _MONTH_RE.match(str(m.get("month") or "").strip())
            if not match:
                notes.append(f"Skipped month with unparseable key: {m.get('month')!r}")
                continue
            y, mo = int(match.group(1)), int(match.group(2))
            if not 1 <= mo <= 12:
                notes.append(f"Skipped month with invalid month number: {m.get('month')!r}")
                continue
            period = date(y, mo, 1)
            fields: dict[str, Any] = {
                "ending_balance": _num(m.get("ending_balance")),
                "avg_daily_balance": _num(m.get("average_ledger_balance")),
                "low_balance": _num(m.get("low_daily_balance")),
            }
            nsf = m.get("nsf_count", m.get("nsf_or_overdraft_count"))
            if nsf is not None:
                try:
                    fields["nsf_count"] = max(0, int(float(nsf)))
                except (TypeError, ValueError):
                    pass
            negative_balance_dates: list[str] | None = None
            raw_negative_dates = m.get("negative_balance_dates")
            if isinstance(raw_negative_dates, list):
                negative_balance_dates = []
                for raw_date in raw_negative_dates:
                    parsed = _parse_date(raw_date)
                    if parsed is not None and parsed.year == y and parsed.month == mo:
                        value = parsed.isoformat()
                        if value not in negative_balance_dates:
                            negative_balance_dates.append(value)
                fields["liquidity"] = {
                    "negative_balance_dates": negative_balance_dates,
                    "negative_balance_days": len(negative_balance_dates),
                }
            # Summary deposits/withdrawals only where the ledger has no events
            # for this month — otherwise rebuild_periods recomputes from events.
            if period not in event_periods:
                fields["deposits"] = _num(m.get("total_deposits"))
                fields["withdrawals"] = _num(m.get("total_withdrawals"))
            fields = {k: v for k, v in fields.items() if v is not None}
            if fields:
                period_upserts[period] = fields
            months_out.append(
                {
                    "month": f"{y:04d}-{mo:02d}",
                    "total_deposits": _num(m.get("total_deposits")),
                    "total_withdrawals": _num(m.get("total_withdrawals")),
                    "ending_balance": _num(m.get("ending_balance")),
                    "average_ledger_balance": _num(m.get("average_ledger_balance")),
                    "low_daily_balance": _num(m.get("low_daily_balance")),
                    "nsf_count": fields.get("nsf_count"),
                    "negative_balance_dates": negative_balance_dates,
                }
            )

    return {
        "events": events,
        "period_upserts": period_upserts,
        "event_periods": event_periods,
        "months": months_out,
        "notes": notes,
    }


# --- DB persistence through the existing engine ------------------------------


async def _persist_plan(
    db: AsyncSession,
    dealer_id: UUID,
    plan: dict[str, Any],
    account_id: UUID | None = None,
    document_id: UUID | None = None,
) -> None:
    """Persist one extraction plan, scoped to one (dealer, account) pair.
    account_id=None keeps everything in the legacy null-account scope —
    existing null-account rows are matched and preserved, never migrated.

    document_id (0119) stamps provenance onto every cash event created here —
    the "reference the PDF" backbone. CSV bulk import stays document-less."""
    for e in plan["events"]:
        db.add(
            DealerCashEvent(
                dealer_id=dealer_id,
                account_id=account_id,
                period=e["period"],
                occurred_on=e["occurred_on"],
                description=e["description"],
                amount=e["amount"],
                category=e["category"],
                flags=e["flags"],
                categorized_by=e.get("categorized_by") or "ai",
                source="document",
                document_id=document_id,
            )
        )
    await db.flush()

    account_clause = (
        DealerFinancialPeriod.account_id == account_id
        if account_id is not None
        else DealerFinancialPeriod.account_id.is_(None)
    )
    for period, fields in sorted(plan["period_upserts"].items()):
        fp = (
            await db.execute(
                select(DealerFinancialPeriod).where(
                    DealerFinancialPeriod.dealer_id == dealer_id,
                    DealerFinancialPeriod.period == period,
                    account_clause,
                )
            )
        ).scalar_one_or_none()
        if fp is None:
            fp = DealerFinancialPeriod(
                dealer_id=dealer_id, period=period, account_id=account_id, source="document"
            )
            db.add(fp)
        for k, v in fields.items():
            # Manual wins (mirrors rebuild_periods): never clobber a non-null
            # field that was manually entered.
            if fp.source == "manual" and getattr(fp, k) is not None:
                continue
            setattr(fp, k, v)
    await db.flush()

    if plan["event_periods"]:
        await rebuild_periods(db, dealer_id, plan["event_periods"], account_id=account_id)

    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer_id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after document extract for %s", dealer_id)

    # Deterministic recurrence stamping (system regulates the AI-extracted
    # data) — best-effort, a recurrence failure never fails an extraction.
    if plan["events"]:
        try:
            await stamp_recurrence(db, dealer_id)
        except Exception:
            logger.exception("dealer-os: recurrence stamp failed after document extract for %s", dealer_id)


# --- doc-type classification & routing (doc hub, 0114) -----------------------

# The model's classification vocabulary. detected_kind additionally allows
# 'archive' (set by the router for ZIP parents, never by the model).
_DOC_TYPES = {
    "bank_statement",
    "tax_return",
    "profit_and_loss",
    "balance_sheet",
    "debt_schedule",
    "loan_agreement",
    "credit_report",
    "other",
}
_MAX_TAX_YEARS = 12
_MAX_PL_MONTHS = 36
_MAX_DEBTS = 40
_DEBT_TRAILING_MONTHS = 12


def _normalize_doc_type(raw: Any) -> str | None:
    s = str(raw or "").strip().lower()
    return s if s in _DOC_TYPES else None


def _clean_tax_years(raw: Any) -> list[dict[str, Any]]:
    """[{"year": int, "revenue": float|None}], deduped, capped, sane years."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:_MAX_TAX_YEARS]:
        if not isinstance(item, dict):
            continue
        try:
            year = int(float(item.get("year")))
        except (TypeError, ValueError):
            continue
        if not 1990 <= year <= 2100 or year in seen:
            continue
        seen.add(year)
        out.append({"year": year, "revenue": _num(item.get("revenue"))})
    return out


def _clean_pl_months(raw: Any) -> list[dict[str, Any]]:
    """[{"month": "YYYY-MM", "revenue": float|None, "net_income": float|None}]."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:_MAX_PL_MONTHS]:
        if not isinstance(item, dict):
            continue
        match = _MONTH_RE.match(str(item.get("month") or "").strip())
        if not match:
            continue
        y, mo = int(match.group(1)), int(match.group(2))
        if not 1 <= mo <= 12:
            continue
        key = f"{y:04d}-{mo:02d}"
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "month": key,
            "revenue": _num(item.get("revenue")),
            "net_income": _num(item.get("net_income")),
        }
        if entry["revenue"] is None and entry["net_income"] is None:
            continue
        out.append(entry)
    return out


def _clean_debts(raw: Any) -> list[dict[str, Any]]:
    """Per-obligation rows from debt schedules AND loan/MCA agreements (0126):
    the legacy trio plus the contract's native cadence and pricing. A missing
    monthly_payment is derived from payment_amount x frequency so the metric
    engines always see a monthly figure."""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:_MAX_DEBTS]:
        if not isinstance(item, dict):
            continue
        lender = str(item.get("lender") or "").strip()[:160]
        monthly = _num(item.get("monthly_payment"))
        balance = _num(item.get("balance"))
        payment_amount = _num(item.get("payment_amount"))
        frequency = str(item.get("payment_frequency") or "").strip().lower() or None
        if frequency not in FREQUENCY_MONTHLY_MULT:
            frequency = None
        factor = _num(item.get("factor_rate"))
        if factor is not None and not (1.0 < factor <= 5.0):
            factor = None
        rate = _num(item.get("rate"))
        term_months = _num(item.get("term_months"))
        term_months = int(term_months) if term_months and 0 < term_months <= 600 else None
        payoff = _num(item.get("payoff_amount"))
        if monthly is None and payment_amount is not None and frequency:
            monthly = round(payment_amount * FREQUENCY_MONTHLY_MULT[frequency], 2)
        if not lender and monthly is None and balance is None:
            continue
        out.append(
            {
                "lender": lender or "(unnamed lender)",
                "monthly_payment": monthly,
                "balance": balance,
                "payment_amount": payment_amount,
                "payment_frequency": frequency,
                "factor_rate": factor,
                "rate": rate,
                "term_months": term_months,
                "payoff_amount": payoff,
            }
        )
    return out


def _build_doc_meta(
    doc_type: str | None,
    tax_years: list[dict[str, Any]],
    pl_months: list[dict[str, Any]],
    debts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compact classifier payload persisted on the row — the _clean_* helpers
    already capped every list/string, so this stays JSONB-friendly."""
    meta: dict[str, Any] = {"doc_type": doc_type}
    if tax_years:
        meta["tax_years"] = tax_years
    if pl_months:
        meta["pl_months"] = pl_months
    if debts:
        meta["debts"] = debts
        total = _debts_monthly_total(debts)
        if total > 0:
            meta["total_monthly_debt_service"] = total
    return meta


def _debts_monthly_total(debts: list[dict[str, Any]]) -> float:
    return round(
        sum(float(d["monthly_payment"]) for d in debts if d.get("monthly_payment") is not None), 2
    )


def merge_tax_filings(
    existing_by_year: dict[int, Any],
    tax_years: list[dict[str, Any]],
    notes: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Pure precedence-law merge of AI tax years onto the dealer's filings.

    PRODUCT LAW: a human's entry always wins and is never overwritten.
    - Missing year -> a create payload {"year", "filed": True, "revenue_reported"}.
    - Existing row -> fill ONLY NULL fields: revenue_reported is set only when
      currently None (a differing document value is noted, never applied);
      filed may move False -> True (the return itself evidences filing) but
      NEVER True -> False.

    Mutates the existing row objects in place (attribute-level, so bare test
    objects work). Returns (to_create, changed_existing_count).
    """
    to_create: list[dict[str, Any]] = []
    changed = 0
    for item in tax_years:
        year = item["year"]
        revenue = item.get("revenue")
        row = existing_by_year.get(year)
        if row is None:
            to_create.append({"year": year, "filed": True, "revenue_reported": revenue})
            continue
        row_changed = False
        current = getattr(row, "revenue_reported", None)
        if current is None:
            if revenue is not None:
                row.revenue_reported = revenue
                row_changed = True
        elif revenue is not None and abs(float(current) - float(revenue)) >= 0.01:
            if notes is not None:
                notes.append(
                    f"Tax {year}: kept existing reported revenue {float(current):,.2f} "
                    f"(document said {float(revenue):,.2f})"
                )
        if not bool(getattr(row, "filed", False)):
            row.filed = True
            row_changed = True
        if row_changed:
            changed += 1
    return to_create, changed


async def _fill_business_identity(
    db: AsyncSession, dealer_id, extraction: dict, notes: list[str]
) -> None:
    """Fill the always-required business-profile fields from what the
    document itself prints — legal name, EIN, NAICS. FILL-ONLY-NULL: anything
    the advisor or the client already entered is never overwritten. Flushes,
    never commits; failures never fail the extraction."""
    ident = extraction.get("business_identity")
    if not isinstance(ident, dict):
        return
    try:
        from ..models import DealerBusiness

        dealer = await db.get(DealerBusiness, dealer_id)
        if dealer is None:
            return
        filled = []
        for src, attr, cap in (
            ("legal_name", "legal_name", 180),
            ("ein", "ein", 24),
            ("naics_code", "naics_code", 8),
        ):
            value = ident.get(src)
            if isinstance(value, str) and value.strip() and getattr(dealer, attr, None) in (None, ""):
                setattr(dealer, attr, value.strip()[:cap])
                filled.append(attr)
        if filled:
            await db.flush()
            notes.append(f"Business profile auto-filled from the document: {', '.join(filled)}")
    except Exception:
        logger.exception("dealer-os: business-identity fill failed for dealer %s", dealer_id)


async def _route_tax_years(
    db: AsyncSession,
    dealer_id: UUID,
    tax_years: list[dict[str, Any]],
    notes: list[str],
    document_id: UUID | None = None,
) -> int:
    """Upsert dos_tax_filings from an extracted tax return (fill-only-null).

    document_id (0119): stamped on filing rows this routing creates, and
    refreshed on rows it changes — same posture as the detail refresh (the
    provenance pointer is the AI's own reading, never a human's entry)."""
    if not tax_years:
        return 0
    years = [t["year"] for t in tax_years]
    existing = (
        (
            await db.execute(
                select(DealerTaxFiling).where(
                    DealerTaxFiling.dealer_id == dealer_id, DealerTaxFiling.year.in_(years)
                )
            )
        )
        .scalars()
        .all()
    )
    before = {f.year: (f.revenue_reported, f.filed) for f in existing}
    to_create, changed = merge_tax_filings({f.year: f for f in existing}, tax_years, notes)
    if document_id is not None:
        for f in existing:
            if (f.revenue_reported, f.filed) != before[f.year]:
                f.document_id = document_id
    for payload in to_create:
        db.add(DealerTaxFiling(dealer_id=dealer_id, document_id=document_id, **payload))
    await db.flush()
    return changed + len(to_create)


async def _route_pl_months(
    db: AsyncSession, dealer_id: UUID, pl_months: list[dict[str, Any]], notes: list[str]
) -> int:
    """Upsert revenue/net_income from a monthly P&L onto dos_financial_periods.

    P&L is dealer-level: rows land in the (dealer, account NULL) scope — the
    same null-account path _persist_plan uses for unattributed documents.
    Fill-only-null: a field already populated (statement/manual entry) is kept
    and the conflict is noted, never overwritten."""
    updated = 0
    for m in pl_months:
        y, mo = (int(p) for p in m["month"].split("-"))
        period = date(y, mo, 1)
        fp = (
            await db.execute(
                select(DealerFinancialPeriod).where(
                    DealerFinancialPeriod.dealer_id == dealer_id,
                    DealerFinancialPeriod.period == period,
                    DealerFinancialPeriod.account_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if fp is None:
            fp = DealerFinancialPeriod(
                dealer_id=dealer_id, period=period, account_id=None, source="document"
            )
            db.add(fp)
        changed = False
        for field in ("revenue", "net_income"):
            value = m.get(field)
            if value is None:
                continue
            current = getattr(fp, field)
            if current is None:
                setattr(fp, field, value)
                changed = True
            elif abs(float(current) - float(value)) >= 0.01:
                notes.append(
                    f"P&L {m['month']}: kept existing {field} {float(current):,.2f} "
                    f"(document said {float(value):,.2f})"
                )
        if changed:
            updated += 1
    await db.flush()
    return updated


_DEBT_CONTRACT_FIELDS = (
    "monthly_payment",
    "balance",
    "payment_amount",
    "payment_frequency",
    "factor_rate",
    "rate",
    "term_months",
    "payoff_amount",
)


async def _route_debt_rows(
    db: AsyncSession,
    dealer_id: UUID,
    document_id: UUID | None,
    debts: list[dict[str, Any]],
    notes: list[str],
) -> int:
    """Upsert dos_debts rows from an extracted debt schedule or loan/MCA
    agreement (0126). Match on vendor identity (normalize_vendor of the
    lender name), falling back to a case-insensitive lender match. The
    precedence law holds: origin='admin' rows are NEVER touched, dismissed
    rows never resurrected; AI-drafted rows update fill-or-refresh and gain
    document provenance."""
    if not debts:
        return 0
    existing = (
        (await db.execute(select(DealerDebt).where(DealerDebt.dealer_id == dealer_id)))
        .scalars()
        .all()
    )
    keyed = [(r.vendor_key, r) for r in existing if r.vendor_key]
    by_name = {r.lender.strip().casefold(): r for r in existing}
    touched = 0
    for item in debts:
        lender = item["lender"]
        key = normalize_vendor(lender) or None
        # containment-tolerant match so a contract's "Forward Financing" folds
        # into the activity-drafted "ACH FORWARD FINANCING" row (no duplicates)
        row = by_name.get(lender.strip().casefold())
        if row is None and key:
            row = next((r for k, r in keyed if key_matches(k, key)), None)
        if row is not None:
            if row.origin == "admin" or row.status == "dismissed":
                continue  # a human owns it (or killed it) — extraction never wins
            changed = False
            for field in _DEBT_CONTRACT_FIELDS:
                value = item.get(field)
                if value is not None and getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            if document_id is not None and row.document_id != document_id:
                row.document_id = document_id
                changed = True
            if changed:
                touched += 1
        else:
            new_row = DealerDebt(
                dealer_id=dealer_id,
                lender=lender[:180],
                category="loan",
                origin="ai_draft",
                status="active",
                vendor_key=key,
                document_id=document_id,
                evidence={"source": "document_extraction"},
                **{f: item.get(f) for f in _DEBT_CONTRACT_FIELDS},
            )
            db.add(new_row)
            if key:
                keyed.append((key, new_row))
            by_name[lender.strip().casefold()] = new_row
            touched += 1
    await db.flush()
    if touched:
        notes.append(f"Debt schedule: created/updated {touched} obligation row(s) (admin rows untouched).")
    return touched


async def _route_debt_schedule(
    db: AsyncSession, dealer_id: UUID, debts: list[dict[str, Any]], notes: list[str]
) -> int:
    """Apply a debt schedule's total monthly debt service to the dealer's
    EXISTING trailing period months — one row per month (debt_service is
    preference-merged across account rows in the engine, never summed), only
    where currently NULL. Never creates period rows and never overwrites a
    value that statements or a human already set."""
    total = _debts_monthly_total(debts)
    if total <= 0:
        notes.append("Debt schedule stored — no monthly payment amounts to apply.")
        return 0
    rows = (
        (
            await db.execute(
                select(DealerFinancialPeriod)
                .where(DealerFinancialPeriod.dealer_id == dealer_id)
                .order_by(DealerFinancialPeriod.period.desc())
                .limit(36)
            )
        )
        .scalars()
        .all()
    )
    by_month: dict[date, list[DealerFinancialPeriod]] = {}
    for r in rows:
        by_month.setdefault(r.period, []).append(r)
    updated = 0
    for month in sorted(by_month, reverse=True)[:_DEBT_TRAILING_MONTHS]:
        # Dealer-level number: prefer the null-account row as the carrier.
        target = sorted(by_month[month], key=lambda r: 0 if r.account_id is None else 1)[0]
        if target.debt_service is None:
            target.debt_service = total
            updated += 1
    await db.flush()
    if updated:
        notes.append(
            f"Applied {total:,.2f}/mo total debt service to {updated} month(s) "
            "(only months that had no debt service yet)."
        )
    else:
        notes.append(
            f"Debt schedule total {total:,.2f}/mo — every existing month already "
            "has debt service, nothing overwritten."
        )
    return updated


# --- PDF/image path: same Bedrock vision model as the intake pipeline --------

_EXTRACT_SYSTEM = """You are a financial-document extraction engine for commercial underwriting.
The user message contains ONE document (bank statement, tax return, P&L / income statement, balance sheet, debt schedule, credit report, ...) as a PDF or image.
Read EVERY page/month — never summarize only the first month.

Return ONLY strict JSON (no markdown, no commentary) with exactly this shape:
{
  "doc_type": "bank_statement|tax_return|profit_and_loss|balance_sheet|debt_schedule|loan_agreement|credit_report|other",
  "months": [
    {"month": "YYYY-MM", "total_deposits": number|null, "total_withdrawals": number|null,
     "ending_balance": number|null, "average_ledger_balance": number|null,
     "low_daily_balance": number|null, "nsf_count": number|null,
     "negative_balance_dates": ["YYYY-MM-DD"]}
  ],
  "transactions": [
    {"date": "YYYY-MM-DD", "description": "string", "amount": number}
  ],
  "account": {"institution": "string|null", "name_hint": "string|null",
              "mask": "string|null", "kind_hint": "string|null"},
  "tax_years": [{"year": number, "revenue": number|null}],
  "business_identity": {"legal_name": "string|null", "ein": "string|null", "naics_code": "string|null"},
  "pl_months": [{"month": "YYYY-MM", "revenue": number|null, "net_income": number|null}],
  "debts": [{"lender": "string", "monthly_payment": number|null, "balance": number|null,
             "payment_amount": number|null, "payment_frequency": "daily|weekly|biweekly|monthly|null",
             "factor_rate": number|null, "rate": number|null, "term_months": number|null,
             "payoff_amount": number|null}]
}

Rules:
- "doc_type" is REQUIRED: classify what the document actually IS, regardless of what the uploader called it. Use "other" only when none of the listed types fits.
- months[] and transactions[] are for BANK STATEMENTS: one months[] entry per statement month present in the document. For any non-statement document return "months": [] and "transactions": [].
- negative_balance_dates must list each calendar date whose end-of-day balance is visibly negative. Return [] only when the full statement establishes there were none; use null when daily balances are not readable enough to determine this.
- "account" is optional and best-effort: identify the bank account the statement belongs to. institution = bank name as printed; name_hint = account title/product name (e.g. "Business Complete Checking", "Payroll Account"); mask = LAST 4 digits of the account number only; kind_hint = one of checking|savings|payroll|other if stated. Omit "account" (or use nulls) when the document is not a bank statement or the fields are not visible. Never invent account details.
- "tax_years" is for TAX RETURNS: one entry per tax year covered, revenue = gross receipts / total revenue as reported on the return (null when not stated). [] for other documents.
- "business_identity" is best-effort from ANY document that states it (tax returns and statements print these): legal_name = the entity's legal name as printed; ein = employer identification number formatted XX-XXXXXXX; naics_code = the business activity code when the return shows one. Null anything not clearly printed — never guess.
- "pl_months" is for P&L / INCOME STATEMENTS with a monthly breakdown: one entry per month with revenue and net_income when stated. If the P&L is annual/quarterly only, return [] rather than inventing a monthly split.
- "debts" is for DEBT SCHEDULES and LOAN/MCA AGREEMENTS: one entry per obligation — lender/funder as printed, monthly_payment = the recurring MONTHLY payment (convert only when the document states the payment frequency; null when unknown), balance = current outstanding balance.
- "loan_agreement" covers loan notes, merchant cash advance (MCA) agreements, and financing contracts for a SINGLE obligation. For these also fill: payment_amount = the payment in the contract's own cadence (e.g. the daily remittance), payment_frequency = that cadence, factor_rate = the MCA factor (payback / advance, e.g. 1.38) when stated, rate = the annual interest rate as a percent when stated, term_months = the stated term, payoff_amount = the stated payoff/balance when printed. Never derive factor_rate and rate from each other.
- Every number is a bare number: no currency symbols, no commas. Withdrawals in total_withdrawals are a positive magnitude.
- transactions[] is best-effort: include individual lines when they are legible, with deposits positive and withdrawals/debits NEGATIVE. If lines are not reliably legible, return "transactions": [].
- Use null for any field the document does not state. Never invent numbers.
- If the document contains no extractable financial data at all, still return the classified doc_type with every array empty."""


def _media_type(content_type: str, filename: str) -> str | None:
    lower = f"{content_type} {filename}".lower()
    if "application/pdf" in lower or lower.endswith(".pdf"):
        return "application/pdf"
    if "image/jpeg" in lower or lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if "image/png" in lower or lower.endswith(".png"):
        return "image/png"
    if "image/gif" in lower or lower.endswith(".gif"):
        return "image/gif"
    if "image/webp" in lower or lower.endswith(".webp"):
        return "image/webp"
    return None


def _parse_model_json(text: str) -> dict[str, Any]:
    """Defensive JSON parse: strip code fences, then fall back to the outermost
    {...} slice. Raises ValueError with a clear message when unusable."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned)
    for candidate in (cleaned, cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            continue
    raise ValueError("Model did not return parseable extraction JSON")


async def _extract_via_model(
    db: AsyncSession, doc: DealerDocument, raw: bytes, media: str
) -> dict[str, Any]:
    encoded = base64.b64encode(raw).decode("ascii")
    block_type = "document" if media == "application/pdf" else "image"
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"Document: {doc.filename} (declared kind: {doc.kind}). "
            "Classify the document type and extract its financial data. Return only the required JSON.",
        },
        {"type": block_type, "source": {"type": "base64", "media_type": media, "data": encoded}},
    ]
    model = model_heavy()
    resp = await tracked_messages_create(
        db,
        feature="dealer_os_document_extract",
        client=get_client(),
        model=model,
        metadata={"dealer_id": str(doc.dealer_id), "dos_document_id": str(doc.id)},
        max_tokens=8000,
        system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(
        getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text"
    )
    return _parse_model_json(text)


# --- orchestrator ------------------------------------------------------------


def _is_csv(content_type: str, filename: str) -> bool:
    lower = f"{content_type} {filename}".lower()
    return "csv" in lower or lower.endswith((".csv", ".tsv", ".txt"))


def _is_xlsx(content_type: str, filename: str) -> bool:
    lower = f"{content_type} {filename}".lower()
    return "spreadsheetml" in lower or lower.endswith(".xlsx")


async def store_document_bytes(
    db: AsyncSession,
    dealer_id: UUID,
    raw: bytes,
    filename: str,
    content_type: str = "application/pdf",
    *,
    kind: str = "statement",
    plaid_statement_id: str | None = None,
    plaid_item_id: UUID | None = None,
) -> DealerDocument:
    """Create a DealerDocument from server-held bytes (no HTTP upload).

    The shared core the Plaid statement puller uses (and the shape the three
    inline copies in router.py should eventually converge on): S3 store via
    the standard key layout, row construction, flush. The caller runs
    extract_document and owns the commit."""
    from . import storage  # local import keeps module import order stable

    safe = storage.safe_filename(filename)
    key = storage.build_key(dealer_id, safe)
    s3_key = key if storage.put_bytes(key, raw, content_type) else None
    doc = DealerDocument(
        dealer_id=dealer_id,
        filename=safe[:260],
        content_type=content_type[:120],
        size_bytes=len(raw),
        s3_key=s3_key,
        kind=kind,
        status="uploaded",
        plaid_statement_id=plaid_statement_id,
        plaid_item_id=plaid_item_id,
    )
    db.add(doc)
    await db.flush()
    return doc


async def extract_document(
    db: AsyncSession, doc: DealerDocument, raw: bytes | None = None, account_id: UUID | None = None
) -> dict[str, Any]:
    """Extract + normalize one document through the engine pipeline.

    raw may be passed directly (upload path — bytes are already in hand);
    otherwise they are fetched from the S3 archive via doc.s3_key. Updates
    doc.status/extracted/error in place. Flushes, never commits.

    Phase 3 account threading: an explicit account_id (admin picked one at
    upload) wins outright; otherwise, when the model returns an "account" hint
    for a statement, match_or_create_account resolves/creates the dos_accounts
    row (AI proposal; admin roles never overwritten). The resolved account is
    stamped onto the document, its cash events, and its period upserts, and
    the rebuild runs scoped to that (dealer, account) pair — documents with no
    account signal stay in the legacy null-account scope.
    """
    doc.status = "extracting"
    doc.error = None
    await db.flush()
    try:
        if raw is None:
            if not doc.s3_key:
                raise ValueError(
                    "Original file bytes are not archived (no s3_key) — re-upload the document to extract it."
                )
            raw = storage.get_bytes(doc.s3_key)
            if raw is None:
                raise ValueError("Could not fetch the archived file from S3 — re-upload the document.")

        notes: list[str] = []
        if _is_xlsx(doc.content_type, doc.filename):
            txns, errors = parse_xlsx_bytes(raw)
            extraction: dict[str, Any] = {"months": [], "transactions": txns}
            notes.extend(errors)
            source = "xlsx"
        elif _is_csv(doc.content_type, doc.filename):
            txns, errors = parse_csv_bytes(raw)
            extraction = {"months": [], "transactions": txns}
            notes.extend(errors)
            source = "csv"
        else:
            media = _media_type(doc.content_type, doc.filename)
            if media is None:
                raise ValueError(
                    f"Unsupported document type ({doc.content_type or 'unknown'}) — "
                    "upload a CSV, XLSX, PDF, or image."
                )
            extraction = await _extract_via_model(db, doc, raw, media)
            source = "model"

        rules = await load_active_rules(db, doc.dealer_id)
        plan = apply_extraction(extraction, rules=rules)
        notes.extend(plan["notes"])

        # --- classify (doc hub, 0114) ------------------------------------
        has_statement_data = bool(plan["events"] or plan["period_upserts"])
        if source == "model":
            detected = _normalize_doc_type(extraction.get("doc_type"))
            classified = detected is not None
            tax_years = _clean_tax_years(extraction.get("tax_years"))
            pl_months = _clean_pl_months(extraction.get("pl_months"))
            debts = _clean_debts(extraction.get("debts"))
            if detected is None:
                # No usable label — fall back to whichever payload the model
                # actually produced, so real data still routes.
                if has_statement_data:
                    detected = "bank_statement"
                elif tax_years:
                    detected = "tax_return"
                elif pl_months:
                    detected = "profit_and_loss"
                elif debts:
                    detected = "debt_schedule"
        else:
            # CSV/XLSX are transaction imports by definition — but a parser
            # label is not a model classification, so an empty file still fails.
            detected, classified = "bank_statement", False
            tax_years, pl_months, debts = [], [], []

        # Validity: fail only when there is neither statement data, nor tax
        # years, nor P&L months, nor debts, nor a recognized classification.
        if (
            not has_statement_data
            and not tax_years
            and not pl_months
            and not debts
            and not classified
        ):
            raise ValueError(
                "No usable financial data found in the document"
                + (f" ({'; '.join(notes[:3])})" if notes else "")
            )

        doc.detected_kind = detected
        doc.doc_meta = _build_doc_meta(detected, tax_years, pl_months, debts)

        # --- route by detected type --------------------------------------
        if detected == "bank_statement":
            # Existing statement pipeline, unchanged. Resolve the bank
            # account: explicit admin choice wins; else the model's account
            # hint drives match_or_create (AI proposal only — admin-set roles
            # are never overwritten by a rematch).
            resolved_account_id = account_id
            account_hint = extraction.get("account")
            if resolved_account_id is None and isinstance(account_hint, dict) and any(
                str(account_hint.get(k) or "").strip()
                for k in ("institution", "name_hint", "mask", "kind_hint")
            ):
                try:
                    account = await match_or_create_account(
                        db, doc.dealer_id, account_hint, plan["months"]
                    )
                    resolved_account_id = account.id
                    notes.append(
                        f"Matched to account '{account.name}'"
                        + (f" ****{account.mask}" if account.mask else "")
                    )
                except Exception:
                    logger.exception("dealer-os: account match failed for document %s", doc.id)
            doc.account_id = resolved_account_id
            await _persist_plan(
                db, doc.dealer_id, plan, account_id=resolved_account_id, document_id=doc.id
            )
        elif detected == "tax_return":
            upserted = await _route_tax_years(db, doc.dealer_id, tax_years, notes, document_id=doc.id)
            await _fill_business_identity(db, doc.dealer_id, extraction, notes)
            notes.append(f"Tax return: upserted {upserted} tax year(s) (existing entries kept).")
        elif detected == "profit_and_loss":
            updated = await _route_pl_months(db, doc.dealer_id, pl_months, notes)
            notes.append(f"P&L: filled revenue/net income on {updated} month(s).")
            try:
                await recompute_snapshot(db, doc.dealer_id)
            except Exception:
                logger.exception(
                    "dealer-os: snapshot recompute failed after P&L extract for %s", doc.dealer_id
                )
        elif detected == "debt_schedule":
            await _route_debt_schedule(db, doc.dealer_id, debts, notes)
            await _route_debt_rows(db, doc.dealer_id, doc.id, debts, notes)
            try:
                await recompute_snapshot(db, doc.dealer_id)
            except Exception:
                logger.exception(
                    "dealer-os: snapshot recompute failed after debt-schedule extract for %s",
                    doc.dealer_id,
                )
        elif detected == "loan_agreement":
            # One contract != total debt service: create/refresh the obligation
            # row(s) only — never stamp period-level totals from a single note.
            await _route_debt_rows(db, doc.dealer_id, doc.id, debts, notes)
            notes.append("Loan/MCA agreement: obligation captured on the debt schedule.")
            try:
                await recompute_snapshot(db, doc.dealer_id)
            except Exception:
                logger.exception(
                    "dealer-os: snapshot recompute failed after loan-agreement extract for %s",
                    doc.dealer_id,
                )
        else:
            # balance_sheet | credit_report | other — a classified document
            # with a stored summary is a SUCCESS, not a failure.
            notes.append(
                f"Classified as {detected or 'unrecognized'} — summary stored, no ledger rows written."
            )

        doc.extracted = {
            "months": plan["months"],
            "transactions_count": len(plan["events"]),
            "notes": notes[:50],
            "parser": source,
            "doc_type": detected,
        }
        doc.status = "extracted"
        doc.error = None
    except Exception as exc:
        logger.exception("dealer-os: document extraction failed for %s", doc.id)
        doc.status = "failed"
        doc.error = str(exc)[:2000]
        try:
            await db.flush()
        except Exception:
            # A failed persist can leave the session unusable — roll back the
            # partial ingest and re-record just the document failure (same PK).
            await db.rollback()
            db.add(doc)
            await db.flush()
        return {}
    await db.flush()
    return doc.extracted or {}
