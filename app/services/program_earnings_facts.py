"""Current factual earnings for published program rules, without scenarios.

The source calculator is shared with the cockpit and its audited corrections,
so a program reads the same earnings the file shows: the accepted filed return
first, else an accepted P&L. Cached AI earnings, bank cash flow and
hypothetical refinance addbacks cannot become an annual EBITDA eligibility fact.
"""

from app.services.financial_metric_reporting import validated_annual_pair


def annual_ebitda_fact(analyses: list[dict], *, entity: str | None, state: dict | None = None) -> tuple[float | None, dict]:
    from app.services.document_associations import attach_rows, source_excluded
    from app.services.financial_cockpit import profit_and_loss, tax_years
    from app.services.financial_earnings_sources import attach
    from app.services.financial_metric_review import prepare

    all_sources = [source for row in analyses for source in (
        [row] if isinstance(row.get("identity_association"), dict)
        else attach_rows([row], profile_id=None, expected_entity=entity, state=state)
    )]
    usable = [row for row in all_sources if not source_excluded(row.get("identity_association"))]
    years = tax_years(usable, business_entity=entity)
    statement = profit_and_loss(usable, business_entity=entity)
    review = prepare(usable, years, statement, {}, [], [], {},
                     state=state, entity=entity)
    attach(review["ebitda"], review, all_sources, years, statement, {}, state=state, entity=entity)
    pair = validated_annual_pair({"ebitda": review["ebitda"]})
    selection = review["ebitda"]["source_selection"]
    result = next((row for row in review["ebitda"]["candidates"] if not selection.get("stale") and any(
        str(ref.get("file_id")) == str(selection.get("selected_file_id")) for ref in row.get("source_refs", []))), {})
    if pair:
        result = pair["ebitda"]
    source = {
        "verification_status": "unknown", "kind": "validated_annual_ebitda",
        "currency": pair["currency"] if pair else result.get("currency"),
        "period": result.get("period"), "months_covered": result.get("months_covered"),
        "annualization_factor": result.get("annualization_factor"),
        "basis": pair["basis"] if pair else "validated_profit_and_loss_annual_ebitda",
        "accounting_basis": result.get("basis"),
        "source_refs": pair["source_refs"] if pair else result.get("source_refs", []),
        "calculation_revision": review["revision"],
        "missing_inputs": result.get("missing_inputs", []),
        "corrected_inputs": [item["key"] for item in review["inputs"]
                             if item.get("correction") and item["key"].startswith(
                                 "tax_return:" if result.get("kind") == "tax_estimate" else "profit_and_loss.")],
        "eligibility_policy": "current_accepted_comparable_usd_filed_return_or_profit_and_loss",
        "selected_file_id": selection.get("selected_file_id"),
        "source_selection_stale": selection.get("stale", False),
    }
    if pair and pair["currency"] == "USD":
        return pair["ebitda_annual"], {**source, "verification_status": "verified", "reason": None}
    if pair and pair["currency"] != "USD":
        reason = "The current earnings source is not denominated in USD; program thresholds do not assume a currency conversion."
    elif not result:
        reason = "An accepted business return or a current accepted P&L is needed. Bank cash flow and cached AI earnings are not annual EBITDA."
    else:
        reason = "Annual EBITDA requires an accepted return of the same business in USD, or a current accepted P&L with known USD currency and accounting basis, a 6–12 month reporting period, and a complete reconciled EBITDA calculation."
        if result.get("missing_inputs"):
            reason += " Missing reconciliation components: " + ", ".join(key.rsplit(".", 1)[-1].replace("_", " ") for key in result["missing_inputs"]) + "."
        if result.get("notes"):
            reason += " " + " ".join(result["notes"])
    return None, {**source, "reason": reason, "action": "request_or_review_profit_and_loss"}
