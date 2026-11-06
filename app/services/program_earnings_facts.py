"""Current factual earnings for published program rules, without scenarios.

The source calculator is shared with the cockpit and its audited corrections.
Cached AI earnings, bank cash flow and hypothetical refinance addbacks cannot
become an annual EBITDA eligibility fact.
"""

from app.services.financial_metric_reporting import validated_annual_pair


def annual_ebitda_fact(analyses: list[dict], *, entity: str | None, state: dict | None = None) -> tuple[float | None, dict]:
    from app.services.financial_cockpit import profit_and_loss, tax_years
    from app.services.financial_metric_review import prepare

    review = prepare(analyses, tax_years(analyses), profit_and_loss(analyses), {}, [], [], {},
                     state=state, entity=entity)
    pair = validated_annual_pair({"ebitda": review["ebitda"]})
    result = next((row for row in review["ebitda"]["candidates"]
                   if row.get("kind") in {"computed_ebitda", "reported_ebitda"}), {})
    if pair:
        result = pair["ebitda"]
    source = {
        "verification_status": "unknown", "kind": "validated_annual_ebitda",
        "currency": pair["currency"] if pair else result.get("currency"),
        "period": result.get("period"), "months_covered": result.get("months_covered"),
        "annualization_factor": result.get("annualization_factor"),
        "basis": "validated_profit_and_loss_annual_ebitda",
        "accounting_basis": result.get("basis"),
        "source_refs": pair["source_refs"] if pair else result.get("source_refs", []),
        "calculation_revision": review["revision"],
        "missing_inputs": result.get("missing_inputs", []),
        "corrected_inputs": [item["key"] for item in review["inputs"]
                             if item.get("correction") and item["key"].startswith("profit_and_loss.")],
        "eligibility_policy": "current_accepted_comparable_usd_profit_and_loss_6_to_12_months",
    }
    if pair and pair["currency"] == "USD":
        return pair["ebitda_annual"], {**source, "verification_status": "verified", "reason": None}
    if pair and pair["currency"] != "USD":
        reason = "The current P&L is not denominated in USD; program thresholds do not assume a currency conversion."
    elif not result:
        reason = "A current accepted P&L is needed. Tax-derived earnings estimates, bank cash flow and cached AI earnings are not annual EBITDA."
    else:
        reason = "Annual EBITDA requires a current accepted P&L for the same business, known USD currency and accounting basis, a 6–12 month reporting period, and a complete reconciled EBITDA calculation."
        if result.get("missing_inputs"):
            reason += " Missing reconciliation components: " + ", ".join(key.rsplit(".", 1)[-1].replace("_", " ") for key in result["missing_inputs"]) + "."
        if result.get("notes"):
            reason += " " + " ".join(result["notes"])
    return None, {**source, "reason": reason, "action": "request_or_review_profit_and_loss"}
