from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.capital_partners import router_admin as routes
from app.capital_partners.models import CpListing
from app.capital_partners.schemas import ListingAIDraftIn, ListingIn
from app.capital_partners.services import listing_draft, listings, sync
from app.services import deal_budget
from app.services.financing_budget_disclosure import (
    cost_formula,
    frozen_budget,
    public_budget,
    render_budget_html,
)
from app.services.investor_presentation_pdf import render_investor_presentation_pdf
from app.tests.test_investor_report_layout import (
    detailed_investor_financials,
    stressed_investor_financials,
)


def cost_row(label, category, value, **updates):
    return {"id": str(uuid4()), "label": label, "category": category, "payer": "borrower", "method": "fixed", "value": value,
        "basis": "loan_amount", "timing": "closing", "settlement": "withheld", "enabled": True,
        "recipient": "PRIVATE RECIPIENT", "notes": "PRIVATE COMMISSION NOTE", "source_ref": "PRIVATE SOURCE ID",
        "reserve_release": "unknown", "recipient_role": "third_party", **updates}


def budget_fixture(**updates):
    return {"revision": 3, "currency": "USD", "costs_confirmed": True, "funding_mode": "multiple_investors",
        "investor_funding_amount": 150_000, "minimum_investor_contribution": 25_000,
        "rows": [cost_row("Origination points", "origination", 2, method="percent", recipient_role="platform"),
            cost_row("Legal closing", "closing_cost", 800, settlement="separate"),
            cost_row("Tax escrow", "escrow_reserve", 3_000, recipient_role="escrow"),
            cost_row("Referral fee", "referral", 500, settlement="separate"),
            cost_row("Investor servicing", "servicing", 1, payer="investor", method="percent", basis="outstanding_principal", timing="annual", recipient_role="platform"),
            cost_row("Investor management", "management", 25, payer="investor", timing="monthly", recipient_role="platform"),
            cost_row("Borrower servicing", "servicing", 30, timing="monthly", recipient_role="platform"),
            cost_row("Insurance escrow", "escrow_reserve", 500, recipient_role="escrow")], **updates}


def offering_fixture(*, stress=False, unknown=False):
    data = stressed_investor_financials() if stress else detailed_investor_financials()
    terms = data["proposed_terms"]
    terms.update(source="term_sheet", active_proposal=True, recorded_terms_ref={"id": "synthetic-terms", "version": 3},
        program_key="private_loan_bridge", program_name="Private Loan / Bridge",
        coverage_context={"income_annual": 120_000, "debt_service_annual": 100_000, "base_dscr": 1.2, "basis": "Synthetic source"})
    budget = budget_fixture(costs_confirmed=not unknown)
    if stress:
        budget["rows"] = [dict(row, id=str(uuid4())) for row in budget["rows"] * 20]
    use_rows = [{"category": category, "amount": amount} for category, amount in data["use_of_funds"]["category_totals"].items()]
    calc = deal_budget.calculate(budget, terms, use_rows)
    terms.update(deal_budget_snapshot=deepcopy(budget), deal_budget_revision=3, deal_budget_calculation=deepcopy(calc))
    report = {"current": deepcopy(budget), "recorded": deepcopy(budget), "basis": "recorded_terms", "stale": False, "calculation": calc}
    data["deal_budget"] = report
    data["investor_report"]["deal_budget"] = public_budget(report)
    data["investor_report"]["references"].append({"id": "S19", "label": "Recorded financing cost budget", "kind": "staff_deal_budget", "version": 3, "used_for": ["deal_budget"]})
    data["investor_report"]["section_reference_ids"]["deal_budget"] = ["S19"]
    return data


