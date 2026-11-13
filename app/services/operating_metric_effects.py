"""Separate accounting NOI, coverage earnings and cash for noncash D&A."""
from __future__ import annotations

from app.services.financial_metric_review import number


def noncash_da(action, expenses):
    linked = expenses.get(action.get("expense_row_id")) or {}
    return (linked.get("category") == "depreciation_amortization"
            or action.get("kind") == "expense_group_change" and action.get("expense_category") == "depreciation_amortization")


def basis(cockpit, action=None, body=None):
    noi = cockpit.get("noi") or {}
    headline = noi.get("headline") or {}
    earnings = (cockpit.get("ebitda") or {}).get("headline") or {}
    # The source reporting policy explicitly leaves expensed D&A in business
    # NOI. An arbitrary manual/custom metric does not establish that operand.
    noi_known = bool((noi.get("definition") or {}).get("includes_depreciation_and_amortization") is True
                     and headline.get("kind") in {"business_operating_income", "tax_derived_noi"}
                     and number(headline.get("annualized_value")) is not None)
    if action is not None and body is not None:
        from app.services.operating_plan_commands import participants
        from app.services.operating_plan_guidance import reconcile_groups

        evidence = cockpit.get("operating_evidence") or {}
        references = {row["key"]: row for row in (evidence.get("guidance") or {}).get("expense_groups", [])}
        groups = {row["key"]: row for row in reconcile_groups(body, evidence)}
        files = {str(row.get("file_id")): row for row in evidence.get("files", [])}
        noi_files = {str(row.get("file_id")) for row in headline.get("source_refs", []) if row.get("file_id")}
        rows = participants(action, body)
        for row in rows:
            reference = references.get(row.get("source_group_key")) or {}
            scope = all(reference.get(key) == body.get(key) for key in ("period_start", "period_end", "currency", "basis"))
            accepted = bool(reference.get("source_file_ids") and all(files.get(str(fid), {}).get("eligible_for_review") for fid in reference["source_file_ids"]))
            same_noi_source = bool(noi_files & {str(fid) for fid in reference.get("source_file_ids", [])})
            if not scope or not accepted or not same_noi_source or reference.get("category") != "depreciation_amortization" or groups.get(row.get("source_group_key"), {}).get("status") != "matches_source":
                noi_known = False
        noi_known = bool(noi_known and rows)
    earnings_known = bool(earnings.get("kind") in {"computed_ebitda", "reported_ebitda", "tax_estimate"}
                          and number(earnings.get("annualized_value")) is not None)
    return {"noi": noi_known, "earnings": earnings_known, "cash": True,
            "issues": {
                "noi": [] if noi_known else ["Confirm that the selected source NOI includes this expensed depreciation and amortization."],
                "earnings": [] if earnings_known else ["Confirm the selected coverage earnings basis excludes depreciation and amortization before showing its earnings effect."],
                "cash": []}}


def effects(cockpit, accounting_gross, implementation, recurring_cost, one_time_cost, fraction, *, action=None, body=None):
    policy = basis(cockpit, action, body)
    result = {"effect_type": "noncash_da", "metric_issues": policy["issues"],
              "accounting_gross_annual_impact": round(accounting_gross, 2) if accounting_gross is not None else None}
    for metric in ("noi", "earnings", "cash"):
        gross = accounting_gross if metric == "noi" else 0.0 if accounting_gross is not None else None
        if not policy[metric]:
            gross = None
        net = gross - recurring_cost if gross is not None and implementation is not None else None
        first = net * fraction - one_time_cost if net is not None and fraction else 0.0 if net is not None and fraction == 0 else None
        result[metric + "_gross_annual_impact"] = round(gross, 2) if gross is not None else None
        result[metric + "_net_annual_impact"] = round(net, 2) if net is not None else None
        result[metric + "_first_year_impact"] = round(first, 2) if first is not None else None
    return result


def metric_value(row, metric, period):
    """Presence matters: an explicitly unknown metric never falls back."""
    key = f"{metric}_{period}_impact"
    fallback = {"gross_annual": "gross_annual_impact", "net_annual": "net_annual_impact", "first_year": "first_year_impact"}[period]
    return row.get(key) if key in row else row.get(fallback)


