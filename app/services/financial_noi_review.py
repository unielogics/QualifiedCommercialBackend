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


def _identity_dependency(association):
    return {key: (association or {}).get(key) for key in (
        "status", "allows_financial_use", "expected_entity", "observed_entity", "observed_entities")}


def _earnings_operand(review, file_id, field, original):
    """The value earnings already use for this return, corrections included.

    Operating income then starts from the same bottom line as EBITDA, whatever
    basis the return prints (ordinary business income first).
    """
    target = next((item for item in (review or {}).get("inputs", [])
        if item.get("key", "").startswith(f"tax_return:{file_id}:") and item["key"].endswith(f".{field}")), None)
    return number(target.get("value")) if target else original


def _reviewed_tax_input(review, file_id, field, original):
    target = next((item for item in (review or {}).get("inputs", [])
        if item.get("key", "").startswith(f"tax_return:{file_id}:") and item["key"].endswith(f".{field}")), None)
    if not target or not target.get("correction"):
        return original
    if field == "income" and not any(ref.get("fact_key") == "ordinary_business_income" for ref in target.get("source_refs", [])):
        return original
    return number(target.get("value"))


def period_key(source):
    """Ordering only; a printed year never invents actual reporting dates."""
    return source.get("period_end") or (f"{source['year']:04d}-12-31" if source.get("year") else "")


def _as_of(now=None):
    return now.date() if isinstance(now, datetime) else now if isinstance(now, date) else datetime.now(UTC).date()


def _automatic_exclusion(facts, first, last, year, today):
    forecast = any(facts.get(key) is True for key in ("is_forecast", "is_projection", "forecast", "projected"))
    forecast = forecast or any(re.search(r"\b(?:forecast|projection|projected|budget|budgeted|pro forma)\b",
        str(facts.get(key) or "").strip().casefold().replace("_", " "))
        for key in ("statement_type", "report_type", "period_type", "reporting_type", "statement_basis"))
    if forecast:
        return "forecast_source"
    for value in (first, last):
        try:
            if date.fromisoformat(str(value)) > today:
                return "future_reporting_period"
        except ValueError:
            pass
    return "future_reporting_period" if year and year > today.year else None


