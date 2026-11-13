"""Proposal cost effects, without rewriting any historical earnings evidence."""
import hashlib
import json
from copy import deepcopy


def validate_write(budget, expected_revision, terms):
    from fastapi import HTTPException

    from app.services.deal_budget import assert_principal_matches

    actual = int(budget.get("revision") or 0)
    if expected_revision is not None and expected_revision != actual or actual > 0 and expected_revision is None:
        raise HTTPException(409, "Deal costs changed or their revision was not included. Reload before saving this loan or plan.")
    if terms.get("amount"):
        assert_principal_matches(budget, terms)


def apply_to_terms_calculation(calculation, budget, costs):
    relevant = [row for row in budget.get("rows") or [] if row.get("enabled", True) and row.get("payer", "borrower") == "borrower"
        and row.get("timing", "closing") != "closing" and row.get("category") != "escrow_reserve"
        and row.get("coverage_treatment", "new_cost") == "new_cost"]
    coverage = costs.get("financing_fee_coverage") or {}
    update = {"financing_fee_coverage": deepcopy(coverage), "principal_solution": deepcopy(costs.get("principal_solution"))}
    if relevant:
        denominator = coverage.get("annual_debt_service_including_fees")
        numerator = coverage.get("income_annual_after_new_operating_expenses")
        value = numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None
        update.update(dscr_after=round(value, 3) if value is not None else None,
            cash_flow_value=numerator, projected_annual_debt_service=denominator,
            dscr_status="ready" if calculation.dscr_before is not None and value is not None else "needs_evidence",
            dscr_explanation=calculation.dscr_explanation + " Projected coverage includes new recurring financing charges and deducts new operating costs once; historical earnings remain unchanged.")
    return calculation.model_copy(update=update)


def profit_projection(net_profit, ebitda, evaluation):
    source = net_profit.get("conclusion") or {}
    currency = (ebitda.get("headline") or {}).get("currency")
    before = source.get("value") if source.get("key") in {"tax_return", "profit_and_loss"} else None
    financing = evaluation.get("annual_new_financing_fees", 0)
    operating = evaluation.get("annual_new_operating_expenses", 0)
    after = round(before - financing - operating, 2) if before is not None and financing is not None and operating is not None and (currency == "USD" or financing + operating == 0) else None
    return {"annual_before": before, "annual_after_new_budget_expenses": after,
        "annual_new_financing_expenses": financing, "annual_new_operating_expenses": operating,
        "currency": currency, "period": source.get("period"), "status": "illustrative_partial_projection" if after is not None else "unavailable",
        "note": "Illustrates new recurring budget expenses only; historical source profit is preserved. Closing-fee accounting recognition and changes in old/new loan interest require separate reconciliation. Loan principal, escrow and funding proceeds are not earnings."}


def loan_from_plan(plan):
    return {"amount": plan.get("new_loan_amount"), "rate_pct": plan.get("new_loan_rate_pct"),
        "term_months": plan.get("new_loan_term_months"),
        "structure": "interest_only" if plan.get("new_loan_interest_only") else plan.get("new_loan_structure"),
        "interest_only_months": plan.get("new_loan_interest_only_months") or 0,
        "repayment_config": plan.get("new_loan_repayment_config"), "payments_per_year": plan.get("new_loan_payments_per_year") or 12}


def solve_plan(plan, budget):
    """Use the solved principal for a labelled preview; only save writes it."""
    if budget is None or not plan.get("new_loan_amount"):
        return plan, None
    from app.services.deal_budget import solve_principal

    solution = solve_principal(budget, loan_from_plan(plan))
    effective = dict(plan)
    if solution.get("status") == "calculated" and solution.get("gross_loan_amount") is not None:
        effective["new_loan_amount"] = solution["gross_loan_amount"]
        if solution.get("requires_apply"):
            effective["new_payment_monthly"] = None
    return effective, solution


