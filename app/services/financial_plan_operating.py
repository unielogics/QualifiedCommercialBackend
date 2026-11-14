"""Apply a published operating plan to a financing projection, never its history.

The operating-plan evaluator owns evidence, review, scope and driver math. This
adapter consumes its validated result and uses the first-year net improvement
once, before new financing-budget expenses are deducted by the shared engine.
"""
from __future__ import annotations

from copy import deepcopy

from app.services.financial_metric_review import number


def coverage_adjustment(context, plan, dscr):
    context = context or {}
    evaluated = context.get("evaluation") or {}
    totals = evaluated.get("totals") or {}
    approved = totals.get("approved") or {}
    proposed = totals.get("proposed") or {}
    projection = (evaluated.get("projection") or {}).get("approved") or {}
    baseline = number((evaluated.get("baseline") or {}).get("earnings"))
    rows = [row for row in evaluated.get("actions") or [] if row.get("included_in_approved_projection")]
    # A one-time benefit is not recurring operating earnings. Implementation
    # costs on a recurring action are already deducted in first_year_impact.
    recurring = [row for row in rows if row.get("kind") != "one_time_adjustment" and (row.get("calculation") or {}).get("cadence") != "one_time"]
    dated = bool(recurring and all(number(row.get("first_year_impact")) is not None for row in recurring))
    first_impact = round(sum(number(row.get("first_year_impact")) for row in recurring), 2) if dated else None
    run_rate = round(sum(number(row.get("net_annual_impact")) or 0 for row in recurring), 2)
    additive = (evaluated.get("additive_totals") or {}).get("approved")
    if additive and recurring:
        # In the v2 plan changes interact (a cost follows the revenue another change adds), so the reviewed
        # scenario is taken whole instead of adding up what each change is worth on its own.
        first_impact = number(additive.get("first_year_impact")) if dated else None
        run_rate = number(additive.get("annual_impact")) or 0.0
    impact = first_impact if dated else run_rate
    result = {"status": "none", "applied": False, "source": "published_operating_plan",
        "version_number": context.get("version_number"), "version_id": context.get("version_id"),
        "baseline_annual": baseline, "annual_impact": run_rate if recurring else None,
        "first_year_impact": first_impact, "applied_annual_impact": 0.0,
        "applied_count": 0, "candidate_impacts": {}, "projected_dscr": None,
        "pending_count": sum(row.get("review_state") not in {"approved", "rejected", "excluded"} for row in evaluated.get("actions") or []),
        "proposed_first_year_impact": ((evaluated.get("projection") or {}).get("proposed") or {}).get("first_year_coverage_impact"),
        "proposed_annual_impact": proposed.get("annual_impact") if proposed.get("complete") else None,
        "proposed_dscr": None, "reason": None, "noi_eligible": projection.get("noi") is not None,
        "basis": "First 12 months of net operating improvements, after effective-date proration and implementation costs; compared with the loan's selected annual debt-service stress window."}
    if recurring and not dated:
        result.update(timing_basis="undated_run_rate",
                      basis="Undated stabilized annual run-rate after recurring implementation costs, compared with the selected annual debt-service coverage window. No dated first-year benefit is assumed.")
    if any(row.get("effect_type") == "noncash_da" for row in evaluated.get("actions") or []):
        from app.services.operating_metric_effects import metric_value

        noi_first = [number(metric_value(row, "noi", "first_year")) for row in recurring]
        noi_values = noi_first if noi_first and all(value is not None for value in noi_first) else [number(metric_value(row, "noi", "net_annual")) for row in recurring]
        noi_ready = bool(context.get("available") and context.get("state") != "client_submitted"
                         and approved.get("noi_complete") and projection.get("noi") is not None
                         and noi_values and all(value is not None for value in noi_values))
        result.update(noi_applied=noi_ready,
                      applied_noi_annual_impact=round(sum(noi_values), 2) if noi_ready else None)
    if not context.get("available"):
        result["reason"] = "No published operating expenses and earnings plan is available."
        return result
    result["status"] = "awaiting_review"
    if context.get("state") == "client_submitted":
        result["reason"] = "Client proposals are provisional until staff reviews the evidence and publishes the reviewed plan."
        return result
    if not rows:
        pending = any(row.get("review_state") not in {"rejected", "excluded"} for row in evaluated.get("actions") or [])
        blocked = any(row.get("review_state") in {"stale", "blocked", "needs_baseline_reconciliation"} for row in evaluated.get("actions") or [])
        result.update(status="blocked" if blocked else "awaiting_review" if pending else "none",
            reason="The operating plan's evidence or baseline changed; review and publish its improvements again." if blocked else "Operating improvements require staff review before inclusion in projected coverage." if pending else "No active incremental operating improvements are included in this published plan.")
        return result
    if projection.get("status") != "reviewed_illustration" or not approved.get("complete") or baseline is None:
        result.update(status="blocked", reason="The reviewed plan's earnings, business, period, currency or evidence needs reconciliation.")
        return result
    if not recurring or any(number(row.get("net_annual_impact")) is None for row in recurring):
        result.update(status="blocked", reason="Recurring improvements with known implementation costs are required for projected coverage.")
        return result
    overrides = plan.get("overrides") or {}
    from app.services.financial_plan import DOUBLE_COUNTED_ADD_BACK_KINDS
    manual_addbacks = any(row.get("kind") not in DOUBLE_COUNTED_ADD_BACK_KINDS and (number(row.get("amount_monthly")) or 0) > 0 for row in plan.get("add_backs") or [])
    if overrides.get("income_annual") is not None or manual_addbacks:
        result.update(status="blocked", reason="Reconcile the operating plan with manual income overrides and operating addbacks before stacking their earnings effects.")
        return result
    for candidate in dscr.get("candidates") or []:
        income = number(candidate.get("numerator"))
        if candidate.get("key") != "bank_cash_flow" and candidate.get("currency_comparable") and income is not None and abs(income - baseline) < .01:
            result["candidate_impacts"][candidate["key"]] = impact
    headline = (dscr.get("headline") or {}).get("key")
    if headline not in result["candidate_impacts"]:
        result.update(status="blocked", reason="The selected DSCR earnings basis does not match the operating plan's reviewed annual earnings baseline.")
        return result
    result.update(status="applied", applied=True, applied_annual_impact=impact,
        applied_count=len(recurring), reason="Reviewed operating improvements are included once in projected coverage; historical earnings and DSCR remain unchanged.")
    if result["pending_count"]:
        result["reason"] = f"{len(recurring)} reviewed change(s) included once; {result['pending_count']} other change(s) remain pending and are not included. Historical earnings and DSCR remain unchanged."
    # Preserve an explicitly incomplete proposal as unknown, not zero savings.
    return result


