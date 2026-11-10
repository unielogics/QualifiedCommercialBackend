from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers import application_profiles as routes
from app.schemas.deal_budget import DealBudgetPatch, DealBudgetPreview, DealCostRow
from app.services import application_terms, deal_budget, financial_cockpit
from app.tests.test_atomic_client_terms_plan import baseline, combined, payload
from app.tests.test_atomic_client_terms_plan import env as env


def cost(**changes):
    return DealCostRow.model_validate({"id": uuid4(), "label": "Reviewed cost", "category": "closing_cost", "value": 100, **changes}).model_dump(mode="json")


async def preview_cockpit(_db, profile):
    """The preview now evaluates the existing payoff context as well as costs."""
    return {"plan": {"record": deepcopy(getattr(profile, "financial_plan", None) or {})},
        "requested_amount": (getattr(profile, "current_funding_request", None) or {}).get("requested_amount"),
        "proposed_terms": {}, "debt": {"obligations": [], "service": {}}, "dscr": {}, "bank": {}}


@pytest.mark.asyncio
async def test_scoped_budget_routes_and_staff_access():
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(routes.profiles, "load_profile", AsyncMock(side_effect=HTTPException(404, "Not found"))):
        for call in (routes.get_application_deal_budget(uuid4(), actor, db),
            routes.patch_application_deal_budget(uuid4(), DealBudgetPatch(expected_revision=0), actor, db),
            routes.preview_application_deal_budget(uuid4(), DealBudgetPreview(expected_revision=0), actor, db)):
            with pytest.raises(HTTPException) as error:
                await call
            assert error.value.status_code == 404
    db.commit.assert_not_awaited()
    with pytest.raises(HTTPException) as denied:
        await routes.get_application_deal_budget(uuid4(), SimpleNamespace(role=Role.CLIENT), db)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_stale_budget_fails_before_mutating_or_projecting_fees():
    profile = SimpleNamespace(id=uuid4(), deal_budget={"revision": 3, "rows": []})
    before = deepcopy(profile.deal_budget)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await deal_budget.update(db, profile, DealBudgetPatch(expected_revision=2), SimpleNamespace(role=Role.LOAN_EXEC))
    assert error.value.status_code == 409 and profile.deal_budget == before
    db.flush.assert_not_awaited()


def test_new_platform_rows_project_once_without_counting_lender_fees_or_escrow():
    profile = SimpleNamespace()
    rows = [DealCostRow(id=uuid4(), label="QC points", category="origination", method="percent", value="2.1250", recipient_role="platform").model_dump(mode="json"),
        DealCostRow(id=uuid4(), label="Lender points", category="origination", method="percent", value=1, recipient_role="lender").model_dump(mode="json"),
        DealCostRow(id=uuid4(), label="Professional services", category="consulting", value=500).model_dump(mode="json"),
        DealCostRow(id=uuid4(), label="Reserve", category="escrow_reserve", value=2000).model_dump(mode="json")]
    deal_budget.project_legacy(profile, {"rows": rows})
    assert float(profile.forecast_fee_points) == pytest.approx(2.125)
    assert profile.forecast_consulting_fee == 500
    assert deal_budget.snapshot(SimpleNamespace(id=uuid4(), forecast_fee_points="2.1250", forecast_consulting_fee=500))["rows"][0]["value"] == "2.1250"


def test_settlement_and_precision_contract():
    with pytest.raises(ValidationError, match="two decimal"):
        DealCostRow(id=uuid4(), label="Dollar fee", category="other", value="1.234")
    with pytest.raises(ValidationError, match="paid separately"):
        DealCostRow(id=uuid4(), label="Investor closing", category="management", value=100, payer="investor", settlement="withheld")
    with pytest.raises(ValidationError):
        DealBudgetPatch(expected_revision=0, rows=[cost(value=True)])


