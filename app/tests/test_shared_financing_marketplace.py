from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException

from app.capital_partners import router_admin as routes
from app.capital_partners.models import CpListing
from app.capital_partners.schemas import ListingIn, ListingProductIn
from app.capital_partners.services import listings, sync
from app.services import investor_report, lender_products, production_term_sheets
from app.services.application_terms_pdf import _collateral_html
from app.services.investor_presentation_pdf import (
    investment_metrics,
    render_investor_presentation_pdf,
)
from app.services.production_term_sheet_pdf import render_term_sheet_html
from app.tests.test_investor_report_layout import detailed_investor_financials
from app.tests.test_production_term_sheet_pdf import _sheet


def disclosure(revision=2):
    return {"enabled": True, "revision": revision, "properties": [{"address": {"street": "120 Synthetic Road", "city": "Example City", "state": "FL", "postal_code": "33101"},
        "estimated_value": "500000.00", "existing_liens_total": "200000.00", "available_lien_amount": "100000.00", "proposed_lien_position": 2,
        "valuation_date": "2026-10-01", "currency": "USD", "source_file_ids": []}],
        "assets": [{"asset_type": "equipment", "description": "Synthetic treatment equipment", "estimated_value": "40000.00", "existing_liens_total": "0.00",
            "available_lien_amount": None, "currency": "USD", "notes": "Private operator note", "owner_name": "Private owner", "source_file_ids": ["private-file-id"]}]}


def financing():
    return {"proposed_terms": {"source": "term_sheet", "program_key": "private_loan_bridge", "program_name": "Private Loan / Bridge",
        "amount": 200_000, "rate_pct": 0, "term_months": 24, "payments_per_year": 12, "structure": "amortizing",
        "repayment_config": {"amortization_months": 60, "balloons": [], "after_balloon": "keep_payment"},
        "interest_only": False, "monthly_payment": 3333.33, "coverage_payment_monthly": 10_000, "maturity_balance": 120_000,
        "collateral_revision": 2, "collateral_snapshot": disclosure(), "recorded_terms_ref": {"id": "terms-id", "version": 3}},
        "dscr_after_plan": -0.53}


def test_private_bridge_is_a_business_capability_without_changing_property_loan_types():
    assert lender_products.group_of("private_loan_bridge") == "business_lending"
    assert lender_products.LENDER_PRODUCT_LABELS["private_loan_bridge"] == "Private Loan / Bridge"
    assert "private_loan_bridge" not in lender_products.REAL_ESTATE_PRODUCTS
    assert lender_products.MANUAL_FINANCING_PRODUCTS["private_loan_bridge"]["description"]


def test_marketplace_primary_uses_canonical_schedule_zero_rate_and_signed_dscr_preserving_alternatives():
    existing = {"loan_amount": 70_000, "offered_amount": 70_000, "products": [
        {"program_key": "private_loan_bridge", "label": "Stale", "amount": 70_000, "primary": True},
        {"program_key": "sba", "label": "SBA alternative", "amount": 150_000, "primary": False}]}
    original = deepcopy(existing)
    fields = sync.canonical_financing_fields(financing(), existing)
    assert fields["loan_amount"] == fields["offered_amount"] == 200_000
    assert fields["note_rate_pct"] == 0 and fields["term_months"] == 24
    primary, alternative = fields["products"]
    assert primary["rate_pct"] == 0 and primary["dscr"] == -0.53
    assert primary["repayment_config"]["amortization_months"] == 60 and primary["maturity_balance"] == 120_000
    assert primary["financing_source"] == "canonical_financing"
    assert alternative == existing["products"][1]
    assert existing == original
    existing["offered_amount"] = 25_000
    assert "offered_amount" not in sync.canonical_financing_fields(financing(), existing)


def test_a_different_explicit_primary_is_kept_as_an_alternative():
    existing = {"products": [{"program_key": "sba", "label": "SBA", "primary": True, "amount": 100_000}]}
    products = sync.canonical_financing_fields(financing(), existing)["products"]
    assert [row["program_key"] for row in products] == ["private_loan_bridge", "sba"]
    assert [row["primary"] for row in products] == [True, False]


def test_cleared_working_plan_preserves_issued_terms_without_reintroducing_its_loan():
    data = financing()
    data["proposed_terms"].update(active_proposal=False, source="recorded_reference")
    assert sync.canonical_financing_fields(data, {}) == {}
    assert investment_metrics(data["proposed_terms"])["investment_amount"] is None


