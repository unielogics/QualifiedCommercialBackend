"""Evidence-derived drafts and industry questions, never invented business costs."""

from __future__ import annotations

import math
import re
from copy import deepcopy

POLICY_VERSION = "operating-guidance-v1"
# Match named facts, not fixed IRS line numbers: the same expense appears on
# different lines on Schedule C, Forms 1120/1120-S and supporting schedules.
LINES = (
    ("rent", "rent", "Rent and premises", ("rent", "rents", "rent_expense", "rent_and_lease", "rent_buildings")),
    ("salaries_and_wages", "salaries", "Employee salaries and wages", ("salaries_and_wages", "wages", "salaries", "payroll_expense")),
    ("officer_compensation", "salaries", "Officer compensation", ("officer_compensation", "compensation_of_officers", "officers_compensation")),
    ("contract_labor", "professional_fees", "Contract labor", ("contract_labor", "contractor_expense", "subcontractors")),
    ("payroll_taxes", "payroll_taxes", "Employer payroll taxes", ("payroll_taxes", "employer_payroll_taxes")),
    ("insurance", "insurance", "Business insurance", ("insurance", "insurance_expense", "business_insurance")),
    ("utilities", "utilities", "Utilities", ("utilities", "utility_expense")),
    ("advertising", "marketing", "Marketing and advertising", ("advertising", "marketing", "marketing_expense", "advertising_expense")),
    ("software", "software", "Software and subscriptions", ("software", "software_subscriptions", "subscriptions")),
    ("professional_fees", "professional_fees", "Legal and professional fees", ("professional_fees", "legal_and_professional", "legal_and_professional_fees", "legal_fees")),
    ("supplies", "supplies", "Supplies", ("supplies", "office_supplies", "supplies_expense")),
    ("repairs", "repairs", "Repairs and maintenance", ("repairs", "repairs_and_maintenance", "repair_expense")),
    ("transportation", "transportation", "Travel and transportation", ("transportation", "travel", "car_and_truck_expenses")),
    ("cogs", "cogs", "Cost of goods sold", ("cost_of_goods_sold", "cogs")),
    ("taxes_and_licenses", "other", "Taxes and licenses (classify before use)", ("taxes_and_licenses",)),
    ("depreciation_amortization", "depreciation_amortization", "Depreciation and amortization", ("depreciation_and_amortization", "depreciation", "depreciation_expense")),
)
ESSENTIALS = (
    ("rent", "Rent and premises", "Confirm rent, related-party rent, shared premises or home-office arrangements."),
    ("salaries", "Employee compensation", "Itemize employee roles, headcount, wages, owner pay and contractor costs separately."),
    ("payroll_taxes", "Payroll taxes and benefits", "Confirm employer taxes and benefit costs; avoid repeating amounts already in wages or cost of goods sold."),
    ("insurance", "Insurance", "Confirm business, professional liability and workers' compensation coverage and premiums."),
    ("utilities", "Utilities", "Confirm electricity, telephone, internet and other recurring utility costs."),
    ("software", "Software", "List licenses, subscriptions, users and fixed charges."),
    ("professional_fees", "Professional services", "Confirm accounting, legal, billing and contractor arrangements."),
    ("marketing", "Marketing", "Separate channels and agency fees; link any assumed sales return to the product or service and its margin."),
    ("supplies", "Supplies", "Confirm the supplies needed to deliver the business's products or services."),
    ("cogs", "Product and service delivery costs", "Explain direct materials, direct labor and other variable delivery costs, or mark not applicable."),
    ("merchant_processing", "Payment processing", "Confirm processing volume, fees and statement period; bank settlements are not gross sales."),
)


def _number(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("amount"))
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").replace("$", ""))
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def _fact(facts, aliases):
    containers = [("", facts)]
    for section in ("expenses", "expense_breakdown", "deductions", "deductions_detail", "other_deductions_detail", "income_statement", "profit_and_loss"):
        value = facts.get(section)
        if isinstance(value, dict):
            containers.append((section + ".", value))
    for prefix, values in containers:
        for key in aliases:
            value = _number(values.get(key))
            if value is not None:
                return value, prefix + key
    normalized = {re.sub(r"[^a-z0-9]", "", alias.casefold()) for alias in aliases}
    for section in ("expense_lines", "expense_breakdown", "other_deductions_detail", "deductions_detail"):
        rows = facts.get(section)
        if not isinstance(rows, list):
            continue
        matched = [(index, row) for index, row in enumerate(rows) if isinstance(row, dict) and any(
            re.sub(r"[^a-z0-9]", "", str(row.get(key) or "").casefold()) in normalized
            for key in ("key", "category", "label", "description"))]
        if matched and all(_number(row.get("amount", row.get("value"))) is not None for _, row in matched):
            return sum(_number(row.get("amount", row.get("value"))) for _, row in matched), section
    return None, None