def draft_fixture():
    profile = SimpleNamespace(id=uuid4(), industry="Wellness services", naics_label=None)
    data = offering_fixture()
    draft = {"profile_id": profile.id, "loan_id": None, "program_label": "Private Loan / Bridge", "city": "Miami", "state": "FL",
        "name": "PRIVATE BORROWER LLC", "loan_amount": 200_000, "offered_amount": 150_000, "minimum_investment": 25_000,
        "source_snapshot": {"financials": {"proposed_terms": data["proposed_terms"], "dscr": .8, "dscr_after_plan": 1.2,
            "deal_budget": data["deal_budget"], "private_credit_name": "PRIVATE OWNER"},
            "file_fingerprints": {"terms": "terms-v3", "deal_budget": "budget-v3"}}}
    return profile, draft


def test_public_budget_keeps_labels_formulas_and_real_charges_but_excludes_private_fields():
    data = offering_fixture()
    public = public_budget(data["deal_budget"])
    assert public["rows"][0]["label"] == "Origination points"
    assert cost_formula(public["rows"][0]) == "2% of gross loan - at closing"
    assert public["rows"][2]["label"] == "Tax escrow" and public["rows"][7]["label"] == "Insurance escrow"
    assert public["calculation"]["net_client_cash"] == 192_500
    assert public["calculation"]["investor_fees"] > 0
    assert public["calculation"]["escrow_reserves"] == 3_500
    assert public["calculation"]["platform_fee_revenue"] < sum(row["total_amount"] for row in public["rows"])
    for private in ("PRIVATE RECIPIENT", "PRIVATE COMMISSION NOTE", "PRIVATE SOURCE ID"):
        assert private not in str(public)
    row = cost_row("Fee to PRIVATE RECIPIENT", "referral", 100)
    projected = public_budget({"current": budget_fixture(rows=[row]), "calculation": {"rows": []}})
    assert projected["rows"][0]["label"] == "Referral fee"


def test_unrecorded_historical_budget_does_not_inherit_current_rows():
    public = public_budget({"current": budget_fixture(), "recorded": {}, "basis": "unrecorded_terms", "calculation": {}})
    assert public["rows"] == [] and not public["costs_confirmed"]
    assert "No cost budget was recorded" in render_budget_html(None)


def test_frozen_cost_html_uses_stored_math_and_preserves_unknowns():
    data = offering_fixture(unknown=True)
    budget, terms = data["deal_budget"]["recorded"], data["proposed_terms"]
    with patch.object(deal_budget, "calculate", side_effect=AssertionError("Historical math must remain frozen")):
        disclosure = frozen_budget(budget, 3, terms, data["deal_budget"]["calculation"])
    html = render_budget_html(disclosure)
    for text in ("Tax escrow", "Insurance escrow", "2% of gross loan", "per month", "Reserve, not fee income", "Unknown - complete the cost review"):
        assert text in html
    assert "PRIVATE RECIPIENT" not in html and "PRIVATE COMMISSION NOTE" not in html


@pytest.mark.parametrize("stress,unknown", [(False, False), (True, True)])
def test_investor_cost_report_stays_five_pages_with_net_cash_fees_and_separate_coverage(stress, unknown):
    data = offering_fixture(stress=stress, unknown=unknown)
    original = deepcopy(data)
    document = fitz.open(stream=render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data), filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert len(document) == 5 and all("CONFIDENTIAL" in page.get_text() for page in document)
    for expected in ("Investment capital to raise", "Net loan proceeds at funding", "Tax escrow", "Insurance escrow", "2% of gross loan", "Financing-fee scenario", "Full-loan contractual receipts"):
        assert expected.casefold() in text.casefold()
    if unknown:
        assert "Net return unknown" in text and "8 of 160 rows" in text
    else:
        assert "$192,500" in text and "$150,000" in text and "$25,000" in text
        assert "Use-of-funds allocations exceed net loan proceeds" in text and "$7,500" in text
    for private in ("PRIVATE RECIPIENT", "PRIVATE COMMISSION NOTE", "PRIVATE SOURCE ID"):
        assert private not in text
    assert data == original


