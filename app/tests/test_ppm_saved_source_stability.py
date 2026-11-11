"""Real saved-financing projection across ORM refreshes, without AI/storage."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.capital_partners import router_admin
from app.capital_partners.services import ppm, prefill, sync
from app.models.application_profile import ApplicationProfile
from app.models.production_package import ProductionPackage
from app.services import application_programs, deal_budget, financial_cockpit, use_of_funds
from app.tests import test_capital_partners as capital_tests
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import configure, ppm_harness


def sources():
    return [{"file_id": "10000000-0000-4000-8000-000000000001", "file_name": "Synthetic business tax.pdf",
        "classification": "tax_return", "classifications": ["tax_return"], "accepted": True,
        "key_facts": {"entity_name": "Synthetic Services LLC", "tax_year": 2025, "currency": "USD", "basis": "cash",
            "gross_receipts": 900000, "ordinary_business_income": -120000, "interest_expense": 40000, "depreciation": 15000}}]


def budget():
    return {"schema_version": 1, "revision": 5, "currency": "USD", "funding_mode": "single_investor", "costs_confirmed": True,
        "loan_amount_mode": "gross_loan", "investor_funding_amount": None, "minimum_investor_contribution": None,
        "rows": [{"id": "20000000-0000-4000-8000-000000000002", "label": "Synthetic origination", "enabled": True,
            "category": "origination", "payer": "borrower", "method": "percent", "value": 3, "basis": "loan_amount",
            "timing": "closing", "settlement": "withheld", "recipient_role": "platform"}]}


@pytest.fixture
def real_sources(monkeypatch):
    monkeypatch.setattr(capital_tests, "MODELS", [*capital_tests.MODELS, ApplicationProfile, ProductionPackage])
    monkeypatch.setattr(application_programs, "active_selections", AsyncMock(return_value=[]))
    monkeypatch.setattr(application_programs, "published_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal("250000.00"), "synthetic_request", None, False)))
    evidence = sources()

    async def load(_db, profile):
        cockpit = financial_cockpit.build_cockpit(deepcopy(evidence), [], requested_amount=250000,
            plan=deepcopy(profile.financial_plan), financing_budget=deal_budget.snapshot(profile),
            business_entity="Synthetic Services LLC", profile_id=str(profile.id))
        cockpit["collateral"] = {"enabled": False, "revision": 0, "properties": [], "assets": []}
        terms = sync.proposed_terms(cockpit)
        cockpit["deal_budget"] = {**deal_budget.snapshot(profile), "calculation": deal_budget.calculate(deal_budget.snapshot(profile), terms)}
        return cockpit

    monkeypatch.setattr(financial_cockpit, "load_cockpit", load)
    return evidence


async def linked(h, monkeypatch):
    scenario = Scenario(h)
    listing = await scenario.listing(await scenario.admin(), publish=False)
    profile = ApplicationProfile(vertical="main_street", funding_category="general", industry="Synthetic services",
        financial_plan={"new_loan_amount": 143210.56, "new_loan_rate_pct": 21.125, "new_loan_term_months": 24,
            "new_loan_structure": "amortizing", "new_payment_source": "manual_terms"}, deal_budget=budget())
    h.db.add(profile)
    await h.db.flush()
    listing.profile_id = profile.id
    await h.db.commit()
    await h.db.refresh(listing, attribute_names=["documents"])
    original = ppm.saved_context
    _context, renderer, stored = configure(monkeypatch, listing)
    monkeypatch.setattr(ppm, "saved_context", original)
    return listing, profile, renderer, stored


@pytest.mark.asyncio
async def test_real_readiness_and_canonical_financing_have_one_source_across_orm_numeric_reload(real_sources, monkeypatch):
    async with ppm_harness() as h:
        listing, profile, _, _ = await linked(h, monkeypatch)
        first = await prefill.readiness_snapshot(h.db, profile)
        again = await prefill.readiness_snapshot(h.db, profile)
        assert first["file_fingerprints"] == again["file_fingerprints"]
        await router_admin._apply_file_financing(h.db, listing)
        assert isinstance(listing.loan_amount, float)
        before = await ppm.saved_context(h.db, listing)
        await h.db.flush()
        await h.db.refresh(listing)
        await h.db.refresh(listing, attribute_names=["documents"])
        assert isinstance(listing.loan_amount, Decimal)
        after = await ppm.saved_context(h.db, listing)
        assert before["fingerprint"] == after["fingerprint"]
        assert not any(row["key"] == "listing_snapshot" for row in after["missing"])
        listing.loan_amount = float(listing.loan_amount) + .00000001
        listing.note_rate_pct = float(listing.note_rate_pct) + .0000000001
        rounded = await ppm.saved_context(h.db, listing)
        assert rounded["fingerprint"] == after["fingerprint"]
        assert not any(row["key"] in {"listing_snapshot", "financing_outdated"} for row in rounded["missing"])


@pytest.mark.asyncio
async def test_render_guard_accepts_same_saved_numeric_values_but_keeps_actual_source_changes(real_sources, monkeypatch):
    async with ppm_harness() as h:
        listing, profile, renderer, _ = await linked(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        context = await ppm.saved_context(h.db, listing)
        await h.db.flush()
        await h.db.refresh(listing)
        await h.db.refresh(listing, attribute_names=["documents"])
        legal, selected = ppm.ppm_presets.defaults(context, ppm.LEGAL_FIELDS)
        doc = await ppm._version(h.db, listing, context, legal, {"overview": "Preserved staff wording"}, h.current.id,
            generation_status="source_based", legal_presets=selected)
        assert doc.version >= 1 and renderer.call_count == 1
        unchanged = await ppm.saved_context(h.db, listing)
        assert unchanged["fingerprint"] == context["fingerprint"]
        original = deepcopy(profile.financial_plan)
        profile.financial_plan = {**original, "new_loan_amount": original["new_loan_amount"] + .01}
        await h.db.flush()
        assert (await ppm.saved_context(h.db, listing))["fingerprint"] != context["fingerprint"]
        count = len(listing.documents)
        with pytest.raises(HTTPException) as error:
            await ppm._version(h.db, listing, context, legal, {}, h.current.id,
                generation_status="source_based", legal_presets=selected)
        assert error.value.status_code == 409 and error.value.detail["code"] == "ppm_source_changed"
        assert len(listing.documents) == count


def test_canonical_saved_source_numbers_are_lossless_and_do_not_reinterpret_identifiers():
    hash_source = sync.saved_source_fingerprint
    assert hash_source({"amount": Decimal("143210.5600"), "term": 24}) == hash_source({"amount": 143210.56, "term": 24.0})
    assert hash_source({"amount": Decimal("1000000000000.01")}) != hash_source({"amount": Decimal("1000000000000.02")})
    assert hash_source({"rate": Decimal("21.125")}) != hash_source({"rate": Decimal("21.126")})
    assert hash_source({"amount": Decimal("143210.56")}) != hash_source({"amount": "143210.56"})
    assert hash_source({"flag": True}) != hash_source({"flag": 1})
    assert hash_source({"flag": False}) != hash_source({"flag": 0})
    assert hash_source({"id": "001"}) != hash_source({"id": "1"})
    assert hash_source({"amount": -0.0}) == hash_source({"amount": Decimal("0.00")})
    assert sync._fingerprint({"amount": Decimal("143210.56")}) != sync._fingerprint({"amount": 143210.56})


@pytest.mark.parametrize("changed", ["fee", "rate", "noi", "identity", "period"])
@pytest.mark.asyncio
async def test_real_financial_or_source_changes_still_invalidate_saved_authority(real_sources, monkeypatch, changed):
    async with ppm_harness() as h:
        listing, profile, _, _ = await linked(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        await h.db.commit()
        before = await ppm.saved_context(h.db, listing)
        if changed == "fee":
            current = deepcopy(profile.deal_budget)
            current["rows"][0]["value"] += .01
            current["revision"] += 1
            profile.deal_budget = current
            await h.db.flush()
        elif changed == "rate":
            profile.financial_plan = {**profile.financial_plan, "new_loan_rate_pct": profile.financial_plan["new_loan_rate_pct"] + .001}
            await h.db.flush()
        else:
            facts = real_sources[0]["key_facts"]
            if changed == "noi":
                facts["ordinary_business_income"] += 1
            elif changed == "identity":
                facts["entity_name"] = "Another synthetic company LLC"
            else:
                facts["tax_year"] = 2024
        after = await ppm.saved_context(h.db, listing)
        assert after["fingerprint"] != before["fingerprint"]
