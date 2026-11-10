"""Explicit, audited annual NOI bases, separate from extracted P&L history.

A business tax return can suggest a tax-derived operating-income estimate.
It never becomes a P&L, EBITDA, or an accepted program-eligibility fact.
"""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from app.enums import Role
from app.services.financial_metric_review import number, stamp


def _year(value):
    match = re.search(r"\b((?:19|20)\d{2})\b", str(value or ""))
    return int(match.group(1)) if match else None


def _money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def catalog(analyses, candidates, *, entity=None):
    from app.services.document_associations import identity_matches, source_excluded
    from app.services.financial_fact_citations import (
        financial_analysis_citation_status,
        validated_fact_citations_for_analysis,
    )
    from app.services.financial_noi import PL_CLASSES, TAX_CLASSES, _entity, _source_refs
    from app.services.financial_tax_earnings import (
        _first,
        normalize_tax_earnings,
        tax_source_is_business,
    )

    pnl = {str(row["source_refs"][0]["file_id"]): row for row in candidates if row.get("source_refs")}
    sources = []
    for row in analyses:
        classes = {row.get("classification"), *(row.get("classifications") or [])}
        facts = row.get("key_facts") or {}
        is_tax = bool(classes.intersection(TAX_CLASSES))
        if not is_tax and not classes.intersection(PL_CLASSES):
            continue
        business_tax = tax_source_is_business(facts) and "personal_tax_return" not in classes
        observed = _entity(facts)
        reason = ("Personal or unscoped tax returns cannot establish business NOI." if is_tax and not business_tax else
                  "Review this document's business association in Evidence first." if source_excluded(row.get("identity_association")) else
                  "This source does not establish the file's business identity." if not entity or not identity_matches(entity, observed, row.get("identity_association")) else None)
        candidate = pnl.get(str(row.get("file_id")))
        tax = normalize_tax_earnings(facts) if is_tax and business_tax else {}
        first = tax.get("period_start") if is_tax else (candidate or {}).get("period_start")
        last = tax.get("period_end") if is_tax else (candidate or {}).get("period_end")
        year = _year(last) or _year(facts.get("tax_year") or facts.get("fiscal_year") or facts.get("return_year"))
        notes = []
        if is_tax:
            income, income_paths = _first(facts, ("ordinary_business_income", "ordinary_business_income_loss", "ordinary_business_income_loss_current"))
            interest = number(tax.get("interest"))
            proposed = _money(income + interest) if income is not None and interest is not None else None
            notes.append("Tax-derived estimate: ordinary business tax income plus known deducted interest. Depreciation and amortization remain expenses; tax/book differences and non-operating items require staff review.")
            if not first or not last:
                notes.append("The return's exact reporting dates are not established. Confirm that the reviewed amount represents the stated annual period; no automatic annualization is applied.")
            if income is None or interest is None:
                notes.append("Tax income or interest is unknown. Enter a supported annual amount after reviewing the return; missing deductions are not assumed to be zero.")
            operands = {"income": income, "interest": interest, "income_fact_paths": income_paths,
                        "interest_fact_paths": tax.get("component_fact_paths", {}).get("interest", [])}
        else:
            proposed = number((candidate or {}).get("annualized_value"))
            operands = {"candidate": candidate}
            notes.extend((candidate or {}).get("notes", []))
        analysis = SimpleNamespace(id=row.get("analysis_id"), bucket_file_id=row.get("file_id"),
            content_hash=row.get("content_hash"), status="completed",
            analysis={"key_facts": facts, "financial_fact_citations": row.get("financial_fact_citations")})
        citation_status = financial_analysis_citation_status(analysis)
        citations = [citation for values in validated_fact_citations_for_analysis(analysis).values() for citation in values]
        if not citations:
            notes.append("No current server-validated source quotations support this extraction. Inspect the source document; legacy extractions may require explicit reanalysis.")
        if not row.get("accepted"):
            notes.append("The source document is not accepted. Saving this staff NOI review does not accept the document.")
        currency = tax.get("currency") if is_tax else (candidate or {}).get("currency")
        basis = tax.get("basis") if is_tax else (candidate or {}).get("basis")
        sources.append({"file_id": str(row.get("file_id")), "file_name": row.get("file_name"),
            "kind": "business_tax_return" if is_tax else "profit_and_loss", "year": year,
            "currency": currency, "basis": basis, "period_start": first, "period_end": last,
            "proposed_annual_value": proposed, "eligible": reason is None, "reason": reason,
            "accepted": bool(row.get("accepted")), "notes": notes, "citation_status": citation_status,
            "source_refs": [{**ref, "citations": citations,
                "pages": sorted({citation["page"] for citation in citations if isinstance(citation.get("page"), int)})}
                for ref in _source_refs(row, facts)],
            "source_stamp": stamp({"file_id": row.get("file_id"), "analysis": row.get("analysis_id"),
                "hash": row.get("content_hash"), "facts": facts, "accepted": row.get("accepted"),
                "association": row.get("identity_association"), "entity": entity, "operands": operands,
                "citation_manifest": row.get("financial_fact_citations")})})
    return sorted(sources, key=lambda row: (row.get("year") or 0, row["file_id"]), reverse=True)