def _source(cockpit):
    noi = cockpit.get("noi") or {}
    headline = noi.get("headline") or noi.get("source_headline") or (cockpit.get("ebitda") or {}).get("headline") or {}
    catalog = (noi.get("review") or {}).get("sources") or []
    ids = [str(row.get("file_id")) for row in headline.get("source_refs") or []]
    if headline.get("forecast_assumption"):
        return None
    permitted = [row for row in catalog if row.get("eligible") and row.get("automatic_eligible", True)]
    selected = next((row for row in permitted if row["file_id"] in ids), None)
    if selected:
        return selected
    # A manual annual review without a source cannot silently adopt the latest
    # return from another year/currency as its expense baseline.
    if headline:
        permitted = [row for row in permitted if (not headline.get("year") or str(row.get("year")) == str(headline["year"]))
                     and (not headline.get("currency") or row.get("currency") == headline["currency"])]
    return permitted[0] if permitted else None


def _source_facts(raw, source):
    facts = raw.get("key_facts") or {}
    statements = facts.get("income_statements")
    if source and source.get("kind") == "profit_and_loss" and isinstance(statements, list):
        from app.services.document_associations import identity_matches

        entity = next((row.get("entity") for row in source.get("source_refs") or [] if row.get("entity")), None)
        matches = [row for row in statements if isinstance(row, dict)
                   and str(row.get("period_start") or "") == str(source.get("period_start") or "")
                   and str(row.get("period_end") or "") == str(source.get("period_end") or "")
                   and (not source.get("currency") or row.get("currency") == source["currency"])
                   and (not entity or identity_matches(entity, row.get("business_name") or row.get("entity_name") or facts.get("business_name")))]
        # Ambiguous same-file columns must be clarified, never combined or
        # replaced with a convenient top-level total from another period.
        return matches[0] if len(matches) == 1 else {}
    return facts