def plan_comparison(evaluation, loan, *, comparable, noi_comparable, same_basis, debt_currency_ok, loan_evaluation=None):
    """Present the same annual scenarios in the file summary and the live editor."""
    base = evaluation["baseline"]
    feedback = evaluation["draft_feedback"]
    summary = feedback["summary"]
    working = summary["working"]
    reviewed = evaluation["projection"]["approved"]
    approved = [row for row in evaluation["actions"] if row["included_in_approved_projection"]]
    approved_total = evaluation["totals"]["approved"]
    has_terms = loan["loan_status"] != "not_saved"
    ready_terms = has_terms and working["refinance_annual_debt_service"] is not None
    current_debt = base["annual_debt_service"] if debt_currency_ok else None
    plan_debt = working["refinance_annual_debt_service"] if has_terms else current_debt
    current_cash = working["cash_after_debt_before_annual"]
    cash_delta = approved_total.get("cash_annual_impact", approved_total["annual_impact"])
    loan_cash = loan["net_loan_cash_change_monthly"] if has_terms else 0
    reviewed_cash = None
    if approved_total["complete"] and current_cash is not None and cash_delta is not None and loan_cash is not None:
        reviewed_cash = round(current_cash + cash_delta + 12 * loan_cash, 2)

    def margin(noi, rows, ready):
        revenue = number(base["revenue"])
        changes = [row["revenue_change_annual"] for row in rows]
        if not (ready and comparable and noi_comparable and same_basis) or noi is None or revenue is None or None in changes:
            return None
        revenue += sum(changes)
        return round(100 * noi / revenue, 2) if revenue > 0 else None

    reviewed_ratio = reviewed["dscr_current"] if current_debt is not None and base["currency"] == "USD" else None
    if has_terms:
        reviewed_ratio = reviewed["dscr_with_refinance"] if ready_terms else None

    def coverage_gap(earnings, charges):
        if base["currency"] != "USD" or plan_debt is None or plan_debt <= 0 or earnings is None or charges is None:
            return None
        return round(max(0, 1.25 * plan_debt - (earnings - charges)), 2)

    charges = reviewed.get("annual_new_operating_expenses") if has_terms else 0
    loan_evaluation = loan_evaluation or {}
    schedule = loan_evaluation.get("schedule") or {}
    repayment = schedule.get("repayment_config") or {}
    terms = loan_evaluation.get("loan_terms") or {}
    maturity = schedule.get("maturity_balloon") if "maturity_balloon" in schedule else terms.get("balloon", schedule.get("balloon"))
    coverage_basis = ("annualized_recorded_payment" if loan_evaluation.get("new_loan_annual_debt_service_basis") == "annualized_recorded_payment"
                      else schedule.get("coverage_basis"))
    regular_before = loan.get("before_debt_service_annual") if has_terms else current_debt
    regular_after = loan.get("after_debt_service_annual") if has_terms else current_debt
    payment_breakdown = {
        "regular_before_monthly": loan.get("before_debt_service_monthly") if debt_currency_ok else None,
        "regular_after_monthly": loan.get("after_debt_service_monthly") if debt_currency_ok and has_terms else None,
        "regular_before_annual": regular_before if debt_currency_ok else None,
        "regular_after_annual": regular_after if debt_currency_ok else None,
        "coverage_before_annual": current_debt,
        "coverage_after_annual": plan_debt,
        "new_loan_regular_monthly": number(loan_evaluation.get("new_regular_payment_monthly")) if debt_currency_ok else None,
        "retained_debt_monthly": number(loan_evaluation.get("debt_service_before_new_payment_monthly")) if debt_currency_ok else None,
        "financing_fees_monthly": loan.get("new_financing_fees_monthly") if debt_currency_ok else None,
        "maturity_balance": number(maturity) if debt_currency_ok else None,
        "coverage_basis": coverage_basis,
        "term_months": number(terms.get("term_months")),
        "amortization_months": number(repayment.get("amortization_months")),
        "payments_per_year": number(schedule.get("periods_per_year")),
    }
    return {
        "loan_basis": "saved_terms" if ready_terms else "incomplete_terms" if has_terms else "current_debt",
        "payment_breakdown": payment_breakdown,
        "loan_cash_monthly": loan_cash if has_terms else None,
        "target_gap": {"proposed": coverage_gap(working["earnings"], charges),
            "approved": coverage_gap(reviewed["earnings"], charges)},
        "proposed_basis": working["basis"],
        "baseline": {"earnings": base["earnings"], "noi": base["noi"], "dscr": working["dscr_current_debt_before"],
            "cash_after_debt": current_cash, "operating_margin": margin(base["noi"], [], True), "debt_service": current_debt},
        "proposed": {"earnings": working["earnings"], "noi": working["noi"],
            "dscr": working["dscr_with_refinance_after"] if has_terms else working["dscr_current_debt_after"],
            "cash_after_debt": working["cash_after_debt_after_annual"],
            "operating_margin": margin(working["noi"], feedback["actions"], summary["complete"]), "debt_service": plan_debt},
        "approved": {"earnings": reviewed["earnings"], "noi": reviewed["noi"], "dscr": reviewed_ratio,
            "cash_after_debt": reviewed_cash, "operating_margin": margin(reviewed["noi"], approved, approved_total["complete"]),
            "debt_service": plan_debt},
    }