@pytest.mark.asyncio
async def test_ai_draft_is_reviewable_idempotent_and_preserves_staff_copy():
    profile, draft = draft_fixture()
    db = SimpleNamespace(get=AsyncMock(return_value=profile), add=lambda _: pytest.fail("Drafting must not create a listing"))
    payload = ListingAIDraftIn(profile_id=profile.id, current_title="Reviewed private bridge", current_summary="Staff reviewed summary")
    with patch.object(listing_draft.prefill, "prefill", AsyncMock(return_value=draft)), patch.object(listing_draft.prefill, "existing_listing", AsyncMock(return_value=None)), \
         patch.object(listing_draft, "get_settings", return_value=SimpleNamespace(bedrock_enabled=False)):
        first = await listing_draft.prepare(db, SimpleNamespace(id=uuid4()), payload)
        second = await listing_draft.prepare(db, SimpleNamespace(id=uuid4()), payload)
    assert first["draft_id"] == second["draft_id"]
    assert first["draft"]["name"] == "Reviewed private bridge" and first["draft"]["summary"] == "Staff reviewed summary"
    assert first["draft"]["offered_amount"] == 150_000 and first["draft"]["minimum_investment"] == 25_000
    assert first["preserved_fields"] == ["name", "summary"] and first["status"] == "file_prefill"


@pytest.mark.asyncio
async def test_ai_provider_receives_only_safe_fact_choices_and_never_sets_money():
    profile, draft = draft_fixture()
    captured = {}
    async def select(_db, *, title_parts, facts, actor_id):
        captured.update(title_parts=title_parts, facts=facts)
        return listing_draft.CopySelection(title_order=["industry", "location", "program"], summary_fact_keys=["coverage"])
    with patch.object(listing_draft.prefill, "prefill", AsyncMock(return_value=draft)), patch.object(listing_draft.prefill, "existing_listing", AsyncMock(return_value=None)), \
         patch.object(listing_draft, "get_settings", return_value=SimpleNamespace(bedrock_enabled=True)), patch.object(listing_draft, "_select_copy", select):
        result = await listing_draft.prepare(SimpleNamespace(get=AsyncMock(return_value=profile)), SimpleNamespace(id=uuid4()), ListingAIDraftIn(profile_id=profile.id))
    assert result["status"] == "ai_draft"
    assert result["draft"]["name"] == "Wellness services | Miami, FL | Private Loan / Bridge"
    assert "recorded-quote coverage" in result["draft"]["summary"] and "borrower recurring charges" in result["draft"]["summary"]
    assert result["draft"]["offered_amount"] == 150_000 and result["draft"]["minimum_investment"] == 25_000
    assert "PRIVATE" not in str(captured)


@pytest.mark.asyncio
async def test_provider_failure_falls_back_with_unknowns_and_no_invented_minimum():
    profile, draft = draft_fixture()
    draft["minimum_investment"] = None
    draft["source_snapshot"]["financials"]["deal_budget"]["calculation"]["minimum_investor_contribution"] = None
    with patch.object(listing_draft.prefill, "prefill", AsyncMock(return_value=draft)), patch.object(listing_draft.prefill, "existing_listing", AsyncMock(return_value=None)), \
         patch.object(listing_draft, "get_settings", return_value=SimpleNamespace(bedrock_enabled=True)), patch.object(listing_draft, "_select_copy", AsyncMock(side_effect=RuntimeError("Unavailable"))):
        result = await listing_draft.prepare(SimpleNamespace(get=AsyncMock(return_value=profile)), SimpleNamespace(id=uuid4()), ListingAIDraftIn(profile_id=profile.id))
    assert result["status"] == "file_prefill" and result["warnings"]
    assert result["draft"]["minimum_investment"] is None
    assert any("minimum" in item for item in result["missing"])
    assert "PRIVATE BORROWER" not in result["draft"]["name"]