def coverage_context(evaluation):
    """Budget calculations always start before new costs, avoiding a second charge."""
    return {"income_annual": evaluation.get("income_after_annual_before_budget", evaluation.get("income_after_annual")),
        "debt_service_annual": evaluation.get("debt_service_after_annual_before_financing_fees", evaluation.get("debt_service_after_annual")),
        "new_loan_annual_debt_service": evaluation.get("new_loan_annual_debt_service",
            evaluation.get("new_payment_monthly") * 12 if evaluation.get("new_payment_monthly") is not None else None),
        "base_dscr": evaluation.get("headline_after_before_budget", evaluation.get("headline_after")),
        "basis": evaluation.get("headline_key"),
        "operating_plan": deepcopy(evaluation.get("operating_plan"))}


def apply_to_evaluation(result, budget, principal_solution=None):
    if budget is None or not result.get("loan_terms"):
        return
    # Bind downstream allocation reviews to the current classifications and
    # revision, even when equal dollar totals survive a reclassification.
    # Only the opaque digest crosses into the shared cockpit evaluation.
    result["financing_budget_fingerprint"] = hashlib.sha256(
        json.dumps(budget, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    from app.services.deal_budget import calculate
    from app.services.financial_cockpit import dscr_status

    loan = dict(result["loan_terms"])
    loan["coverage_context"] = coverage_context(result)
    calc = calculate(budget, loan)
    if principal_solution is not None:
        calc["principal_solution"] = principal_solution
    result["principal_solution"] = deepcopy(calc.get("principal_solution"))
    result["financing_budget_calculation"] = calc
    relevant = [row for row in budget.get("rows") or [] if row.get("enabled", True) and row.get("payer", "borrower") == "borrower"
        and row.get("timing", "closing") != "closing" and row.get("category") != "escrow_reserve"
        and row.get("coverage_treatment", "new_cost") == "new_cost"]
    new_financing = any(row.get("earnings_treatment", "financing_cost") == "financing_cost" for row in relevant)
    new_operating = any(row.get("earnings_treatment") == "operating_expense" for row in relevant)
    coverage = calc.get("financing_fee_coverage") or {}
    finance = coverage.get("annual_borrower_financing_fees") if new_financing else 0.0
    operating = calc.get("annual_new_operating_expenses") if new_operating else 0.0
    result.update(income_after_annual_before_budget=result.get("income_after_annual"),
        headline_after_before_budget=result.get("headline_after"),
        debt_service_after_annual_before_financing_fees=result.get("debt_service_after_annual"),
        annual_new_financing_fees=finance, annual_new_operating_expenses=operating,
        financing_fee_coverage=deepcopy(coverage))
    denominator = coverage.get("annual_debt_service_including_fees") if new_financing else result.get("debt_service_after_annual")
    if principal_solution and principal_solution.get("status") != "calculated":
        denominator = None
        result["coverage_unavailable_reason"] = "The loan principal cannot be solved from the cost inputs. Review the target proceeds and financed costs."
    elif (new_financing and finance is None) or (new_operating and operating is None):
        denominator = None
        result["coverage_unavailable_reason"] = "New recurring costs require reviewed amounts and classifications before projected coverage is available."
    result["debt_service_after_annual"] = denominator
    result["debt_service_after_monthly"] = round(denominator / 12, 2) if denominator is not None else None
    for candidate in result.get("candidates") or []:
        original = candidate.get("numerator_after")
        candidate["numerator_after_before_budget"] = original
        numerator = round(original - operating, 2) if original is not None and operating is not None else None
        candidate["numerator_after"] = numerator
        candidate["value_after"] = round(numerator / denominator, 2) if numerator is not None and denominator is not None and denominator > 0 else None
        candidate["status_after"] = dscr_status(candidate["value_after"])
        candidate["no_debt_left"] = bool(denominator is not None and denominator <= 0 and numerator is not None and numerator > 0)
    headline = next((row for row in result.get("candidates") or [] if row.get("key") == result.get("headline_key")), None)
    if headline:
        result.update(income_after_annual=headline["numerator_after"], headline_after=headline["value_after"],
            headline_after_status=headline["status_after"], no_debt_left=headline["no_debt_left"])
    debt_only = result["debt_service_after_annual_before_financing_fees"]
    result["headline_after_before_financing_fees"] = (round(result["income_after_annual"] / debt_only, 2)
        if result.get("income_after_annual") is not None and debt_only is not None and debt_only > 0 else None)
    # The historical replay has no loan start date. Apply a clearly identified
    # annual-window monthly equivalent, never a made-up dated debit.
    monthly = (finance + operating) / 12 if finance is not None and operating is not None else None
    if relevant:
        result["budget_cash_proxy_monthly"] = round(monthly, 2) if monthly is not None else None
        for index, row in enumerate(result.get("replay") or []):
            for key in ("withdrawals_after", "net_after", "ending_after", "lowest_after", "added", "cumulative"):
                value = row.get(key)
                if value is None or monthly is None:
                    row[key] = None
                else:
                    sign = 1 if key in {"withdrawals_after", "added"} else -1
                    periods = index + 1 if key in {"ending_after", "lowest_after", "cumulative"} else 1
                    row[key] = round(value + sign * monthly * periods, 2)
        replay = result.get("replay") or []
        after = [row["net_after"] for row in replay if row.get("net_after") is not None]
        result["net_after_avg"] = round(sum(after) / len(after), 2) if after else None
        result["ending_after_last"] = replay[-1].get("ending_after") if replay else None
        result["cash_kept"] = replay[-1].get("cumulative") if replay else None
        result["months_dipping_negative_after"] = sum(row["lowest_after"] < 0 for row in replay if row.get("lowest_after") is not None) if monthly is not None else None
        result["replay_projection_available"] = bool(result.get("replay_projection_available") and monthly is not None)
        result["replay_note"] = (result.get("replay_note") or "") + " New recurring budget charges use their annual coverage-window monthly equivalent; existing baseline charges are not deducted again."
    for key in ("debt_service_after_regular_monthly", "debt_service_after_interest_only_monthly"):
        if new_financing:
            result[key] = round(result[key] + finance / 12, 2) if result.get(key) is not None and finance is not None else None
    for phase in ("regular", "interest_only"):
        after, before = result.get(f"debt_service_after_{phase}_monthly"), result.get("debt_service_before_monthly")
        result[f"net_{phase}_monthly_change"] = round(after - before, 2) if after is not None and before is not None else None
    result["closing_cash_effects"] = {key: calc.get(key) for key in ("gross_loan_amount", "net_client_cash", "borrower_financed_fees", "borrower_financed_reserves", "borrower_withheld_fees", "borrower_withheld_reserves", "borrower_paid_separately")}
    result["closing_cash_note"] = "Closing costs, reserves and funding proceeds are shown at loan closing, separately from historical statement replay. No loan commencement date or dated closing debit is invented."
    if isinstance(result.get("risk"), dict):
        if relevant:
            from app.services.loan_schedule import risk_words

            result["risk"]["loan_schedule_only"] = deepcopy(result["risk"])
            for key in ("coverage_by_period", "cash_coverage_by_period"):
                result["risk"][key] = []
            for key in ("lowest_cash_coverage", "peak_cash_shortfall_monthly", "peak_cash_shortfall_period", "peak_cash_shortfall_month", "lowest_coverage", "lowest_coverage_month", "coverage_during_interest_only", "coverage_after_interest_only"):
                result["risk"][key] = None
            result["risk"]["coverage_unavailable_reason"] = "Payment-level coverage with budget costs requires aligned fee timing; use the fee-inclusive annual plan DSCR. The separately labelled loan-only reference excludes budget costs."
            result["risk"]["words"] = risk_words(result.get("schedule") or {}) + " " + result["risk"]["coverage_unavailable_reason"]
        result["risk"]["financing_fee_coverage"] = deepcopy(coverage)
        result["risk"]["coverage_scope"] = "Loan schedule risk; financing-budget coverage is shown separately and included in the headline plan DSCR."