def test_cleared_proposal_preview_does_not_present_historic_return_as_current_investment():
    data = detailed_investor_financials()
    data["proposed_terms"].update(active_proposal=False, source="recorded_reference")
    data["investor_report"]["collateral"]["active_proposal"] = False
    document = fitz.open(stream=render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data), filetype="pdf")
    assert "No active financing proposal" in document[0].get_text()
    assert "Terms needed" in document[0].get_text()
    assert "Recorded collateral reference" in document[3].get_text()


def test_canonical_primary_and_eight_explicit_alternatives_round_trip():
    old = {"products": [{"program_key": f"program_{i}", "label": f"Option {i}", "primary": i == 0} for i in range(8)]}
    fields = sync.canonical_financing_fields(financing(), old)
    parsed = ListingIn.model_validate(fields)
    assert len(parsed.products) == 9 and sum(row.primary for row in parsed.products) == 1


@pytest.mark.asyncio
async def test_performance_only_edit_and_mixed_decimal_json_do_not_reprice_committed_product():
    old = {"program_key": "private_loan_bridge", "label": "Private", "primary": True, "amount": "200000.00", "rate_pct": "0.00", "term_months": 24,
        "monthly_payment": "3333.33", "dscr": -.53}
    listing = SimpleNamespace(id=uuid4(), status="open", products=[old])
    payload = ListingIn(products=[ListingProductIn(**{**old, "amount": 200_000, "monthly_payment": 3333.33, "dscr": -.2, "notes": "New evidence reviewed"})])
    with patch.object(routes.L, "committed_amounts", AsyncMock()) as commitments:
        await routes._guard_committed_listing_edit(SimpleNamespace(), listing, payload)
    commitments.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_participation_amount_cannot_be_reduced_below_existing_allocations():
    listing = SimpleNamespace(id=uuid4(), status="open", offered_amount=200_000)
    with patch.object(routes.L, "committed_amounts", AsyncMock(return_value={listing.id: 100_000})):
        with pytest.raises(HTTPException) as error:
            await routes._guard_committed_listing_edit(SimpleNamespace(), listing, ListingIn(offered_amount=50_000))
    assert error.value.status_code == 409 and listing.offered_amount == 200_000


@pytest.mark.asyncio
async def test_production_terms_check_seen_collateral_revision_before_creating_any_version():
    profile = SimpleNamespace(id=uuid4(), investor_collateral=disclosure(3))
    db = SimpleNamespace(add=lambda _: pytest.fail("A stale editor must not create a version"))
    from app.enums import Role
    with patch("app.services.application_terms.lock_profile_terms", AsyncMock()):
        with pytest.raises(HTTPException) as error:
            await production_term_sheets.record_sheet(db, profile=profile, user=SimpleNamespace(role=Role.LOAN_EXEC),
                body={"extra": {"expected_collateral_revision": 2}}, request=None)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_committed_listing_cannot_be_repriced_by_refresh():
    listing = SimpleNamespace(id=uuid4(), status="open", products=[], loan_amount=70_000, offered_amount=70_000,
        note_rate_pct=12, term_months=120, source_snapshot={})
    original = deepcopy(listing.__dict__)
    with patch.object(routes.L, "committed_amounts", AsyncMock(return_value={listing.id: 10_000})):
        with pytest.raises(HTTPException) as error:
            await routes._guard_financing_change(SimpleNamespace(), listing, {"financials": financing()})
    assert error.value.status_code == 409
    assert listing.__dict__ == original


@pytest.mark.asyncio
async def test_publish_checks_fresh_file_even_if_old_cached_snapshot_looks_current():
    listing = SimpleNamespace(profile_id=uuid4(), source_snapshot={"file_fingerprints": {"terms": "same", "collateral": "old"}})
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))
    with patch.object(sync, "current_fingerprints", AsyncMock(return_value=({"terms": "same", "collateral": "new"}, {}, {}))):
        with pytest.raises(HTTPException) as error:
            await routes._require_current_file_snapshot(db, listing)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_explicit_draft_refresh_updates_one_primary_and_preserves_the_partial_offering():
    listing = SimpleNamespace(id=uuid4(), status="draft", profile_id=uuid4(), products=[], loan_amount=70_000, offered_amount=25_000,
        note_rate_pct=12, term_months=120, source_snapshot={"keep": "audit metadata"})
    fresh = {"financials": financing(), "file_fingerprints": {"terms": "new"}}
    with patch.object(routes.P, "readiness_snapshot", AsyncMock(return_value=fresh)), patch.object(routes.L, "committed_amounts", AsyncMock(return_value={})):
        await routes._apply_file_financing(SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace())), listing)
    assert listing.loan_amount == 200_000 and listing.note_rate_pct == 0 and listing.term_months == 24
    assert listing.offered_amount == 25_000 and listing.products[0]["primary"]
    assert listing.source_snapshot["keep"] == "audit metadata"