def catalog(analyses, candidates, *, entity=None, metric_review=None, now=None):
    from app.services.document_associations import identity_matches, source_excluded
    from app.services.financial_fact_citations import (
        financial_analysis_citation_status,
        validated_fact_citations_for_analysis,
    )
    from app.services.financial_noi import PL_CLASSES, TAX_CLASSES, _entity, _period, _source_refs
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
        first = tax.get("period_start") if is_tax else (candidate or {}).get("period_start") or facts.get("period_start")
        last = tax.get("period_end") if is_tax else (candidate or {}).get("period_end") or facts.get("period_end")
        year = _year(last) or _year(facts.get("tax_year") or facts.get("fiscal_year") or facts.get("return_year"))
        notes = []
        if is_tax:
            income, income_paths = _first(facts, ("ordinary_business_income", "ordinary_business_income_loss", "ordinary_business_income_loss_current"))
            interest = number(tax.get("interest"))
            proposed = _money(income + interest) if income is not None and interest is not None else None
            notes.append("From the filed return: its bottom line, less other income, plus deducted interest. Depreciation and amortization remain expenses.")
            if not first and not last:
                notes.append("The return prints its tax year without period dates; it is read as that full year.")
            elif not first or not last:
                notes.append("The return's exact reporting dates are not established. Confirm that the reviewed amount represents the stated annual period; no automatic annualization is applied.")
            operands = {"income": income, "interest": interest, "income_fact_paths": income_paths,
                        "interest_fact_paths": tax.get("component_fact_paths", {}).get("interest", [])}
        else:
            proposed = number((candidate or {}).get("annualized_value"))
            operands = {"candidate": candidate}
            notes.extend((candidate or {}).get("notes", []))
        # Preserve the old catalog stamp byte-for-byte for legacy receipts.
        # New reviews bind only their relevant operands/identity/period below.
        legacy_stamp = stamp({"file_id": row.get("file_id"), "analysis": row.get("analysis_id"),
            "hash": row.get("content_hash"), "facts": facts, "accepted": row.get("accepted"),
            "association": row.get("identity_association"), "entity": entity, "operands": operands,
            "citation_manifest": row.get("financial_fact_citations")})
        other = None
        if is_tax:
            from app.services.financial_baseline import return_ladder

            income = _earnings_operand(metric_review, row.get("file_id"), "income",
                                       _reviewed_tax_input(metric_review, row.get("file_id"), "income", income))
            interest = _earnings_operand(metric_review, row.get("file_id"), "interest",
                                         _reviewed_tax_input(metric_review, row.get("file_id"), "interest", interest))
            # An interest deduction the reader did not find is not added back:
            # operating income is then understated, never missing.
            ladder = return_ladder(facts, income=income, interest=interest, depreciation=None) if business_tax else {}
            other = ladder.get("other_income")
            proposed = _money(ladder["noi"]) if ladder.get("noi") is not None else None
            if income is None:
                notes.append("The return's bottom line was not read. Enter it from the document; operating income is not estimated without it.")
            elif interest is None:
                notes.append("Interest was not read from the return, so none is added back.")
            relevant_values = {"income": income, "interest": interest,
                "non_operating": {key: facts.get(key) for key in (
                    "other_income", "non_operating_income", "rental_income", "capital_gains", "tax_form", "form_type")}}
        else:
            relevant_values = {key: (candidate or {}).get(key) for key in (
                "value", "annualized_value", "reported_value", "computed_value", "gross_revenue",
                "cost_of_goods_sold", "total_operating_expenses", "reconciliation_required", "months_covered")}
            relevant_values["fundamentals"] = [{"key": item.get("key"), "value": item.get("value")}
                for item in (candidate or {}).get("fundamentals", [])]
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
        months = _period({"period_start": first, "period_end": last})[2]
        period_assumed = bool(is_tax and business_tax and not first and not last and year)
        if period_assumed:
            months = 12
        automatic_reason = _automatic_exclusion(facts, first, last, year, _as_of(now))
        if automatic_reason:
            notes.append("Forecast or future-period evidence is excluded from automatic current NOI. An explicit review remains a disclosed annual assumption, not historical actual income.")
        belongs = bool((not is_tax or business_tax) and (reason is None or not observed or not entity or identity_matches(entity, observed)))
        financial_stamp = stamp({"file_id": str(row.get("file_id")), "analysis": row.get("analysis_id"),
            "hash": row.get("content_hash"), "entity": entity, "identity": _identity_dependency(row.get("identity_association")),
            "values": relevant_values, "currency": currency, "basis": basis, "year": year,
            "period_start": first, "period_end": last,
            "forecast_markers": {key: facts.get(key) for key in ("is_forecast", "is_projection", "forecast", "projected",
                "statement_type", "report_type", "period_type", "reporting_type", "statement_basis")}})
        sources.append({"file_id": str(row.get("file_id")), "file_name": row.get("file_name"),
            "kind": "business_tax_return" if is_tax else "profit_and_loss", "year": year,
            "currency": currency, "basis": basis, "period_start": first, "period_end": last,
            "proposed_annual_value": proposed, "eligible": reason is None, "reason": reason,
            "automatic_eligible": reason is None and automatic_reason is None, "automatic_reason": automatic_reason,
            "belongs_to_business": belongs, "months_covered": months, "financial_stamp": financial_stamp,
            **({"tax_operands": {"income": income, "interest": interest, "other_income": other},
                "period_assumed": period_assumed} if is_tax else {}),
            "accepted": bool(row.get("accepted")), "notes": notes, "citation_status": citation_status,
            "source_refs": [{**ref, "citations": citations,
                "pages": sorted({citation["page"] for citation in citations if isinstance(citation.get("page"), int)})}
                for ref in _source_refs(row, facts)],
            "source_stamp": legacy_stamp})
    return sorted(sources, key=lambda row: (row.get("year") or 0, row["file_id"]), reverse=True)


