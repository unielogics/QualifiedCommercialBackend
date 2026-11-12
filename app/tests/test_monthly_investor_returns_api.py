"""Real lazy routes, permissions and frozen-source boundaries; synthetic only."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.capital_partners import enums as E
from app.capital_partners.models import CpListingEvent
from app.capital_partners.services import ppm
from app.enums import Role
from app.services import application_profiles, financial_cockpit, investor_returns
from app.services.deal_budget import calculate
from app.tests.test_capital_partners import Scenario
from app.tests.test_deal_budget_independent_math import budget, cost, terms
from app.tests.test_marketplace_ppm_lifecycle import (
    completed_draft,
    configure,
    ppm_harness,
    review_payload,
)
from app.tests.test_ppm_saved_source_stability import linked
from app.tests.test_ppm_saved_source_stability import real_sources as real_sources


def financials(amount=100000):
    saved = budget([cost(label="Private Provider charge", recipient="Private Provider", notes="Private operator note",
        source_ref="private-source", category="monitoring", value=100, timing="monthly")],
        payoff_terms={"interest_policy": "accrued_only", "charge_amount": 0})
    saved.update(revision=3, updated_at="2026-01-01T00:00:00+00:00")
    loan = {**terms(amount=amount), "source": "term_sheet", "recorded_terms_ref": {"version": 4}}
    current = deepcopy(saved)
    current["rows"][0]["value"] = 900
    current["revision"] = 9
    return {"proposed_terms": loan, "deal_budget": {"basis": "recorded_terms", "recorded": saved,
            "current": current, "calculation": calculate(saved, loan)}, "bank_account": "private-account"}


@pytest.mark.asyncio
async def test_member_and_admin_price_the_same_frozen_budget_without_live_file_or_private_data(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin)
        snapshot = {"refreshed_at": "2026-01-02T00:00:00+00:00", "financials": financials()}
        listing.source_snapshot = deepcopy(snapshot)
        live = AsyncMock(side_effect=AssertionError("Frozen offering pricing must not load a live file"))
        monkeypatch.setattr(financial_cockpit, "load_cockpit", live)
        admin_response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns?exit_month=8&investment_amount=25000")
        assert admin_response.status_code == 200, admin_response.text
        member, _ = await scenario.member("monthly-investor@example.test")
        h.current = member
        member_response = await h.client.get(f"/api/v1/capital/listings/{listing.ref}/investor-returns?exit_month=8&investment_amount=25000")
        assert member_response.status_code == 200, member_response.text
        staff, public = admin_response.json(), member_response.json()
        assert staff["holding"] == public["holding"]
        assert staff["selected_exit"] == public["selected_exit"]
        assert public["selected_exit"]["total_fees"] == 200  # Frozen100/month, not the900 draft.
        assert public["selected_exit"]["net_income"] == 1800
        assert public["source"]["kind"] == "published_listing" and staff["source"]["kind"] == "listing_saved"
        assert public["source"]["id"] == listing.ref
        assert public["source"]["budget_revision"] == 3 and public["source"]["terms_version"] == 4
        assert public["request"] == {"exit_month": 8, "investment_amount": 25000}
        assert public["full_term"]["net_income"] == 2700
        serialized = json.dumps(public)
        for secret in ("Private Provider", "Private operator note", "private-source", "private-account",
                       snapshot["financials"]["deal_budget"]["recorded"]["rows"][0]["id"]):
            assert secret not in serialized
        live.assert_not_awaited()
        await h.db.refresh(listing)
        assert listing.source_snapshot == snapshot


@pytest.mark.parametrize("gate", ["requested", "waiting", "suspended"])
@pytest.mark.asyncio
async def test_member_gate_runs_before_projection_cache(monkeypatch, gate):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        user, member = await scenario.member(f"monthly-{gate}@example.test", cleared=False)
        if gate == "waiting":
            member.status = E.MemberStatus.APPROVED
            member.waiting_period_ends_at = datetime.now(UTC) + timedelta(days=2)
        elif gate == "suspended":
            member.status = E.MemberStatus.SUSPENDED
        h.current = user
        pricing = Mock(side_effect=AssertionError("Unauthorized readers must not enter the cache"))
        monkeypatch.setattr(investor_returns, "project", pricing)
        response = await h.client.get(f"/api/v1/capital/listings/{listing.ref}/investor-returns")
        assert response.status_code == 403
        pricing.assert_not_called()


@pytest.mark.parametrize("listing_status", ["draft", "withdrawn"])
@pytest.mark.asyncio
async def test_hidden_offering_is_not_a_projection_access_path(monkeypatch, listing_status):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        await h.db.refresh(listing)
        listing.status = listing_status
        h.current, _ = await scenario.member(f"hidden-{listing_status}@example.test")
        pricing = Mock(side_effect=AssertionError("Hidden offering cache must not be accessed"))
        monkeypatch.setattr(investor_returns, "project", pricing)
        assert (await h.client.get(f"/api/v1/capital/listings/{listing.ref}/investor-returns")).status_code == 404
        pricing.assert_not_called()


@pytest.mark.asyncio
async def test_refreshed_unpublished_candidate_has_no_member_projection(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False, offered="100000")
        listing.loan_amount, listing.note_rate_pct = 100000, 12
        listing.source_snapshot = {"financials": financials()}
        context, _render, _stored = configure(monkeypatch, listing)
        draft = await completed_draft(h, listing, context)
        base = f"/api/v1/capital/admin/listings/{listing.id}"
        assert (await h.client.post(base + "/ppm/review", json=review_payload(draft, context))).status_code == 200
        published = await h.client.post(base + "/publish", json={"confirmed": True, "reason": "Publish synthetic current offering"})
        assert published.status_code == 200, published.text
        context["fingerprint"] = "b" * 64
        listing.source_snapshot = {"financials": financials(225000)}
        candidate = await h.client.get(base + "/investor-returns")
        assert candidate.status_code == 200 and candidate.json()["offering_investor_capital"] == 225000
        h.current, _ = await scenario.member("waiting-publication@example.test")
        pricing = Mock(side_effect=AssertionError("Unpublished candidate must not be priced for members"))
        monkeypatch.setattr(investor_returns, "project", pricing)
        response = await h.client.get(f"/api/v1/capital/listings/{listing.ref}/investor-returns")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "offering_update_pending_publication"
        assert "holding" not in response.json() and "net_income" not in response.text
        pricing.assert_not_called()


@pytest.mark.parametrize("query", ["exit_month=-1", "exit_month=0", "exit_month=602", "exit_month=nan", "exit_month=inf",
    "investment_amount=0", "investment_amount=-1", "investment_amount=nan", "investment_amount=inf", "investment_amount=1.001",
    "investment_amount=25000.001", "investment_amount=1000000", "investment_amount=1e40"])
@pytest.mark.asyncio
async def test_invalid_query_returns_validation_error_not_a_server_failure(monkeypatch, query):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        listing.source_snapshot = {"financials": financials()}
        response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns?{query}")
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(("budget_currency", "loan_currency", "available"), [
    (None, None, False), ("", "", False), ("USD", None, True), (None, "USD", True),
])
def test_currency_must_be_confirmed_before_returning_any_monetary_illustration(budget_currency, loan_currency, available):
    saved = financials()["deal_budget"]["recorded"]
    loan = terms()
    saved["currency"], loan["currency"] = budget_currency, loan_currency
    original = calculate(saved, loan)
    result = investor_returns.project(saved, loan, investment_amount=25000, curve_work_limit=0)
    assert calculate(saved, loan) == original  # The legacy calculation remains unchanged.
    if available:
        assert result["currency"] == "USD" and result["holding"]
        assert result["initial_outlay"] == 25000
    else:
        assert result["status"] == "unavailable" and result["missing_inputs"] == ["currency"]
        assert result["currency"] is result["initial_outlay"] is None
        assert result["offering_investor_capital"] is result["investment_amount"] is None
        assert result["holding"] == result["payoff_curve"] == []
        assert result["selected_exit"] is result["full_term"] is None
        assert result["request"]["investment_amount"] == 25000  # User input, not a priced quote.


@pytest.mark.asyncio
async def test_saved_listing_with_unknown_currency_returns_gaps_not_a_money_quote():
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        snapshot = financials()
        snapshot["proposed_terms"]["currency"] = None
        snapshot["deal_budget"]["recorded"]["currency"] = None
        listing.source_snapshot = {"financials": snapshot}
        response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns?investment_amount=25000")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "unavailable" and result["missing_inputs"] == ["currency"]
        assert result["source"]["kind"] == "listing_saved"
        assert result["offering_investor_capital"] is result["investment_amount"] is result["initial_outlay"] is None
        assert result["holding"] == result["payoff_curve"] == []
        assert result["selected_exit"] is result["full_term"] is None


@pytest.mark.asyncio
async def test_staff_saved_and_working_preview_use_canonical_context_without_saving(monkeypatch):
    from app.routers import application_profiles as profile_routes

    async with ppm_harness() as h:
        h.client._transport.app.include_router(profile_routes.router, prefix="/api/v1")
        h.current = await Scenario(h).admin()
        saved = financials()["deal_budget"]["recorded"]
        loan = terms()
        plan = {"new_loan_amount": 100000, "new_loan_rate_pct": 12, "new_loan_term_months": 12,
                "new_loan_structure": "interest_only", "new_loan_payments_per_year": 12}
        profile = SimpleNamespace(id=uuid4(), deal_budget=deepcopy(saved), financial_plan=deepcopy(plan))
        cockpit = financial_cockpit.build_cockpit([], [], plan=plan, requested_amount=100000)
        cockpit["proposed_terms"] = loan
        monkeypatch.setattr(application_profiles, "load_profile", AsyncMock(return_value=profile))
        monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(return_value=cockpit))
        path = f"/api/v1/application-profiles/{profile.id}/investor-returns"
        before = deepcopy((profile.deal_budget, profile.financial_plan))
        saved_response = await h.client.get(path + "?exit_month=6.5")
        assert saved_response.status_code == 200, saved_response.text
        assert saved_response.json()["source"]["kind"] == "staff_saved"
        assert saved_response.json()["request"]["exit_month"] == 6.5
        working = {**deepcopy(saved), "expected_revision": saved["revision"], "loan": {
            "amount": 200000, "rate_pct": 12, "term_months": 12, "structure": "interest_only", "payments_per_year": 12}}
        for key in ("revision", "updated_at", "source", "recorded_terms_ref"):
            working.pop(key, None)
        response = await h.client.post(path + "/preview?exit_month=8&investment_amount=25000", json=working)
        assert response.status_code == 200, response.text
        assert response.json()["source"]["kind"] == "staff_preview"
        assert response.json()["offering_investor_capital"] == 200000
        assert (profile.deal_budget, profile.financial_plan) == before
        assert not (await h.db.execute(__import__("sqlalchemy").select(CpListingEvent))).scalars().all()


@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.CAPITAL_PARTNER])
@pytest.mark.asyncio
async def test_staff_profile_permission_precedes_source_load_and_pricing(monkeypatch, role):
    from app.routers import application_profiles as profile_routes

    async with ppm_harness() as h:
        h.client._transport.app.include_router(profile_routes.router, prefix="/api/v1")
        h.current = await h.user(f"denied-monthly-{role.value}@example.test", role)
        load = AsyncMock(side_effect=AssertionError("Unauthorized profile lookup"))
        monkeypatch.setattr(application_profiles, "load_profile", load)
        response = await h.client.get(f"/api/v1/application-profiles/{uuid4()}/investor-returns")
        assert response.status_code == 403
        preview = await h.client.post(f"/api/v1/application-profiles/{uuid4()}/investor-returns/preview", json={"expected_revision": 0})
        assert preview.status_code == 403
        load.assert_not_awaited()


@pytest.mark.parametrize("snapshot", [{}, {"financials": []}, {"financials": {"proposed_terms": {"amount": 100000},
    "deal_budget": {"basis": "unknown", "current": {"costs_confirmed": True, "rows": []}}}}])
@pytest.mark.asyncio
async def test_missing_or_ambiguous_saved_basis_never_invents_a_numeric_projection(snapshot):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        listing.source_snapshot = snapshot
        response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "unavailable"
        assert result["holding"] == [] and result["selected_exit"] is result["full_term"] is None
        assert result["missing_inputs"] in (["saved_financial_snapshot"], ["saved_cost_basis"])


@pytest.mark.parametrize("role", [Role.LOAN_EXEC, Role.BROKER, Role.CLIENT])
@pytest.mark.asyncio
async def test_admin_listing_acl_precedes_pricing(monkeypatch, role):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin())
        h.current = await h.user(f"denied-admin-monthly-{role.value}@example.test", role)
        pricing = Mock(side_effect=AssertionError("Admin ACL must run before cached pricing"))
        monkeypatch.setattr(investor_returns, "project", pricing)
        response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns")
        assert response.status_code == 403
        pricing.assert_not_called()


@pytest.mark.asyncio
async def test_lazy_projection_does_not_enter_real_ppm_or_file_economic_fingerprints(real_sources, monkeypatch):
    from app.capital_partners import router_admin
    from app.capital_partners.services import sync

    async with ppm_harness() as h:
        listing, profile, _renderer, _stored = await linked(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        await h.db.commit()
        before = await ppm.saved_context(h.db, listing)
        stored = deepcopy((profile.financial_plan, profile.deal_budget, listing.source_snapshot))
        original = calculate(profile.deal_budget, before["financials"]["proposed_terms"])
        response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/investor-returns?exit_month=8.5")
        assert response.status_code == 200, response.text
        after = await ppm.saved_context(h.db, listing)
        assert before["fingerprint"] == after["fingerprint"]
        assert sync.budget_economics(before["financials"]["deal_budget"]) == sync.budget_economics(after["financials"]["deal_budget"])
        assert calculate(profile.deal_budget, after["financials"]["proposed_terms"]) == original
        assert (profile.financial_plan, profile.deal_budget, listing.source_snapshot) == stored
        assert not h.db.new and not h.db.dirty and not h.db.deleted