def prepare(analyses, candidates, *, state=None, entity=None):
    state = state or {}
    sources = catalog(analyses, candidates, entity=entity)
    source_stamp = stamp([{key: row[key] for key in ("file_id", "source_stamp", "eligible")} for row in sorted(sources, key=lambda row: row["file_id"])])
    history = [event for event in state.get("events", []) if event.get("source_metric") == "noi"]
    current = history[-1] if history else None
    return {"revision": state.get("revision", 0), "source_stamp": source_stamp,
            "mode": current.get("mode", "automatic") if current else "automatic",
            "stale": bool(current and current.get("mode") != "automatic" and current.get("source_stamp") != source_stamp),
            "sources": sources, "current": current, "history": history}


def effective(review):
    """Project only a current explicit review; callers retain source history."""
    current = review.get("current")
    if not current or current["mode"] == "automatic" or review["stale"]:
        return None
    source = next((row for row in review["sources"] if row["file_id"] == current.get("selected_file_id")), None)
    tax = bool(source and source["kind"] == "business_tax_return")
    kind = "tax_derived_noi" if tax else "staff_reviewed_noi"
    label = "Tax-derived NOI estimate (staff reviewed)" if tax else "Staff-reviewed business NOI"
    first, last = current.get("period_start"), current.get("period_end")
    period = f"{first} – {last} (annual staff review)" if first and last else f"Year {current['year']} (annual staff review)"
    notes = ["Explicit annual staff NOI review. Source documents, P&L history, EBITDA and program qualification remain independent."]
    if tax:
        notes.append("Tax-derived operating-income estimate; not a historical P&L result. Tax/book differences and non-operating items were subject to staff review; D&A is not automatically added back.")
    if not source:
        notes.append("Manual annual NOI supplied by staff without a linked source document; review provenance is retained in the internal audit.")
    elif not source["accepted"]:
        notes.append("Linked evidence has not been accepted; this NOI review does not accept or verify that evidence.")
    return {"key": f"noi-review:{current['id']}", "label": label, "kind": kind,
        "value": current["annual_value"], "annualized_value": current["annual_value"],
        "year": str(current["year"]), "period": period, "period_start": first, "period_end": last,
        "months_covered": 12, "annualization_factor": 1, "annualized_run_rate": False,
        "currency": current["currency"], "basis": (source or {}).get("basis") or "staff_reviewed", "complete": True, "provisional": False,
        "entity_comparable": True, "entity_mismatch": False, "reconciliation_required": False,
        "formula": "staff-reviewed annual NOI", "staff_adjusted": True, "staff_reviewed": True,
        "tax_derived_estimate": tax, "source_accepted": bool(source and source["accepted"]),
        "source_value": source.get("proposed_annual_value") if source else None,
        "reviewed_value": current["annual_value"], "input_keys": [], "active_input_keys": [],
        "fundamentals": [], "missing_inputs": [], "notes": notes,
        "source_refs": deepcopy(source["source_refs"]) if source else [],
        "review_receipt": {key: current[key] for key in ("id", "revision", "at", "mode")}}


