"""Scoped evidence suggestions; observed bank outflows are never booked expenses."""

from __future__ import annotations

import math
from typing import Any


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def _first(row, *keys):
    return next((row[key] for key in keys if row.get(key) is not None), None)


def build_evidence(analyses: list[dict], cockpit: dict, *, files=None, offer=None) -> dict[str, Any]:
    inventory = []
    by_id = {str(row["file_id"]): row for row in analyses}
    raw_files = files if files is not None else analyses
    for raw in raw_files:
        get = raw.get if isinstance(raw, dict) else lambda key, default=None, item=raw: getattr(item, key, default)
        file_id = str(get("file_id") or get("id"))
        row = by_id.get(file_id, {})
        if row.get("classification") == "operating_plan":
            continue
        association = row.get("identity_association") or {}
        inventory.append({"file_id": file_id, "file_name": get("file_name") or row.get("file_name"),
            "classification": row.get("classification"), "analysis_id": row.get("analysis_id"),
            "content_hash": get("content_hash") or row.get("content_hash"),
            "association_status": association.get("status", "unconfirmed"),
            "association_stamp": association.get("association_stamp"),
            "eligible_for_review": bool(association.get("allows_financial_use") and row.get("accepted")
                and row.get("analysis_id") and row.get("content_hash") and get("status", "uploaded") != "rejected")})
    inventory_by_id = {row["file_id"]: row for row in inventory}
    sources, candidates, warnings = [], [], []
    for analysis in analyses:
        classes = set(analysis.get("classifications") or []) | {analysis.get("classification")}
        if "merchant_processing_statement" not in classes:
            continue
        facts = analysis.get("key_facts") or {}
        data = facts.get("merchant_processing") if isinstance(facts.get("merchant_processing"), dict) else facts
        volume = number(_first(data, "card_volume", "processing_volume", "total_card_volume", "gross_card_sales"))
        fees = number(_first(data, "total_processing_fees", "processing_fees", "total_fees"))
        rate = number(_first(data, "effective_rate_pct", "processing_rate_pct"))
        basis = data.get("rate_basis") if data.get("rate_basis") in {"all_in", "processor_markup"} else "unknown"
        if rate is None and volume and fees is not None and data.get("fees_complete") is True:
            rate, basis = fees / volume * 100, "all_in"
        processor = str(_first(data, "processor", "processor_name") or "").strip()
        account = str(_first(data, "merchant_account_last4", "account_last4", "merchant_id_last4") or "").strip()
        currency = str(data.get("currency") or facts.get("currency") or "").upper()
        fid = str(analysis["file_id"])
        known_group = bool(processor and account and currency)
        source_key = f"{processor.casefold()}:{account}:{currency}" if known_group else f"unidentified:{fid}"
        notes = []
        if not known_group:
            notes.append("Confirm processor and merchant account before combining statements or claiming savings.")
        if basis != "all_in":
            notes.append("Confirm complete processing costs; processor markup alone is not an all-in effective rate.")
        if volume is None:
            notes.append("Card volume is unknown; bank deposits or net settlements cannot substitute for card sales.")
        sources.append({"source_key": source_key, "file_id": fid, "file_name": analysis.get("file_name"),
            "processor": processor or None, "account_last4": account or None, "currency": currency or None,
            "period_start": data.get("period_start") or facts.get("period_start"),
            "period_end": data.get("period_end") or facts.get("period_end"), "volume": volume,
            "total_fees": fees, "transactions": number(_first(data, "transaction_count", "transactions")),
            "effective_rate_pct": rate, "rate_basis": basis, "warnings": notes,
            "fixed_fee": number(data.get("fixed_fee")), "per_transaction_fee": number(data.get("per_transaction_fee")),
            "eligible_for_review": bool(inventory_by_id.get(fid, {}).get("eligible_for_review") and known_group),
            "source_pages": data.get("source_pages") or [], "_order": analysis.get("analyzed_at") or ""})
    # One latest statement per identified processor/account; historical months remain evidence links.
    latest = {}
    for row in sources:
        order = (str(row.get("period_end") or ""), row["_order"], row["file_id"])
        old = latest.get(row["source_key"])
        if old is None or order > old[0]:
            latest[row["source_key"]] = (order, row)
    sources = [row for _order, row in latest.values()]
    for row in sources:
        row.pop("_order", None)
        if row["total_fees"] is not None:
            candidates.append({"key": "merchant:" + row["source_key"], "label": f"{row['processor'] or 'Card processor'} processing fees",
                "payee": row["processor"] or "", "category": "merchant_processing", "current_amount": row["total_fees"],
                "cadence": "monthly", "source_file_ids": [row["file_id"]],
                "source_note": "Statement-period processing fees; verify a complete month and reconcile against the earnings baseline.",
                "merchant_source_key": row["source_key"], "warning": "Confirm statement period before using a monthly amount."})
    for row in ((cockpit.get("bank") or {}).get("recurring") or {}).get("payees", []):
        if row.get("is_debt") or row.get("is_card") or row.get("kind") in {"transfer", "internal_transfer", "owner_draw", "loan_payment", "tax"}:
            continue
        amount = number(row.get("monthly_average"))
        if amount is None:
            continue
        candidates.append({"key": "bank:" + str(row.get("key")), "label": row.get("payee") or "Recurring bank payment",
            "payee": row.get("payee") or "", "category": "other", "current_amount": amount, "cadence": "monthly",
            "source_file_ids": [str(fid) for fid in row.get("file_ids", []) if str(fid) in inventory_by_id],
            "bank_vendor_key": row.get("key"), "source_note": "Observed recurring bank payments; verify operating purpose, coverage and baseline inclusion.",
            "warning": "Observed payment suggestion, not a verified operating expense."})
    offers = []
    if offer is not None and str(offer.source_file_id) in inventory_by_id:
        from app.services.merchant_processing import clean_terms

        offers = [{"id": str(offer.id), "source_file_id": str(offer.source_file_id), "status": offer.status,
                   "terms_version": offer.terms_version, **clean_terms(offer.terms or {})}]
    return {"files": sorted(inventory, key=lambda row: row["file_id"]),
            "expense_candidates": sorted(candidates, key=lambda row: row["key"])[:500],
            "merchant_sources": sorted(sources, key=lambda row: row["source_key"]),
            "merchant_offers": offers, "warnings": warnings}