def test_member_collateral_uses_frozen_terms_and_never_exposes_private_notes_or_sources():
    terms = financing()["proposed_terms"]
    terms.update(current_collateral=disclosure(3), current_collateral_revision=3, collateral_stale=True)
    safe = listings.partner_terms(terms)
    assert safe["collateral"]["basis"] == "recorded_terms"
    assert safe["collateral"]["revision"] == 2 and safe["current_collateral_revision"] == 3 and safe["collateral_stale"]
    assert safe["collateral"]["assets"][0]["existing_liens_total"] == "0.00"
    for private in ("Private operator note", "Private owner", "private-file-id"):
        assert private not in str(safe)
    assert "current_collateral" not in safe


def test_collateral_changes_make_the_marketplace_stamp_stale_and_changed_terms_block_publish():
    listing = CpListing(name="Synthetic LLC", source_snapshot={}, status="draft", products=[], loan_amount=200_000, offered_amount=200_000,
        minimum_investment=10_000, note_rate_pct=0, term_months=24)
    # Check collateral independently of the full cockpit fixture.
    before = sync.fingerprints({"collateral": disclosure()})
    sync.mark_published(listing, before, by=None)
    changed = sync.sync_state(listing, sync.fingerprints({"collateral": disclosure(3)}))
    assert changed["changed"] == [{"key": "collateral", "label": "Collateral disclosure"}]
    financials = financing()
    financials["proposed_terms"].update(collateral_stale=True)
    listing.source_snapshot = {"financials": financials}
    listing.documents = []
    assert any("updated terms version" in item for item in listings.publish_blockers(listing))


def test_terms_pdf_discloses_only_frozen_security_and_old_terms_stay_unrecorded():
    row = SimpleNamespace(collateral_snapshot=disclosure(), collateral_revision=2, current_collateral=disclosure(3))
    html = _collateral_html(row)
    for expected in ("120 Synthetic Road", "Synthetic treatment equipment", "0.00 USD", "recorded revision 2", "unverified", "supporting file"):
        assert expected in html
    assert "Private operator note" not in html
    assert "No collateral schedule was recorded" in _collateral_html(SimpleNamespace(collateral_snapshot={}, collateral_revision=None))
    production = render_term_sheet_html(_sheet(extra={"collateral_snapshot": disclosure(), "collateral_revision": 2}), business_name="Synthetic LLC")
    assert "Synthetic treatment equipment" in production and "CONFIDENTIAL" in production


def test_investor_report_uses_frozen_collateral_and_keeps_new_proposal_as_unrecorded_context():
    current, terms = disclosure(3), financing()["proposed_terms"]
    current["assets"][0]["estimated_value"] = "99000.00"
    result = investor_report.collateral_report(current, terms, investor_report.Register([], {}))
    assert result["basis"] == "recorded_terms" and result["terms_stale"]
    assert result["assets"][0]["value"] == 40_000 and result["current_proposal"]["assets"][0]["estimated_value"] == "99000.00"
    assert result["assets"][0]["available_lien_amount"] is None
    legacy = investor_report.collateral_report(current, {"source": "term_sheet"}, investor_report.Register([], {}))
    assert legacy["basis"] == "unrecorded_terms" and legacy["assets"] == []
    proposal = investor_report.collateral_report(current, {"source": "plan", "collateral_revision": 3}, investor_report.Register([], {}))
    assert proposal["basis"] == "staff_proposal" and proposal["recorded_revision"] is None


def test_other_asset_collateral_fits_five_pages_and_sources_remain_accessible():
    data = detailed_investor_financials()
    data["investor_report"]["collateral"] = investor_report.collateral_report(disclosure(3), financing()["proposed_terms"], investor_report.Register([], {}))
    data["investor_report"]["collateral"]["assets"] *= 40
    document = fitz.open(stream=render_investor_presentation_pdf(intake=SimpleNamespace(), financials=data), filetype="pdf")
    text = " ".join(page.get_text() for page in document)
    assert len(document) == 5
    assert "Synthetic treatment equipment" in text and "newer proposal requires updated terms" in text
    assert "1 of 1 properties and 1 of 40 assets shown" in text
    assert "120 Synthetic Road" in text
    assert "Private operator note" not in text and "private-file-id" not in text
