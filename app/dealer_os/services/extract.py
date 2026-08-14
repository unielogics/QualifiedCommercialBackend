"""Document extraction — the source-agnostic ingestion contract.

Every document (CSV/XLSX parsed in pure python; PDF/image read by the SAME
Bedrock vision model the intake pipeline uses) is reduced to one canonical
extraction dict:

    {
      "months": [{"month": "YYYY-MM", "total_deposits", "total_withdrawals",
                  "ending_balance", "average_ledger_balance",
                  "low_daily_balance", "nsf_count"}],
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

from ..models import DealerCashEvent, DealerDocument, DealerFinancialPeriod
from .engines import recompute_snapshot
from .normalize import classify_event, period_of, rebuild_periods
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


def apply_extraction(extraction: dict[str, Any]) -> dict[str, Any]:
    """Transform one canonical extraction dict into the exact rows the engine
    ingests. Pure — returns a plan, touches no DB:

    {
      "events":  [{occurred_on, period, description, amount, category, flags}],
      "period_upserts": {date(period): {deposits?, withdrawals?, ending_balance?,
                                        low_balance?, avg_daily_balance?, nsf_count?}},
      "event_periods": set[date],   # months whose ledger changed -> rebuild_periods
      "months": [...],              # normalized month summaries for doc.extracted
      "notes": [...],
    }

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
            category, flags = classify_event(description, amount)
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


async def _persist_plan(db: AsyncSession, dealer_id: UUID, plan: dict[str, Any]) -> None:
    for e in plan["events"]:
        db.add(
            DealerCashEvent(
                dealer_id=dealer_id,
                period=e["period"],
                occurred_on=e["occurred_on"],
                description=e["description"],
                amount=e["amount"],
                category=e["category"],
                flags=e["flags"],
                categorized_by="ai",
                source="document",
            )
        )
    await db.flush()

    for period, fields in sorted(plan["period_upserts"].items()):
        fp = (
            await db.execute(
                select(DealerFinancialPeriod).where(
                    DealerFinancialPeriod.dealer_id == dealer_id,
                    DealerFinancialPeriod.period == period,
                )
            )
        ).scalar_one_or_none()
        if fp is None:
            fp = DealerFinancialPeriod(dealer_id=dealer_id, period=period, source="document")
            db.add(fp)
        for k, v in fields.items():
            # Manual wins (mirrors rebuild_periods): never clobber a non-null
            # field that was manually entered.
            if fp.source == "manual" and getattr(fp, k) is not None:
                continue
            setattr(fp, k, v)
    await db.flush()

    if plan["event_periods"]:
        await rebuild_periods(db, dealer_id, plan["event_periods"])

    # Best-effort engine refresh — never fail the ingest on engine errors.
    try:
        await recompute_snapshot(db, dealer_id)
    except Exception:
        logger.exception("dealer-os: snapshot recompute failed after document extract for %s", dealer_id)


# --- PDF/image path: same Bedrock vision model as the intake pipeline --------

_EXTRACT_SYSTEM = """You are a financial-document extraction engine for commercial underwriting.
The user message contains ONE document (a bank statement, P&L, tax return, or debt schedule) as a PDF or image.
Read EVERY page/month — never summarize only the first month.

Return ONLY strict JSON (no markdown, no commentary) with exactly this shape:
{
  "months": [
    {"month": "YYYY-MM", "total_deposits": number|null, "total_withdrawals": number|null,
     "ending_balance": number|null, "average_ledger_balance": number|null,
     "low_daily_balance": number|null, "nsf_count": number|null}
  ],
  "transactions": [
    {"date": "YYYY-MM-DD", "description": "string", "amount": number}
  ]
}

Rules:
- One months[] entry per statement month present in the document.
- Every number is a bare number: no currency symbols, no commas. Withdrawals in total_withdrawals are a positive magnitude.
- transactions[] is best-effort: include individual lines when they are legible, with deposits positive and withdrawals/debits NEGATIVE. If lines are not reliably legible, return "transactions": [].
- Use null for any month field the document does not state. Never invent numbers.
- If the document contains no monthly financial data at all, return {"months": [], "transactions": []}."""


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
            "Extract the monthly financials and transactions. Return only the required JSON.",
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


async def extract_document(
    db: AsyncSession, doc: DealerDocument, raw: bytes | None = None
) -> dict[str, Any]:
    """Extract + normalize one document through the engine pipeline.

    raw may be passed directly (upload path — bytes are already in hand);
    otherwise they are fetched from the S3 archive via doc.s3_key. Updates
    doc.status/extracted/error in place. Flushes, never commits.
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

        plan = apply_extraction(extraction)
        notes.extend(plan["notes"])
        if not plan["events"] and not plan["period_upserts"]:
            raise ValueError(
                "No usable financial data found in the document"
                + (f" ({'; '.join(notes[:3])})" if notes else "")
            )
        await _persist_plan(db, doc.dealer_id, plan)

        doc.extracted = {
            "months": plan["months"],
            "transactions_count": len(plan["events"]),
            "notes": notes[:50],
            "parser": source,
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
