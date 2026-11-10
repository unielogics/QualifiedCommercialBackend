"""Read-only financing controls derived from the saved financial workspace.

These figures describe a proposal. They never confer approval, acceptance or
funding, and reading them does not overwrite explicit lifecycle decisions.
"""
from app.services.deal_budget import snapshot as budget_snapshot
from app.services.investor_collateral import snapshot


def from_cockpit(profile, cockpit):
    plan = cockpit.get("plan") or {}
    impact = plan.get("impact") or {}
    evaluation = plan.get("evaluation") or {}
    record = plan.get("record") or {}
    headline = (cockpit.get("dscr") or {}).get("headline") or {}
    coverage = impact.get("dscr") or {}
    service = impact.get("coverage_debt_service_monthly") or impact.get("debt_service_monthly") or {}
    proposal = cockpit.get("proposed_terms") or {}
    terms_link = plan.get("terms_link") or {}
    recorded = terms_link.get("recorded") or {}
    collateral = snapshot(profile)
    budget = budget_snapshot(profile)
    active = bool(evaluation.get("active"))
    current_dscr = headline.get("value")
    after = coverage.get("after", evaluation.get("headline_after")) if active else current_dscr
    current_service = service.get("before", (cockpit.get("debt") or {}).get("service", {}).get("monthly"))
    with_service = service.get("after", evaluation.get("debt_service_after_monthly")) if active else current_service
    amount = record.get("new_loan_amount")
    # A saved cleared scenario intentionally leaves recorded terms outside
    # the plan. Do not resurrect their amount into the current controls.
    if amount is None and terms_link.get("state") != "recorded_not_in_plan":
        amount = proposal.get("amount")
    return {
        "proposed_amount": amount,
        "current_dscr": current_dscr,
        "with_plan_dscr": after,
        "current_annual_debt_service": current_service * 12 if current_service is not None else None,
        "with_plan_annual_debt_service": with_service * 12 if with_service is not None else None,
        "source": headline.get("label") or headline.get("key"),
        "terms_ref": {key: recorded.get(key) for key in ("kind", "id", "version", "label")} if recorded else None,
        "terms_state": terms_link.get("state") or "none",
        "plan_updated_at": record.get("updated_at"),
        "collateral_revision": collateral["revision"],
        "terms_collateral_revision": recorded.get("collateral_revision"),
        "collateral_stale": bool(recorded and recorded.get("collateral_revision") != collateral["revision"]),
        "deal_budget_revision": budget["revision"],
        "terms_deal_budget_revision": recorded.get("deal_budget_revision"),
        "deal_budget_stale": bool(recorded and recorded.get("deal_budget_revision") != budget["revision"]),
        "financing_fee_coverage": ((cockpit.get("deal_budget") or {}).get("calculation") or {}).get("financing_fee_coverage"),
    }


async def read(db, profile):
    from app.services.financial_cockpit import load_cockpit

    return from_cockpit(profile, await load_cockpit(db, profile))
