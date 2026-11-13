"""Business operating income with an explicit source and reporting period.

NOI here means revenue less COGS and operating expenses, including D&A where
the business expensed it. It is not property cash NOI, EBITDA, tax income, or
bank cash retained. Debt principal and interest do not change operating NOI.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from types import SimpleNamespace

from app.services.financial_metric_review import number
from app.services.public_underwriting_packet_pdf import _num, _tax_year

DEFINITION = {
    "key": "business_operating_income",
    "label": "Business operating income (NOI)",
    "formula": "revenue - cost of goods sold - operating expenses",
    "includes_depreciation_and_amortization": True,
    "note": "Operating expenses include depreciation and amortization where expensed. Interest, income taxes, non-operating items and loan principal are separate from operating income.",
}
PL_CLASSES = {"current_p_and_l", "profit_and_loss", "income_statement"}
TAX_CLASSES = {"tax_return", "business_tax_return"}
NOI_POLICY_VERSION = "business-noi-v2"
ALIASES = {
    "gross_revenue": ("gross_revenue", "revenue", "total_revenue", "gross_receipts", "sales", "gross_sales", "total_sales"),
    "cost_of_goods_sold": ("cost_of_goods_sold", "cogs"),
    "total_operating_expenses": ("total_operating_expenses", "operating_expenses"),
    "reported_operating_income": ("operating_income", "operating_profit", "net_operating_income"),
}
LABELS = {
    "gross_revenue": "Revenue",
    "cost_of_goods_sold": "Cost of goods sold",
    "total_operating_expenses": "Operating expenses, including expensed D&A",
    "reported_operating_income": "Source-reported operating income",
}
CITATIONS = {"gross_revenue": "revenue", "cost_of_goods_sold": "cogs",
             "total_operating_expenses": "operating_expenses", "reported_operating_income": "operating_income"}


def _first(facts, keys):
    return next((value for key in keys if (value := _num(facts.get(key))) is not None), None)


def _period(facts):
    first, last = facts.get("period_start"), facts.get("period_end")
    try:
        start, end = date.fromisoformat(str(first)), date.fromisoformat(str(last))
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        complete_months = start <= end and start.day == 1 and end.day == monthrange(end.year, end.month)[1] and 1 <= months <= 12
        declared = number(facts.get("months_covered"))
        consistent = declared is None or declared == months
        return start, end, months if complete_months and consistent else None
    except (TypeError, ValueError):
        return None, None, None


def _end(facts):
    try:
        return date.fromisoformat(str(facts.get("period_end")))
    except (TypeError, ValueError):
        # Normalize printed labels such as "Calendar year 2025" using the
        # same year extraction as the tax history. This placeholder orders
        # business periods only; _period still requires actual P&L dates
        # before producing annual operating income.
        year = _tax_year(facts) or ""
        return date(int(year), 12, 31) if len(year) == 4 and year.isdigit() and 1 <= int(year) <= 9999 else None


def _entity(facts):
    return next((facts.get(key) for key in ("entity_name", "business_name", "entity", "taxpayer") if facts.get(key)), None)


def _source_refs(row, facts):
    from app.services.document_associations import summary

    return [{"file_id": str(row.get("file_id")), "file_name": row.get("file_name"),
             "analysis_id": row.get("analysis_id"), "content_hash": row.get("content_hash"),
             "accepted": bool(row.get("accepted")), "entity": _entity(facts),
             "period_start": facts.get("period_start"), "period_end": facts.get("period_end"),
             "currency": facts.get("currency"), "basis": facts.get("basis") or facts.get("accounting_basis"),
             "identity_association": summary(row.get("identity_association"))}]


def _review_input(review, file_id, key):
    expected = f"profit_and_loss.{key}"
    return next((item for item in (review or {}).get("inputs", []) if item.get("key") == expected
                 and any(str(ref.get("file_id")) == str(file_id) for ref in item.get("source_refs", []))), None)


def _candidate(row, *, entity=None, metric_review=None):
    from app.services.document_associations import identity_matches
    from app.services.financial_fact_citations import validated_fact_citations_for_analysis

    facts = row.get("key_facts") or {}
    source_refs = _source_refs(row, facts)
    start, end, months = _period(facts)
    citations = validated_fact_citations_for_analysis(SimpleNamespace(
        id=row.get("analysis_id"), bucket_file_id=row.get("file_id"), content_hash=row.get("content_hash"),
        status="completed", analysis={"key_facts": facts, "financial_fact_citations": row.get("financial_fact_citations")}))
    fundamentals = []
    values = {}
    for key, aliases in ALIASES.items():
        reviewed = _review_input(metric_review, row.get("file_id"), key)
        # Normalized P&L records can algebraically recover operating expenses
        # from reported operating income. That is useful for display but is
        # not independent source evidence that reconciles the same NOI line.
        # Only a current explicit correction may fill a missing raw operand.
        value = number(reviewed.get("value")) if reviewed and reviewed.get("correction") else _first(facts, aliases)
        refs = reviewed.get("source_refs", []) if reviewed is not None else [
            {**ref, "fact_key": next((alias for alias in aliases if _num(facts.get(alias)) is not None), aliases[0]),
             "fact_present": any(_num(facts.get(alias)) is not None for alias in aliases),
             "citations": citations.get(CITATIONS[key], []),
             "pages": sorted({c["page"] for c in citations.get(CITATIONS[key], []) if isinstance(c.get("page"), int)})}
            for ref in source_refs]
        values[key] = value
        fundamentals.append({"key": key, "label": LABELS[key], "value": value,
                             "signed_value": -value if value is not None and key in {"cost_of_goods_sold", "total_operating_expenses"} else value,
                             "input_key": reviewed["key"] if reviewed is not None else None,
                             "source_refs": refs, "corrected": bool(reviewed and reviewed.get("correction"))})
    revenue, cogs, opex, reported = (values[key] for key in ALIASES)
    computed = round(revenue - cogs - opex, 2) if all(value is not None for value in (revenue, cogs, opex)) else None
    raw_reported = _first(facts, ALIASES["reported_operating_income"])
    components_corrected = any(line["corrected"] for line in fundamentals[:3])
    reported_corrected = fundamentals[3]["corrected"]
    use_reviewed_components = bool(components_corrected and computed is not None and not reported_corrected)
    value = reported if reported_corrected else computed if use_reviewed_components else reported if reported is not None else computed
    difference = round(raw_reported - computed, 2) if raw_reported is not None and computed is not None else None
    staff_adjusted = bool(reported_corrected or use_reviewed_components)
    original_components = [_first(facts, ALIASES[key]) for key in tuple(ALIASES)[:3]]
    source_value = raw_reported if raw_reported is not None else round(original_components[0] - original_components[1] - original_components[2], 2) if all(value is not None for value in original_components) else None
    currency = str(facts.get("currency") or "").strip().upper()
    currency = currency if len(currency) == 3 and currency.isascii() and currency.isalpha() and currency != "XXX" else None
    basis = str(facts.get("basis") or facts.get("accounting_basis") or "").strip().casefold() or None
    identity = _entity(facts)
    comparable = bool(entity and identity_matches(entity, identity, row.get("identity_association")))
    factor = 1 if months == 12 else 12 / months if months is not None and 6 <= months < 12 else None
    period = f"{start.isoformat()} – {end.isoformat()}" if start and end else "Unknown reporting period"
    notes = [DEFINITION["note"], "This is business operating income; it is not an EBITDA addback calculation or bank cash-flow proxy."]
    if reported is not None and computed is None:
        notes.append("Operating income is reported by the source, but revenue, COGS or operating expenses are missing for a complete fundamental reconciliation.")
    if difference is not None and abs(difference) > 0.01:
        notes.append("Reported operating income differs from revenue less COGS and operating expenses. The working staff review is shown beside the retained source amount; reconcile the difference before relying on it." if staff_adjusted else
                     "Reported operating income differs from revenue less COGS and operating expenses. The source-reported amount is shown; reconcile the difference before relying on it.")
    if use_reviewed_components:
        notes.append("Staff corrected the fundamental inputs. Reviewed NOI uses those revenue, COGS and operating-expense inputs; the source-reported operating income remains visible and unchanged.")
    financing_expense = _num((facts.get("notable_expense_lines") or {}).get("bank_loan_payments_ytd"))
    reviewed_opex = _review_input(metric_review, row.get("file_id"), "total_operating_expenses")
    reconciliation_required = bool(financing_expense is not None and financing_expense > 0
                                   and not (reviewed_opex and reviewed_opex.get("correction")))
    if reconciliation_required:
        notes.append("Loan payments appear within source expenses. Reconcile operating expenses versus principal and interest before comparing NOI less debt service; no automatic loan-payment addback is applied.")
    if factor is not None and factor != 1:
        notes.append("Annualized run-rate illustration from the dated P&L period, without a seasonal adjustment; not a completed annual result.")
    elif factor is None:
        notes.append("Annual comparison and debt-service residual require a dated six-to-twelve-month P&L; this source is not annualized.")
    if not currency:
        notes.append("Source currency is not established; no conversion or USD debt-service comparison is assumed.")
    missing = [key for key in ("gross_revenue", "cost_of_goods_sold", "total_operating_expenses") if values[key] is None]
    complete = computed is not None and (difference is None or abs(difference) <= 0.01)
    active = [fundamentals[3]] if reported_corrected or reported is not None and not use_reviewed_components else fundamentals[:3]
    return {"key": f"noi:{row.get('file_id')}", "label": DEFINITION["label"], "kind": DEFINITION["key"],
            "value": value, "annualized_value": round(value * factor, 2) if value is not None and factor is not None else None,
            "reported_value": raw_reported, "effective_reported_value": reported, "computed_value": computed, "reconciliation_difference": difference,
            "source_value": source_value, "reviewed_value": value if staff_adjusted else None, "staff_adjusted": staff_adjusted,
            "gross_revenue": revenue, "cost_of_goods_sold": cogs, "total_operating_expenses": opex,
            "margin_pct": round(value / revenue * 100, 4) if value is not None and revenue is not None and revenue > 0 else None,
            "year": str(end.year) if end else None, "period": period,
            "period_start": start.isoformat() if start else facts.get("period_start"),
            "period_end": end.isoformat() if end else facts.get("period_end"),
            "months_covered": months, "annualization_factor": factor,
            "annualized_run_rate": bool(factor is not None and factor != 1),
            "currency": currency, "basis": basis, "complete": complete,
            "provisional": not bool(row.get("accepted")) or not comparable or not complete or currency is None or basis not in {"cash", "accrual"} or reconciliation_required,
            "reconciliation_required": reconciliation_required,
            "entity_comparable": comparable, "entity_mismatch": bool(entity and not comparable),
            "formula": "staff-reviewed operating income" if reported_corrected else DEFINITION["formula"] if use_reviewed_components else "source-reported operating income" if reported is not None else DEFINITION["formula"],
            # The review workspace needs every source-bound fundamental to
            # inspect and correct it, even when reported NOI supplies the
            # current figure. Keep the arithmetic basis separate: exposing
            # four inputs never means adding reported NOI to its components.
            "input_keys": [line["input_key"] for line in fundamentals if line["input_key"]],
            "active_input_keys": [line["input_key"] for line in active if line["input_key"]],
            "missing_inputs": missing, "fundamentals": fundamentals, "notes": notes, "source_refs": source_refs}


def _direction(delta):
    return None if delta is None else "neutral" if abs(delta) < 0.005 else "up" if delta > 0 else "down"


def _trend(years):
    empty = {"comparable": False, "direction": None, "prior_year": None, "current_year": None,
             "prior_noi": None, "current_noi": None, "change_amount": None, "change_pct": None,
             "revenue_direction": None, "prior_revenue": None, "current_revenue": None,
             "revenue_change_pct": None, "reason": "Two comparable consecutive full-year business P&Ls are needed for the operating-income trend."}
    if len(years) < 2:
        return empty
    prior, current = years[-2:]
    periods_comparable = bool(prior["currency"] and prior["currency"] == current["currency"] and prior["basis"] in {"cash", "accrual"}
                      and prior["basis"] == current["basis"] and int(current["year"]) - int(prior["year"]) == 1
                      and prior["period_start"][5:] == current["period_start"][5:] and prior["period_end"][5:] == current["period_end"][5:])
    comparable = periods_comparable and prior["value"] is not None and current["value"] is not None
    delta = round(current["value"] - prior["value"], 2) if comparable and current["value"] is not None and prior["value"] is not None else None
    revenue_delta = round(current["gross_revenue"] - prior["gross_revenue"], 2) if periods_comparable and current["gross_revenue"] is not None and prior["gross_revenue"] is not None else None
    return {**empty, "comparable": comparable, "direction": _direction(delta),
            "prior_year": prior["year"], "current_year": current["year"], "prior_noi": prior["value"], "current_noi": current["value"],
            "change_amount": delta, "change_pct": round(delta / abs(prior["value"]) * 100, 2) if delta is not None and prior["value"] else None,
            "revenue_direction": _direction(revenue_delta), "prior_revenue": prior["gross_revenue"], "current_revenue": current["gross_revenue"],
            "revenue_change_pct": round(revenue_delta / prior["gross_revenue"] * 100, 2) if revenue_delta is not None and prior["gross_revenue"] > 0 else None,
            "change_pct_basis": "Change divided by the absolute prior-year NOI; a positive change means operating earnings improved, including when both years are losses.",
            "reason": None if comparable else empty["reason"]}


def _automatic_tax(source, metric_review):
    values = source["tax_operands"]
    value = source["proposed_annual_value"]
    annual = value if source.get("months_covered") == 12 else None
    inputs = [item["key"] for item in (metric_review or {}).get("inputs", [])
              if item.get("key", "").startswith(f"tax_return:{source['file_id']}:")
              and item["key"].endswith((".income", ".interest"))]
    return {"key": f"noi-tax:{source['file_id']}", "kind": "tax_derived_noi", "label": "Tax-derived NOI estimate",
        "value": value, "annualized_value": annual, "year": str(source["year"]) if source.get("year") else None,
        "period": f"{source['period_start']} – {source['period_end']}" if source.get("period_start") and source.get("period_end") else f"Tax year {source['year']} (reporting duration unconfirmed)" if source.get("year") else "Unknown tax reporting period",
        "period_start": source.get("period_start"), "period_end": source.get("period_end"),
        "months_covered": source.get("months_covered"), "annualization_factor": 1 if annual is not None else None,
        "annualized_run_rate": False, "currency": source.get("currency"), "basis": source.get("basis"),
        "complete": True, "provisional": True, "tax_derived_estimate": True, "staff_reviewed": False,
        "staff_adjusted": False, "source_accepted": source["accepted"], "entity_comparable": True,
        "entity_mismatch": False, "reconciliation_required": False,
        "formula": "ordinary business tax income + known deducted interest (D&A remains expensed)",
        "source_value": value, "reviewed_value": None, "input_keys": inputs, "active_input_keys": inputs,
        "missing_inputs": [], "fundamentals": [
            {"key": "ordinary_business_income", "label": "Ordinary business tax income", "value": values["income"], "signed_value": values["income"], "source_refs": source["source_refs"]},
            {"key": "interest", "label": "Known deducted interest", "value": values["interest"], "signed_value": values["interest"], "source_refs": source["source_refs"]}],
        "notes": [*source["notes"], "Automatic provisional estimate only. Review its business, annual reporting period, tax/book differences and non-operating items before relying on it; no program condition is approved."],
        "source_refs": source["source_refs"]}


def _automatic_basis(review, candidates, metric_review):
    from app.services.financial_noi_review import period_key

    sources = review["sources"]
    latest = review["latest_period"]
    current = [row for row in sources if row["belongs_to_business"] and not row["automatic_reason"] and period_key(row) == latest]
    eligible = [row for row in current if row["automatic_eligible"]]
    by_file = {row["source_refs"][0]["file_id"]: row for row in candidates}
    pl = [by_file[row["file_id"]] for row in eligible if row["kind"] == "profit_and_loss"
          and row["file_id"] in by_file and by_file[row["file_id"]]["value"] is not None]
    if pl:
        return max(pl, key=lambda row: (bool(row["complete"]), not row["provisional"], row["key"])), []
    tax = [row for row in eligible if row["kind"] == "business_tax_return" and row["proposed_annual_value"] is not None]
    if tax:
        return _automatic_tax(tax[0], metric_review), ["tax_estimate"]
    if current and not eligible:
        return None, ["business_identity_review"]
    older_tax = [row for row in sources if row["automatic_eligible"] and row["kind"] == "business_tax_return"
                 and row["proposed_annual_value"] is not None and period_key(row) < latest]
    if older_tax:
        return _automatic_tax(max(older_tax, key=period_key), metric_review), ["tax_estimate", "older_tax_fallback"]
    if any(row["belongs_to_business"] and row["automatic_reason"] for row in sources) and not current:
        return None, ["forecast_or_future_source"]
    return None, ["missing_noi_operands"]


def _governance(headline, review, automatic_codes):
    from app.services.financial_metric_review import stamp

    source = next((row for row in review["sources"] if headline and any(
        ref.get("file_id") == row["file_id"] for ref in headline.get("source_refs", []))), None)
    limitations = []
    if source and not source["accepted"]:
        limitations.append("source_not_accepted")
    if source and not any(row.get("status") == "verified" for row in source.get("citation_status", {}).values()):
        limitations.append("source_quotations_unverified")
    if headline and headline.get("tax_derived_estimate"):
        limitations.append("tax_book_and_non_operating_review")
    if headline and headline.get("staff_reviewed") and not headline.get("source_refs"):
        limitations.append("manual_without_linked_source")
    if headline and headline.get("forecast_assumption"):
        limitations.append("forecast_or_future_reporting_assumption")
    if review["stale"]:
        status, reason, codes = "stale", review["stale_reason"], ["review_source_changed"]
    elif headline and headline.get("staff_reviewed"):
        status, reason, codes = "reviewed", "Staff explicitly reviewed this annual NOI basis. Source limitations remain disclosed; document acceptance and program qualification are separate.", ["explicit_staff_review"]
        if headline.get("forecast_assumption"):
            reason = "Staff explicitly reviewed this annual NOI assumption from forecast or future-period information. It is not historical actual income, document acceptance or program qualification."
            codes.append("forecast_or_future_reporting_assumption")
    elif headline:
        codes = list(automatic_codes)
        if headline.get("annualized_value") is None:
            codes.append("annual_period_unconfirmed")
        if not headline.get("currency"):
            codes.append("currency_unconfirmed")
        if headline.get("reconciliation_required"):
            codes.append("financing_expenses_need_reconciliation")
        if headline.get("annualized_run_rate"):
            codes.append("annualized_run_rate")
        if headline.get("provisional") and not codes:
            codes.append("source_reconciliation_needed")
        if source and not source["accepted"]:
            codes.append("source_not_accepted")
        status = "provisional" if codes else "source_supported"
        reason = ("A newer business reporting period is incomplete. This explicitly dated older tax estimate is shown for review, rather than substituted as current-year P&L income."
                  if "older_tax_fallback" in codes else
                  "Tax-derived operating-income estimate: ordinary business tax income plus known deducted interest, with D&A retained as an expense. Staff review is required; it is not verified P&L income or program eligibility."
                  if "tax_estimate" in codes else
                  "The source amount is computable, but its reporting period, currency or operating-expense reconciliation needs staff review."
                  if codes else "The latest supported business P&L supplies operating income. Refinancing and EBITDA remain separate.")
    else:
        status, codes = "unavailable", list(automatic_codes)
        reason = ("The latest business source's identity or association needs review before NOI can be used."
                  if "business_identity_review" in codes else
                  "Only forecast or future-period business sources are available. Add a completed historical P&L or business tax return, or explicitly review an annual NOI assumption."
                  if "forecast_or_future_source" in codes else
                  "The latest business P&L has missing operating-income inputs for the latest business reporting period, and no computable business tax estimate is available. Tax net income alone is not NOI: tax income and deducted interest must both be known; missing operands are never assumed to be zero.")
    result = {"policy_version": NOI_POLICY_VERSION, "status": status,
        "review_needed": status not in {"source_supported", "reviewed"}, "status_reason": reason,
        "reason_codes": list(dict.fromkeys(codes)), "source_limitations": limitations}
    result["authority_stamp"] = stamp({**result, "headline": {key: (headline or {}).get(key) for key in (
        "key", "kind", "value", "annualized_value", "year", "period_start", "period_end", "currency", "basis")},
        "financial_source": source.get("financial_stamp") if source else None,
        "identity": review["entity_stamp"], "latest_period": review["latest_period"],
        "review_id": (review.get("current") or {}).get("id")})
    return result


def _residual_comparable(headline):
    return bool(headline and number(headline.get("annualized_value")) is not None
                and headline.get("currency") == "USD" and not headline.get("reconciliation_required"))


def _operating_adjustments(headline, items):
    if not (_residual_comparable(headline) and headline.get("complete") and not headline.get("provisional")):
        return []
    adjustments, used = [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        amount = number(item.get("amount_annual"))
        identity = str(item.get("id") or f"{item.get('kind')}:{item.get('label')}:{amount}:{item.get('source')}")
        if (amount is not None and amount > 0 and item.get("kind") in {"owner_salary", "one_time_expense", "rent_to_owner"}
                and item.get("status") == "hypothetical_unverified"
                and all(str(item.get(key) or "").strip() for key in ("label", "note", "source")) and identity not in used):
            used.add(identity)
            adjustments.append({key: item.get(key) for key in ("id", "kind", "label", "note", "source", "status")}
                               | {"amount_annual": amount})
    return adjustments


def preview_scenario(noi, evaluation):
    """Reuse the current source NOI while replacing only unsaved plan effects."""
    from copy import deepcopy

    scenario = deepcopy(noi.get("scenario") or {})
    scenario.pop("operating_plan", None)
    headline = noi.get("headline") or {}
    current, operating = number(scenario.get("current")), number(evaluation.get("annual_new_operating_expenses", 0))
    comparable = _residual_comparable(headline)
    adjustments = _operating_adjustments(headline,
        (evaluation.get("ebitda") or {}).get("adjusted_annual", {}).get("adjustments", []))
    total = sum(number(row.get("amount_annual")) or 0 for row in adjustments)
    planned = round(current + total - operating, 2) if current is not None and operating is not None and (headline.get("currency") == "USD" or total == operating == 0) else None
    operating_plan = evaluation.get("operating_plan") or {}
    if "noi_applied" in operating_plan:
        noi_impact = number(operating_plan.get("applied_noi_annual_impact"))
        if operating_plan["noi_applied"] and comparable and planned is not None and noi_impact is not None:
            planned = round(planned + noi_impact, 2)
    elif operating_plan.get("applied") and operating_plan.get("noi_eligible") and comparable and planned is not None:
        planned = round(planned + (number(operating_plan.get("applied_annual_impact")) or 0), 2)
    debt = number(evaluation.get("debt_service_after_annual")) if evaluation.get("active") and not evaluation.get("coverage_unavailable_reason") else None
    scenario.update(plan=planned, operating_adjustments=deepcopy(adjustments), annual_new_operating_expenses=operating,
        annual_debt_service_plan=debt, after_debt_service_plan=round(planned - debt, 2) if comparable and planned is not None and debt is not None else None)
    if operating_plan.get("applied") or operating_plan.get("noi_applied"):
        scenario["operating_plan"] = deepcopy(operating_plan)
    return scenario


def build_noi(analyses, *, statement=None, entity=None, metric_review=None,
              current_annual_debt_service=None, plan_annual_debt_service=None,
              operating_adjustments=None, source_selection=None, additional_operating_expense_annual=0,
              review_state=None, now=None):
    """Build source-bound operating income without changing evidence or reviews."""
    from app.services.document_associations import identity_matches, source_excluded
    from app.services.financial_tax_earnings import tax_source_is_business

    relevant, candidates = [], []
    for row in analyses:
        classes = {row.get("classification"), *(row.get("classifications") or [])}
        facts = row.get("key_facts") or {}
        is_pl = bool(classes.intersection(PL_CLASSES))
        is_tax = bool(classes.intersection(TAX_CLASSES)) and tax_source_is_business(facts)
        if classes.intersection({"personal_tax_return"}) or not is_pl and not is_tax:
            continue
        end = _end(facts)
        observed = _entity(facts)
        denied = source_excluded(row.get("identity_association"))
        unmatched = bool(entity and not identity_matches(entity, observed, row.get("identity_association")))
        if denied or unmatched:
            # An unresolved/denied latest source for this named business must
            # not silently revive an older year's operating income. A known
            # different business never establishes this business's period.
            if not observed or not entity or identity_matches(entity, observed):
                relevant.append((end, row, False))
            continue
        relevant.append((end, row, is_pl))
        if is_pl:
            candidates.append(_candidate(row, entity=entity, metric_review=metric_review))
    candidates.sort(key=lambda item: (item.get("period_end") or "", item.get("period_start") or "", item["key"]))
    # A newer tax period is not an operating-income statement, but it prevents
    # an older P&L from masquerading as the business's current annual baseline.
    latest = max((end for end, _row, _pl in relevant if end is not None), default=None)
    excluded = set((source_selection or {}).get("excluded_file_ids", []))
    current = [item for item in candidates if item.get("period_end") == (latest.isoformat() if latest else None)]
    selected_file = (source_selection or {}).get("selected_file_id")
    chosen = next((item for item in current if item["source_refs"][0]["file_id"] == str(selected_file)), None)
    chosen = chosen or (current[-1] if current else None)
    reason = None
    if (source_selection or {}).get("stale"):
        chosen = None
        reason = "Financial source evidence changed. Review the current business sources before relying on NOI."
    elif chosen and chosen["source_refs"][0]["file_id"] in excluded:
        chosen = None
        reason = "The latest business operating statement is excluded. Restore or replace it; a prior year is not substituted."
    elif not chosen:
        reason = "A P&L for the latest business reporting period with operating income or complete revenue, COGS and operating expenses is needed. Tax net income and bank cash retained cannot establish NOI."
    elif chosen["value"] is None:
        reason = "The latest business P&L has missing operating-income inputs. Review its revenue, COGS and operating expenses; a prior year is not substituted."
    for item in candidates:
        item["selected"] = item is chosen
        item["historical_comparison_only"] = item is not chosen
    headline = chosen if chosen and chosen["value"] is not None else None
    # One P&L per annual reporting period; alternative copies are not summed.
    by_period = {}
    for item in candidates:
        if item["months_covered"] == 12:
            by_period[(item["period_start"], item["period_end"])] = item
    years = list(by_period.values())
    from app.services import financial_noi_review

    # Retain the legacy candidate preparation for old review-stamp compatibility;
    # the independent NOI policy now selects its own operating-income basis.
    pnl_headline = headline
    review = financial_noi_review.prepare(analyses, candidates, state=review_state, entity=entity, metric_review=metric_review, now=now)
    historical_ids = {row["file_id"] for row in review["sources"] if not row["automatic_reason"]}
    years = [row for row in years if row["source_refs"][0]["file_id"] in historical_ids]
    headline, automatic_codes = _automatic_basis(review, candidates, metric_review)
    for item in candidates:
        item["selected"] = item is headline
        item["historical_comparison_only"] = item is not headline
        item["automatic_eligible"] = item["source_refs"][0]["file_id"] in historical_ids
    source_headline = headline
    source_reason = _governance(headline, {**review, "stale": False}, automatic_codes)["status_reason"] if not headline else None
    if review["mode"] != "automatic":
        headline = financial_noi_review.effective(review)
    governance = _governance(headline, review, automatic_codes)
    reason = governance["status_reason"] if headline is None else None
    annual = headline.get("annualized_value") if headline else None
    usd = bool(headline and headline.get("currency") == "USD")
    residual_comparable = _residual_comparable(headline)
    current_debt, plan_debt = number(current_annual_debt_service), number(plan_annual_debt_service)
    current_debt = current_debt if current_debt is not None and current_debt >= 0 else None
    plan_debt = plan_debt if plan_debt is not None and plan_debt >= 0 else None
    adjustments = _operating_adjustments(headline, operating_adjustments)
    new_operating = number(additional_operating_expense_annual)
    planned = round(annual + sum(item["amount_annual"] for item in adjustments) - new_operating, 2) if annual is not None and new_operating is not None and (usd or new_operating == 0) else None
    scenario_notes = ["Refinancing changes debt service, not business operating income. Loan proceeds, principal payments and interest savings are not operating-income addbacks.",
                      "After-debt-service amounts are an illustration of operating income less total debt payments, not a complete cash-flow statement or DSCR numerator reconciliation."]
    if not usd and (current_debt is not None or plan_debt is not None):
        scenario_notes.append("Debt service is in USD; confirm matching operating-income currency before comparing the two.")
    if headline and headline["annualized_run_rate"]:
        scenario_notes.append("The annual operating amount is an annualized P&L run-rate illustration, not a filed full-year result.")
    if headline and headline.get("reconciliation_required"):
        scenario_notes.append("After-debt-service results are unavailable until loan payments already included in expenses are reconciled, avoiding a second deduction of the same debt payments.")
    if adjustments:
        scenario_notes.append("The plan's operating adjustments are explicit staff hypotheses, not verified NOI or lender-accepted facts. The historical operating result stays unchanged.")
    if new_operating is None or new_operating:
        scenario_notes.append("New recurring budget rows classified as operating expenses reduce projected operating income once. Financing charges, capitalized fees and escrow do not alter historical NOI.")
    if governance["status"] == "provisional":
        scenario_notes.append("Provisional operating-income illustration only. The NOI review status and source limitations also apply to every after-debt-service amount; this does not establish program qualification.")
    return {**governance, "definition": dict(DEFINITION), "headline": headline, "candidates": candidates, "years": years,
            "trend": _trend(years), "reason": reason, "source_headline": source_headline,
            "source_reason": source_reason, "pnl_headline": pnl_headline, "review": review,
            "scenario": {"current": annual, "plan": planned, "operating_adjustments": adjustments,
                         **{key: governance[key] for key in ("policy_version", "status", "review_needed", "status_reason", "reason_codes", "authority_stamp")},
                         "annual_new_operating_expenses": new_operating,
                         "annual_debt_service_current": current_debt, "annual_debt_service_plan": plan_debt,
                         "after_debt_service_current": round(annual - current_debt, 2) if residual_comparable and annual is not None and current_debt is not None else None,
                         "after_debt_service_plan": round(planned - plan_debt, 2) if residual_comparable and planned is not None and plan_debt is not None else None,
                         "annualized_run_rate": bool(headline and headline["annualized_run_rate"]), "notes": scenario_notes}}