@pytest.mark.asyncio
async def test_stale_ai_apply_is_rejected_before_canonical_values_change():
    profile, draft = draft_fixture()
    listing = SimpleNamespace(id=uuid4(), profile_id=profile.id, status="draft", products=[], loan_amount=200_000,
        offered_amount=150_000, minimum_investment=25_000, note_rate_pct=12, term_months=24, source_snapshot={})
    fresh = draft["source_snapshot"]
    original = deepcopy(listing.__dict__)
    db = SimpleNamespace(get=AsyncMock(return_value=profile))
    with patch.object(routes.P, "readiness_snapshot", AsyncMock(return_value=fresh)):
        with pytest.raises(HTTPException) as error:
            await routes._apply_file_financing(db, listing, payload=ListingIn(expected_deal_budget_revision=2))
    assert error.value.status_code == 409 and listing.__dict__ == original


def test_legacy_participation_and_minimum_survive_unreviewed_default_budget():
    _, draft = draft_fixture()
    financials = deepcopy(draft["source_snapshot"]["financials"])
    financials["deal_budget"] = {"current": {"revision": 0, "costs_confirmed": False}, "recorded": {}, "calculation": {
        "investor_funding_amount": 200_000, "minimum_investor_contribution": 200_000}}
    existing = {"loan_amount": 200_000, "offered_amount": 100_000, "minimum_investment": 10_000, "products": []}
    updates = sync.canonical_financing_fields(financials, existing)
    assert updates["offered_amount"] == 100_000 and updates["minimum_investment"] == 10_000
    assert sync.budget_economics(financials["deal_budget"]) == {}
    new = sync.canonical_financing_fields(financials, {}, is_new=True)
    assert new["offered_amount"] == new["minimum_investment"] == 200_000


def test_member_terms_disclose_real_costs_while_private_internal_economics_stay_out():
    data = offering_fixture()
    safe = listings.partner_terms(data["proposed_terms"])
    assert safe["deal_budget"]["calculation"]["investor_fees"] > 0
    assert safe["deal_budget"]["rows"][2]["label"] == "Tax escrow"
    assert "PRIVATE" not in str(safe)


def test_budget_charge_changes_are_economic_but_performance_dscr_refresh_is_not():
    data = offering_fixture()
    before = sync.budget_economics(data["deal_budget"])
    after = deepcopy(data["deal_budget"])
    after["calculation"]["financing_fee_coverage"]["base_dscr"] = .9
    after["calculation"]["financing_fee_coverage"]["with_financing_fees_dscr"] = .8
    assert before == sync.budget_economics(after)
    after["calculation"]["investor_fees"] += 100
    assert before != sync.budget_economics(after)


def test_fresh_summary_uses_live_coverage_and_keeps_recorded_return_math():
    profile, draft = draft_fixture()
    financials = draft["source_snapshot"]["financials"]
    frozen_roi = financials["deal_budget"]["calculation"]["investor_net_roi_pct"]
    financials["dscr_after_plan"] = 2.0
    financials["deal_budget"]["current_financing_fee_coverage"] = {
        "base_dscr": 2.0, "with_financing_fees_dscr": 1.98, "annual_borrower_financing_fees": 360, "basis": "Current reviewed earnings"}
    _, facts, _ = listing_draft.facts_for_draft(profile, draft)
    assert "base refinance coverage 2.00x" in facts["coverage"]
    assert "current-earnings coverage with borrower recurring charges 1.9800x" in facts["coverage"]
    assert "recorded-quote coverage" not in facts["coverage"]
    assert financials["deal_budget"]["calculation"]["investor_net_roi_pct"] == frozen_roi


def test_ai_apply_requires_both_snapshot_guards_but_manual_edits_remain_supported():
    with pytest.raises(ValidationError):
        ListingIn(ai_draft_id="draft")
    with pytest.raises(ValidationError):
        ListingIn(ai_draft_id="draft", expected_deal_budget_revision=3)
    value = ListingIn(ai_draft_id="draft", expected_deal_budget_revision=3, expected_source_fingerprint="a" * 64)
    assert value.expected_deal_budget_revision == 3
    assert ListingIn(summary="Staff edit").summary == "Staff edit"