@pytest.mark.asyncio
async def test_preview_is_nonmutating_and_accepts_explicit_zero_rate():
    profile = SimpleNamespace(id=uuid4(), deal_budget={"revision": 2, "rows": []})
    before = deepcopy(profile.deal_budget)
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(commit=AsyncMock())
    request = DealBudgetPreview(expected_revision=2, costs_confirmed=True, loan={"amount": 120000, "rate_pct": 0, "term_months": 12, "structure": "amortizing"})
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), \
        patch.object(financial_cockpit, "load_cockpit", AsyncMock(side_effect=preview_cockpit)):
        result = await routes.preview_application_deal_budget(profile.id, request, actor, db)
    assert result.calculation["investor_net_income"] == 0 and result.calculation["investor_net_roi_pct"] == 0
    assert profile.deal_budget == before
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_terms_snapshot_revision_guard_and_historical_cost_math_are_frozen(env):
    env.profile.deal_budget = {"schema_version": 1, "revision": 1, "costs_confirmed": True, "rows": [cost()], "funding_mode": "single_investor"}
    env.session.commit()
    row = await application_terms.save_terms(env.db, env.profile, env.actor, payload(expected_deal_budget_revision=1))
    env.session.commit()
    original = deepcopy(row.deal_budget_snapshot)
    original_math = deepcopy(row.deal_budget_calculation)
    env.profile.deal_budget = {**original, "revision": 2, "rows": [cost(value=500)]}
    env.session.commit()
    result = await application_terms.read_terms(env.db, env.profile, row)
    assert result.deal_budget_stale and result.current_deal_budget_revision == 2
    assert result.deal_budget_snapshot == original and result.deal_budget_calculation == original_math
    with pytest.raises(HTTPException) as error:
        await application_terms.save_terms(env.db, env.profile, env.actor, payload(expected_version=1, expected_deal_budget_revision=1))
    assert error.value.status_code == 409 and row.is_current


@pytest.mark.asyncio
async def test_stale_budget_rolls_back_combined_terms_and_financial_plan(env):
    from app.routers.application_terms import put_client_terms
    await baseline(env)
    env.profile.deal_budget = {"revision": 2, "rows": [], "costs_confirmed": True}
    env.session.commit()
    original = deepcopy(env.profile.financial_plan)
    request = combined(env, expected_deal_budget_revision=1)
    before_commits = env.db.commits
    with pytest.raises(HTTPException) as error:
        await put_client_terms(env.profile.id, request, env.actor, env.db)
    assert error.value.status_code == 409 and env.db.commits == before_commits
    assert env.db.rollbacks == 1 and env.profile.financial_plan == original


@pytest.mark.asyncio
async def test_fixed_origination_dollars_share_cap_with_points_on_terms_saves(env):
    from sqlalchemy import select

    from app.models.application_terms import ApplicationTermSheet

    rows = [DealCostRow(id=uuid4(), label="Points", category="origination", method="percent", value=2).model_dump(mode="json"),
        DealCostRow(id=uuid4(), label="Fixed origination", category="origination", value=2000).model_dump(mode="json")]
    env.profile.deal_budget = {"revision": 1, "costs_confirmed": True, "rows": rows}
    env.session.commit()
    async def enforce(_db, _profile, points):
        if points > 3:
            raise HTTPException(422, "Published three-point ceiling")
    with patch("app.services.funding_programs.enforce_qc_fee_cap_for_profile", AsyncMock(side_effect=enforce)) as cap:
        with pytest.raises(HTTPException) as error:
            await application_terms.save_terms(env.db, env.profile, env.actor, payload(amount=100000, expected_deal_budget_revision=1))
        assert error.value.status_code == 422
        assert env.session.scalars(select(ApplicationTermSheet)).all() == []
        saved = await application_terms.save_terms(env.db, env.profile, env.actor, payload(amount=200000, expected_deal_budget_revision=1))
    assert [float(call.args[2]) for call in cap.await_args_list] == [4, 3]
    assert saved.amount == 200000 and saved.version == 1
    unknown = deal_budget.calculate({"costs_confirmed": True, "rows": rows}, {})
    assert unknown["platform_origination_equivalent_points"] is None and unknown["status"] == "needs_review"