def finish_evaluation(result, operating, context, dscr):
    """Publish projection metadata, including a conditional client preview."""
    result["operating_plan"] = operating
    denominator = number(result.get("debt_service_after_annual"))
    operating["projected_dscr"] = result.get("headline_after") if operating["applied"] else None
    evaluated = (context or {}).get("evaluation") or {}
    proposed = (evaluated.get("totals") or {}).get("proposed") or {}
    projection = (evaluated.get("projection") or {}).get("proposed") or {}
    baseline = number((evaluated.get("baseline") or {}).get("earnings"))
    # This is a client proposal preview, not the primary reviewed projection.
    if not (evaluated.get("baseline") or {}).get("dscr_unavailable_reason") and proposed.get("complete") and projection.get("status") == "unreviewed_illustration" and baseline is not None and denominator is not None and denominator > 0:
        proposed_delta = number(projection.get("first_year_coverage_impact"))
        if proposed_delta is None:
            proposed_delta = number(proposed.get("annual_impact"))
            operating["proposed_timing_basis"] = "undated_run_rate"
        operating_costs = number(result.get("annual_new_operating_expenses", 0))
        if proposed_delta is not None and operating_costs is not None:
            operating["proposed_dscr"] = round((baseline + proposed_delta - operating_costs) / denominator, 2)
    if not operating["applied"]:
        return
    ebitda = result.get("ebitda") or {}
    impact = operating["applied_annual_impact"]
    # Scenario earnings only. The source headline and annual_before are intact.
    annual = number(ebitda.get("annual_after"))
    if annual is not None:
        ebitda["annual_after_before_operating_plan"] = annual
        ebitda["annual_after"] = round(annual + impact, 2)
        before = number(ebitda.get("annual_before"))
        ebitda["annual_change"] = round(ebitda["annual_after"] - before, 2) if before is not None else None
        factor = number(ebitda.get("annualization_factor")) or (1 if ebitda.get("months_covered") == 12 else None)
        if factor:
            before_period = number(ebitda.get("before"))
            ebitda["after"] = round(ebitda["annual_after"] / factor, 2)
            ebitda["change"] = round(ebitda["after"] - before_period, 2) if before_period is not None else None
    adjusted = ebitda.get("adjusted_annual") or {}
    if number(adjusted.get("after")) is not None:
        adjusted["after"] = round(adjusted["after"] + impact, 2)
        adjusted["change"] = round(adjusted["after"] - adjusted["before"], 2)
        adjusted["status"] = "reviewed_operating_plan_projection"
    ebitda["operating_plan"] = deepcopy(operating)
    ebitda.setdefault("notes", []).append(operating["basis"] + " Historical source earnings remain unchanged.")


def refresh_cockpit(cockpit, budget):
    """Re-evaluate the same financing engine after the versioned plan is loaded."""
    from app.services import financial_plan
    from app.services.financial_cockpit import financing_preview, proposed_terms_block
    from app.services.financial_noi import preview_scenario

    plan = cockpit.get("plan") or {}
    record = plan.get("record") or {}
    preview = financing_preview(cockpit, record, budget)
    evaluation = preview["evaluation"]
    bank, debt = cockpit.get("bank") or {}, cockpit.get("debt") or {}
    plan["evaluation"] = evaluation
    plan["impact"] = financial_plan.plan_impact(evaluation, cockpit.get("dscr") or {}, bank.get("accounts") or [])
    plan["risk"] = evaluation.get("risk")
    plan["modifications"] = financial_plan.describe_modifications(record, evaluation, debt.get("obligations") or [], bank.get("recurring") or {}, bank.get("accounts") or [])
    operating = evaluation.get("operating_plan") or {}
    if operating.get("applied"):
        plan["modifications"].append({"kind": "operating_plan", "label": f"Reviewed operating earnings plan v{operating.get('version_number')}",
            "detail": f"First-year net operating improvement {operating['applied_annual_impact']:,.2f}, published version {operating.get('version_number')}."})
    plan["levers"] = financial_plan.coverage_levers(record, evaluation, obligations_list=debt.get("obligations") or [],
        recurring=bank.get("recurring") or {}, dscr=cockpit.get("dscr") or {}, requested_amount=cockpit.get("requested_amount"),
        terms=cockpit.get("proposed_terms"), tax_years=(cockpit.get("tax") or {}).get("years"))
    if cockpit.get("noi") is not None:
        cockpit["noi"]["scenario"] = preview_scenario(cockpit["noi"], evaluation)
    cockpit["proposed_terms"] = proposed_terms_block(cockpit)