def test_current_allocation_gap_blocks_publish_even_when_frozen_cost_math_is_ready():
    data = offering_fixture()
    data["deal_budget"]["calculation"]["status"] = "ready"
    data["deal_budget"]["current_allocation_reconciliation"] = {"status": "overallocated", "variance": -7_500}
    listing = CpListing(id=uuid4(), name="Synthetic offering", status="draft", products=[], documents=[], loan_amount=200_000,
        offered_amount=150_000, minimum_investment=25_000, note_rate_pct=12, term_months=24, source_snapshot={"financials": {"deal_budget": data["deal_budget"]}})
    assert any("Reconcile the current use-of-funds" in value for value in listings.publish_blockers(listing))


def test_different_proposal_scope_labels_requested_uses_as_reference_without_false_hard_gap():
    data = offering_fixture()
    report = data["investor_report"]["deal_budget"]
    report["current_allocation_reconciliation"] = {"scope": "requested_reference", "status": "different_scope", "variance": None}
    document = fitz.open(stream=render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data), filetype="pdf")
    text = " ".join(" ".join(page.get_text().split()) for page in document)
    assert "Requested-budget reference" in text
    assert "proposed allocation remains unconfirmed" in text
    assert "Use-of-funds allocations exceed net loan proceeds" not in text


@pytest.mark.asyncio
async def test_regenerated_legacy_summary_matches_preserved_participation_and_minimum():
    profile, draft = draft_fixture()
    draft["source_snapshot"]["financials"]["deal_budget"]["current"]["revision"] = 0
    listing = SimpleNamespace(id=uuid4(), profile_id=profile.id, name="Legacy offering", summary="", offered_amount=50_000, minimum_investment=10_000)
    async def get(model, key):
        return listing if model is CpListing else profile
    with patch.object(listing_draft.prefill, "prefill", AsyncMock(return_value=draft)), patch.object(listing_draft.prefill, "existing_listing", AsyncMock(return_value={"id": str(listing.id)})), \
         patch.object(listing_draft, "get_settings", return_value=SimpleNamespace(bedrock_enabled=False)):
        result = await listing_draft.prepare(SimpleNamespace(get=get), SimpleNamespace(id=uuid4()),
            ListingAIDraftIn(profile_id=profile.id, listing_id=listing.id, preserve_summary=False))
    assert result["draft"]["offered_amount"] == 50_000 and result["draft"]["minimum_investment"] == 10_000
    assert "Capital to raise: $50,000" in result["draft"]["summary"]
    assert "Minimum contribution: $10,000" in result["draft"]["summary"]
    assert "Capital to raise: $150,000" not in result["draft"]["summary"]


@pytest.mark.asyncio
async def test_committed_summary_only_patch_retains_reviewed_minimum_and_prices():
    profile, draft = draft_fixture()
    financials, fresh = draft["source_snapshot"]["financials"], draft["source_snapshot"]
    fields = sync.canonical_financing_fields(financials, {}, is_new=True)
    listing = CpListing(id=uuid4(), profile_id=profile.id, status="open", vertical="business", name="Reviewed offering", source_snapshot=deepcopy(fresh), **fields)
    db = SimpleNamespace(get=AsyncMock(return_value=profile), flush=AsyncMock())
    with patch.object(routes, "_load_admin_listing", AsyncMock(return_value=listing)), \
         patch.object(routes.P, "readiness_snapshot", AsyncMock(return_value=fresh)), \
         patch.object(routes.L, "committed_amounts", AsyncMock(return_value={listing.id: 100_000})) as commitments, \
         patch.object(routes.L, "log_event", AsyncMock()), patch.object(routes, "_listing_admin_out", AsyncMock(return_value={"ok": True})):
        result = await routes.patch_listing(listing.id, ListingIn(summary="Updated safe public summary"), SimpleNamespace(id=uuid4()), db)
    assert result == {"ok": True} and listing.minimum_investment == 25_000
    assert listing.summary == "Updated safe public summary"
    commitments.assert_not_awaited()