@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.FIELD_REP, Role.REGIONAL_MANAGER, Role.DEALER_PARTNER])
def test_external_cockpit_cannot_read_private_budget_copies_or_staff_recipient_names(role):
    from app.services.financial_metric_review import redact_for_external

    row = cost(recipient="Private recipient", notes="Staff commission detail", source_ref="private-source-id")
    budget = {"rows": [row], "revision": 1}
    private = {"deal_budget_snapshot": budget, "deal_budget_calculation": {"issues": ["Private recipient needs verification"]}}
    cockpit = {"deal_budget": {**budget, "calculation": deal_budget.calculate(budget, {})},
        "proposed_terms": deepcopy(private), "dscr": {"proposed": deepcopy(private)},
        "plan": {"terms_link": {"recorded": deepcopy(private)}}}
    source = deepcopy(cockpit)
    safe = redact_for_external(cockpit, SimpleNamespace(role=role))
    assert safe["deal_budget"]["rows"][0]["category"] == "closing_cost"
    for private_text in ("Private recipient", "Staff commission detail", "private-source-id", "deal_budget_snapshot"):
        assert private_text not in str(safe)
    assert cockpit == source
    assert redact_for_external(cockpit, SimpleNamespace(role=Role.LOAN_EXEC)) == source


@pytest.mark.asyncio
async def test_partial_preview_accepts_null_io_and_unknown_custom_cadence_without_inventing_returns():
    profile = SimpleNamespace(id=uuid4())
    payload = DealBudgetPreview(expected_revision=0, costs_confirmed=True,
        rows=[cost(settlement="withheld")], loan={"amount": 100000, "rate_pct": 10, "term_months": 24,
            "interest_only_months": None, "payments_per_year": None})
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), \
        patch.object(financial_cockpit, "load_cockpit", AsyncMock(side_effect=preview_cockpit)):
        response = await routes.preview_application_deal_budget(profile.id, payload, actor, db)
    assert response.calculation["net_client_cash"] == 99900
    assert response.calculation["investor_net_roi_pct"] is None
    assert response.calculation["status"] == "needs_review"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_legacy_fee_echo_does_not_invalidate_reviewed_budget():
    row = DealCostRow(id=uuid4(), label="Existing points", category="origination", method="percent", value="2.1250").model_dump(mode="json")
    profile = SimpleNamespace(id=uuid4(), deal_budget={"revision": 5, "costs_confirmed": True, "rows": [row]})
    original = deepcopy(profile.deal_budget)
    with (patch("app.services.application_terms.lock_profile_terms", AsyncMock()),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit):
        await deal_budget.sync_legacy_write(SimpleNamespace(), profile, {"forecast_fee_points": "2.125"}, SimpleNamespace(id=uuid4()))
    assert profile.deal_budget == original
    audit.assert_not_awaited()


def test_current_use_allocations_are_reconciled_without_rewriting_frozen_term_math():
    frozen = {"net_client_cash": 192500, "status": "ready", "allocation_reconciliation": {"status": "unknown"}}
    profile = SimpleNamespace(id=uuid4(), deal_budget={"revision": 1, "rows": []},
        use_of_funds=[{"category": "working_capital", "amount": 200000}])
    terms = {"source": "term_sheet", "recorded_terms_ref": {"id": "version-1"},
        "deal_budget_revision": 1, "deal_budget_snapshot": {"revision": 1, "rows": []}, "deal_budget_calculation": frozen}
    report = deal_budget.reporting(profile, terms)
    assert report["current_allocation_reconciliation"]["variance"] == -7500
    assert report["current_allocation_reconciliation"]["status"] == "overallocated"
    assert report["current_allocation_issues"]
    assert report["calculation"] == frozen and terms["deal_budget_calculation"]["status"] == "ready"


def test_current_earnings_fee_coverage_is_separate_from_frozen_quote_costs_and_roi():
    fee = DealCostRow(id=uuid4(), label="Annual servicing", category="servicing", payer="borrower", value=10000, timing="annual").model_dump(mode="json")
    budget = {"revision": 1, "costs_confirmed": True, "rows": [fee]}
    loan = {"amount": 100000, "rate_pct": 0, "term_months": 12, "structure": "amortizing",
        "coverage_context": {"income_annual": 100000, "debt_service_annual": 100000, "new_loan_annual_debt_service": 100000, "base_dscr": 1}}
    frozen = deal_budget.calculate(budget, loan)
    terms = {**loan, "source": "term_sheet", "recorded_terms_ref": {"id": "version-1"},
        "deal_budget_revision": 1, "deal_budget_snapshot": budget, "deal_budget_calculation": frozen,
        "coverage_context": {**loan["coverage_context"], "income_annual": 200000, "base_dscr": 2}}
    report = deal_budget.reporting(SimpleNamespace(id=uuid4(), deal_budget=budget), terms)
    assert report["calculation"] == frozen
    assert report["calculation"]["financing_fee_coverage"]["with_financing_fees_dscr"] == .9091
    current = report["current_financing_fee_coverage"]
    assert current["base_dscr"] == 2 and current["with_financing_fees_dscr"] == 1.8182
    assert current["earnings_basis"] == "current_file_pro_forma" and current["fee_rules_basis"] == "recorded_terms"
    assert report["calculation"]["investor_net_roi_pct"] == frozen["investor_net_roi_pct"]