def build_guidance(analyses, cockpit, *, business_name=None, naics_code=None, industry=None):
    source = _source(cockpit)
    raw = next((row for row in analyses if source and str(row.get("file_id")) == source["file_id"]), {})
    facts = _source_facts(raw, source)
    name = business_name or facts.get("business_name") or facts.get("entity_name") or ""
    code = str(naics_code or facts.get("naics_code") or facts.get("naics") or "").strip()
    code = code if re.fullmatch(r"\d{2,6}", code) else ""
    questions = ["What products or services generate sales, at what price, and what does each cost to deliver?",
                 "Which costs are fixed, which change with sales, and which changes are feasible within your capacity?"]
    if code.startswith("62"):
        questions += ["Confirm clinician/therapist wages versus contract labor, billing services, professional insurance and premises costs.",
                      "For added visits, explain provider capacity, reimbursement per visit, payer mix and collection timing."]
    elif code.startswith(("44", "45", "31", "32", "33")):
        questions += ["Separate product purchases/materials, direct labor, freight, inventory and occupancy; avoid counting direct costs again in payroll.",
                      "For added product sales, confirm unit margin, inventory capacity and return/refund assumptions."]
    elif code.startswith("72"):
        questions += ["Separate food/materials, staffing, rent, utilities, delivery commissions and payment processing.",
                      "Confirm capacity and contribution margin before assuming additional bookings or orders."]
    else:
        questions += ["Confirm employee versus contractor costs, premises, software, insurance and payment fees, or explain why a category does not apply."]
    warnings = ["Source-derived and industry suggestions are drafts for confirmation. They are not verified current expenses or promised results.",
                "Expense detail explains the source baseline. Only explicit proposed changes affect the scenario; the tax/P&L and current NOI stay unchanged."]
    reporting = {key: (source or {}).get(key) for key in ("period_start", "period_end", "currency", "basis")}
    reporting.update(source_file_id=(source or {}).get("file_id"), source_label=(source or {}).get("file_name"), year=(source or {}).get("year"))
    groups, candidates = [], []
    if source:
        months = source.get("months_covered")
        factor = 12 / months if isinstance(months, (int, float)) and 0 < months <= 12 else None
        for key, category, label, aliases in LINES:
            value, path = _fact(facts, aliases)
            if value is None:
                continue
            scope = ":".join(str(source.get(field) or "unknown") for field in ("period_start", "period_end", "year", "currency", "basis"))
            group_key = f"source:{source['file_id']}:{scope}:{key}"
            annual = round(value * factor, 2) if factor is not None else None
            notes = ["Split this reference total into multiple expense lines; do not retain the reference total as well as its full breakdown."]
            if factor is None:
                notes.append("The source reporting duration is unconfirmed. Confirm the annual amount before adopting it.")
            elif factor != 1:
                notes.append("Annualized from the source period for planning; this is a run-rate reference, not a filed full-year result.")
            if category == "depreciation_amortization":
                notes.append("Noncash source expense; changing a depreciation entry is not a cash operating improvement.")
            group = {"key": group_key, "category": category, "label": label, "baseline_annual_amount": annual,
                     "source_amount": value, "currency": source.get("currency"), "basis": source.get("basis"),
                     "year": source.get("year"), "period_start": source.get("period_start"), "period_end": source.get("period_end"),
                     "source_file_ids": [source["file_id"]], "source_label": source.get("file_name"), "source_fact_keys": [path], "notes": notes}
            groups.append(group)
            candidates.append({"key": group_key, "source_candidate_key": group_key, "source_group_key": group_key,
                "label": label, "payee": "", "category": category, "current_amount": annual, "cadence": "annual",
                "source_file_ids": [source["file_id"]], "source_note": f"Draft from {source.get('file_name') or 'business source'}; {source.get('year') or 'period unconfirmed'}. Confirm and reconcile before relying on a change.",
                "warning": " ".join(notes), "currency": source.get("currency"), "basis": source.get("basis"), "year": source.get("year")})
        if not source.get("accepted"):
            warnings.append("The source still needs document review. Confirming this expense disclosure does not accept the source document.")
    else:
        warnings.append("No compatible historical business source is available for expense totals. Enter supported costs and ask the desk to establish or review the NOI baseline.")
    return {"policy_version": POLICY_VERSION, "business": {"name": name, "naics_code": code, "industry": industry or facts.get("industry"), "questions": questions},
            "reporting": reporting, "expense_groups": groups,
            "essentials": [{"category": category, "label": label, "prompt": prompt} for category, label, prompt in ESSENTIALS],
            "action_templates": [
                {"key": "rent", "label": "Review rent or occupancy costs", "kind": "expense_reduction", "category": "rent", "explanation": "Compare supported current and proposed rent, timing and any implementation cost."},
                {"key": "staffing", "label": "Review staffing and delivery capacity", "kind": "expense_reduction", "category": "salaries", "explanation": "Explain roles, rates, hours and capacity; do not assume service levels remain unchanged."},
                {"key": "marketing", "label": "Model a marketing investment", "kind": "marketing_investment", "category": "marketing", "explanation": "State additional spend, the linked product/service, assumed revenue per dollar and contribution margin. Added spend and delivery costs reduce the benefit."},
                {"key": "price", "label": "Review product or service pricing", "kind": "price_change", "category": "cogs", "explanation": "Use the same product/service volume and explain demand and collection assumptions."},
                {"key": "fees", "label": "Compare payment processing costs", "kind": "processing_cost_change", "category": "merchant_processing", "explanation": "Use comparable complete statement and quote data; settlement deposits are not gross card sales."}],
            "warnings": warnings}, candidates