def change(state, review, *, actor, mode, selected_file_id=None, annual_value=None, year=None, currency=None,
           period_start=None, period_end=None, reason, expected_revision, source_stamp, idempotency_key, now=None):
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    selected_file_id = str(selected_file_id) if selected_file_id else None
    currency = str(currency or "").strip().upper() or None
    request = {"mode": mode, "selected_file_id": selected_file_id, "annual_value": annual_value,
        "year": year, "currency": currency, "period_start": str(period_start) if period_start else None,
        "period_end": str(period_end) if period_end else None, "reason": reason.strip(),
        "expected_revision": expected_revision, "source_stamp": source_stamp}
    request_stamp = stamp(request)
    for event in state.get("events", []):
        if event.get("idempotency_key") == idempotency_key:
            if event.get("request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for a different NOI review")
            return state, False
    if state.get("revision", 0) != expected_revision:
        raise HTTPException(409, "Financial review changed; reload before saving NOI")
    if source_stamp != review["source_stamp"]:
        raise HTTPException(409, "Financial source evidence changed; review NOI again before saving")
    if mode not in {"automatic", "source", "manual"}:
        raise HTTPException(422, "Unknown NOI review mode")
    if not request["reason"] or len(reason) > 2000:
        raise HTTPException(422, "A review reason and provenance are required")
    source = next((row for row in review["sources"] if row["file_id"] == selected_file_id), None)
    if selected_file_id and (not source or not source["eligible"]):
        raise HTTPException(422, "Choose eligible business evidence from this file")
    if mode == "source" and not source:
        raise HTTPException(422, "Choose a business tax return or P&L source")
    if mode == "automatic":
        if any(value is not None for value in (selected_file_id, annual_value, year, currency, period_start, period_end)):
            raise HTTPException(422, "Automatic NOI restores source calculations without an override")
    else:
        value = number(annual_value)
        if value is None or abs(value) > 1e15:
            raise HTTPException(422, "A finite supported annual NOI amount is required")
        if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
            raise HTTPException(422, "An annual reporting year from 1900 to 2100 is required")
        if not currency or not re.fullmatch(r"[A-Z]{3}", currency) or currency == "XXX":
            raise HTTPException(422, "A known three-letter currency is required")
        if source and source.get("year") is not None and year != source["year"]:
            raise HTTPException(422, "The reviewed year must match the selected source")
        if source and source.get("currency") and currency != source["currency"]:
            raise HTTPException(422, "The reviewed currency must match the selected source; currency conversion is not automatic")
        if bool(period_start) != bool(period_end):
            raise HTTPException(422, "Provide both reporting dates or neither")
        if period_start and period_end:
            try:
                first, last = date.fromisoformat(str(period_start)), date.fromisoformat(str(period_end))
            except ValueError as exc:
                raise HTTPException(422, "Use valid reporting dates") from exc
            if first > last or last.year != year or (last - first).days > 366:
                raise HTTPException(422, "Reporting dates must be within one year and end in the reviewed year")
        request["annual_value"] = _money(value)
    revision = expected_revision + 1
    event = {**request, "id": str(uuid.uuid4()), "revision": revision, "source_metric": "noi",
        "source_refs": deepcopy(source["source_refs"]) if source else [],
        "actor_id": str(actor.id), "actor_name": getattr(actor, "full_name", None) or getattr(actor, "name", None),
        "at": (now or datetime.now(UTC)).isoformat(), "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {**state, "revision": revision, "events": [*state.get("events", []), event]}, True