def test_lower_proposal_preserves_request_uses_as_reference_without_false_overallocation():
    uses = [{"category": "working_capital", "amount": 200000}]
    original = deepcopy(uses)
    terms = {"amount": 150000, "requested_amount": 200000, "rate_pct": 10, "term_months": 12, "structure": "amortizing"}
    result = deal_budget.calculate({"costs_confirmed": True, "rows": []}, terms, uses)
    allocation = result["allocation_reconciliation"]
    assert allocation["status"] == "different_scope" and allocation["scope"] == "requested_reference"
    assert allocation["requested_uses_total"] == 200000 and allocation["proposed_loan_amount"] == 150000
    assert allocation["allocated_uses"] is None and allocation["variance"] is None
    assert result["status"] == "ready" and uses == original


def test_production_proposal_uses_are_preferred_and_ambiguous_gross_fee_allocation_remains_open():
    requested = [{"category": "working_capital", "amount": 200000}]
    terms = {"amount": 150000, "requested_amount": 200000, "rate_pct": 10, "term_months": 12,
        "proposal_use_of_funds": {"inventory": 100000, "working_capital": 50000}, "proposal_use_of_funds_amount": 150000}
    budget = {"costs_confirmed": True, "rows": []}
    result = deal_budget.calculate(budget, terms, requested)
    assert result["allocation_reconciliation"]["scope"] == "proposed_loan"
    assert result["allocation_reconciliation"]["status"] == "balanced"
    withheld = {**budget, "rows": [cost(value=5000, settlement="withheld")]}
    result = deal_budget.calculate(withheld, terms, requested)
    assert result["allocation_reconciliation"]["scope"] == "proposed_gross_budget"
    assert result["allocation_reconciliation"]["status"] == "needs_closing_reconciliation"
    assert result["status"] == "ready" and result["net_client_cash"] == 145000
    # Explicit net-purpose allocations remain checkable against actual proceeds.
    terms["proposal_use_of_funds"] = {"inventory": 100000, "working_capital": 45000, "closing_fees": 5000}
    assert deal_budget.calculate(withheld, terms, requested)["allocation_reconciliation"]["status"] == "balanced"


def test_zero_investor_reserve_does_not_require_a_refund_to_compute_known_returns():
    reserve = DealCostRow(id=uuid4(), label="Zero reserve", category="escrow_reserve", payer="investor", value=0).model_dump(mode="json")
    result = deal_budget.calculate({"costs_confirmed": True, "rows": [reserve]},
        {"amount": 100000, "rate_pct": 0, "term_months": 12, "structure": "amortizing"})
    assert result["investor_reserves"] == 0 and result["investor_initial_outlay"] == 100000
    assert result["investor_net_roi_pct"] == 0 and result["status"] == "ready"


@pytest.mark.asyncio
async def test_preview_lower_loan_uses_exact_current_request_context_without_changing_it():
    profile = SimpleNamespace(id=uuid4(), current_funding_request={"requested_amount": 200000},
        use_of_funds=[{"category": "working_capital", "amount": 200000}])
    before = deepcopy(vars(profile))
    request = DealBudgetPreview(expected_revision=0, costs_confirmed=True,
        loan={"amount": 150000, "rate_pct": 10, "term_months": 12})
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), \
        patch.object(financial_cockpit, "load_cockpit", AsyncMock(side_effect=preview_cockpit)):
        result = await routes.preview_application_deal_budget(profile.id, request, actor, SimpleNamespace())
    assert result.calculation["allocation_reconciliation"]["status"] == "different_scope"
    assert result.calculation["status"] == "ready" and vars(profile) == before