def client_evidence(evidence):
    """An operating-form token must not expose the desk's entire evidence room."""
    evidence = evidence or {}
    guidance = deepcopy(evidence.get("guidance") or {})
    source_ids = {str(fid) for group in guidance.get("expense_groups", []) for fid in group.get("source_file_ids", [])}
    suggested_ids = {str(fid) for row in evidence.get("expense_candidates", []) for fid in row.get("source_file_ids", [])}
    suggested_ids.update(str(row["file_id"]) for row in evidence.get("merchant_sources", []))
    allowed = {row["file_id"] for row in evidence.get("files", [])
               if row["file_id"] in source_ids or row.get("eligible_for_guidance") and row["file_id"] in suggested_ids}
    candidates = []
    for original in evidence.get("expense_candidates", []):
        ids = [fid for fid in original.get("source_file_ids", []) if fid in allowed]
        if not ids:
            continue
        candidate = deepcopy(original)
        candidate["source_file_ids"] = ids
        candidates.append(candidate)
    return {"guidance": guidance,
            "files": [{key: row.get(key) for key in ("file_id", "file_name", "classification", "eligible_for_review")}
                      for row in evidence.get("files", []) if row["file_id"] in allowed],
            "expense_candidates": candidates,
            "merchant_sources": [{key: row.get(key) for key in ("source_key", "file_id", "file_name", "processor", "currency", "period_start", "period_end", "volume", "total_fees", "transactions", "effective_rate_pct", "rate_basis", "warnings", "fixed_fee", "per_transaction_fee")}
                                 for row in evidence.get("merchant_sources", []) if row["file_id"] in allowed],
            "merchant_offers": [], "warnings": list(evidence.get("warnings") or [])}


def client_evaluation(evaluation):
    from app.services.earnings_improvement import package_summary
    from app.services.financial_metric_reporting import noi_governance_brief

    safe = package_summary(evaluation)
    baseline = safe.get("baseline") or {}
    baseline["noi_governance"] = noi_governance_brief(baseline.get("noi_governance"), internal=False)
    safe["actions"] = safe.pop("adjustments")
    results = {row["id"]: row for row in evaluation.get("actions", [])}
    for row in safe["actions"]:
        if "proposed_math_valid" in results.get(row["id"], {}):
            row["proposed_math_valid"] = bool(results[row["id"]]["proposed_math_valid"])
    if "expense_changes" in evaluation:
        safe["expense_changes"] = deepcopy(evaluation["expense_changes"])
    for key in ("commands", "itemization", "historical_corrections", "goal_progress", "cash_bridge"):
        if key in evaluation:
            safe[key] = deepcopy(evaluation[key])
    safe["schema_version"] = evaluation.get("schema_version")
    return safe


def reconcile_groups(body, evidence):
    """Explain detail versus one source total without replacing current NOI."""
    from app.schemas.earnings_improvement import cost_amount

    factors = {"weekly": 52, "biweekly": 26, "semi_monthly": 24, "monthly": 12,
               "quarterly": 4, "annual": 1}
    active = [row for row in body.get("expenses", []) if not row.get("excluded")]
    groups = (evidence.get("guidance") or {}).get("expense_groups") or []
    known = {row["key"] for row in groups}
    result = []
    for group in groups:
        rows = [row for row in active if row.get("source_group_key") == group["key"]]
        amounts = [cost_amount(row) for row in rows]
        complete = bool(rows) and all(amount is not None and row.get("cadence") in factors
                                      for amount, row in zip(amounts, rows, strict=True))
        total = round(sum(amount * factors[row["cadence"]] for amount, row in zip(amounts, rows, strict=True)), 2) if complete else None
        baseline = group.get("baseline_annual_amount")
        difference = round(total - baseline, 2) if total is not None and baseline is not None else None
        status = ("no_detail" if not rows else "incomplete" if total is None or baseline is None
                  else "matches_source" if abs(difference) <= .01 else "differs_from_source")
        notes = ["A disclosed variance is a reconciliation question, not an automatic earnings improvement."]
        if status == "differs_from_source":
            notes.append("Explain whether the difference is a split, a source-period correction or an actual planned change; retain only one copy of each cost.")
        result.append({key: group.get(key) for key in ("key", "category", "label", "baseline_annual_amount", "currency", "year")} |
                      {"disclosed_annual_amount": total, "difference_annual": difference,
                       "status": status, "notes": notes})
    for key in sorted({row.get("source_group_key") for row in active if row.get("source_group_key")} - known):
        rows = [row for row in active if row.get("source_group_key") == key]
        result.append({"key": key, "category": rows[0]["category"], "label": rows[0]["label"],
            "baseline_annual_amount": None, "disclosed_annual_amount": None, "difference_annual": None,
            "status": "source_changed", "notes": ["This source group is no longer current. Relink or reconcile its expense lines before relying on the scenario."]})
    return result