def prepare(analyses, candidates, *, state=None, entity=None, metric_review=None, now=None):
    state = state or {}
    sources = catalog(analyses, candidates, entity=entity, metric_review=metric_review, now=now)
    source_stamp = stamp([{key: row[key] for key in ("file_id", "source_stamp", "eligible")} for row in sorted(sources, key=lambda row: row["file_id"])])
    history = [event for event in state.get("events", []) if event.get("source_metric") == "noi"]
    current = history[-1] if history else None
    latest_period = max((period_key(row) for row in sources if row["belongs_to_business"] and not row["automatic_reason"]), default="")
    entity_stamp = stamp({"entity": entity})
    stale, stale_reason = False, None
    if current and current.get("mode") != "automatic":
        dependency = current.get("source_dependency")
        if isinstance(dependency, dict) and dependency.get("version") == 2:
            source = next((row for row in sources if row["file_id"] == current.get("selected_file_id")), None)
            stale = (dependency.get("entity_stamp") != entity_stamp
                     or bool(current.get("selected_file_id") and (not source or not source["eligible"]
                         or source["financial_stamp"] != dependency.get("financial_stamp"))))
            if stale:
                stale_reason = "The reviewed NOI source, business identity or financial operands changed. Review and save its annual basis again."
            elif latest_period > (dependency.get("latest_period") or ""):
                stale, stale_reason = True, "A newer business reporting period is available. Review the pinned annual NOI basis against that period."
        else:
            # Old receipts lack scoped operand/identity snapshots. Retain their
            # original guard until an explicit review upgrades the receipt.
            stale = current.get("source_stamp") != source_stamp
            selected = current.get("selected_file_id")
            if selected and any(event.get("revision", 0) > current.get("revision", 0)
                and event.get("input_key", "").startswith(f"tax_return:{selected}:")
                and event["input_key"].endswith((".income", ".interest")) for event in state.get("events", [])):
                stale = True
            if stale:
                stale_reason = "Earlier NOI review uses the legacy evidence catalog; re-review to bind current NOI dependencies."
    return {"revision": state.get("revision", 0), "source_stamp": source_stamp,
            "mode": current.get("mode", "automatic") if current else "automatic",
            "stale": stale, "stale_reason": stale_reason, "entity_stamp": entity_stamp,
            "latest_period": latest_period, "as_of_date": _as_of(now).isoformat(),
            "sources": sources, "current": current, "history": history}


def effective(review):
    """Project only a current explicit review; callers retain source history."""
    current = review.get("current")
    if not current or current["mode"] == "automatic" or review["stale"]:
        return None
    source = next((row for row in review["sources"] if row["file_id"] == current.get("selected_file_id")), None)
    tax = bool(source and source["kind"] == "business_tax_return")
    assumption = bool((source or {}).get("automatic_reason") or (current.get("source_dependency") or {}).get("assumption"))
    kind = "tax_derived_noi" if tax else "staff_reviewed_noi"
    label = "Tax-derived NOI estimate (staff reviewed)" if tax else "Staff-reviewed business NOI"
    if assumption:
        label = "Staff-reviewed NOI assumption" if not tax else "Tax-derived NOI assumption (staff reviewed)"
    first, last = current.get("period_start"), current.get("period_end")
    period = f"{first} – {last} (annual staff review)" if first and last else f"Year {current['year']} (annual staff review)"
    notes = ["Explicit annual staff NOI review. Source documents, P&L history, EBITDA and program qualification remain independent."]
    if assumption:
        notes.append("This explicit review uses forecast or future-period information. It is an annual assumption, not historical actual operating income.")
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
        "forecast_assumption": assumption,
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
            if event.get("request_stamp") != request_stamp or event.get("actor_id") != str(actor.id):
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
        "source_dependency": {"version": 2, "entity_stamp": review["entity_stamp"],
            "latest_period": max(review["latest_period"], request.get("period_end") or (f"{year:04d}-12-31" if year else "")),
            "assumption": bool((source or {}).get("automatic_reason") or
                _automatic_exclusion({}, request.get("period_start"), request.get("period_end"), year,
                    date.fromisoformat(review["as_of_date"]))),
            "financial_stamp": source.get("financial_stamp") if source else None},
        "source_refs": deepcopy(source["source_refs"]) if source else [],
        "actor_id": str(actor.id), "actor_name": getattr(actor, "full_name", None) or getattr(actor, "name", None),
        "at": (now or datetime.now(UTC)).isoformat(), "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {**state, "revision": revision, "events": [*state.get("events", []), event]}, True
