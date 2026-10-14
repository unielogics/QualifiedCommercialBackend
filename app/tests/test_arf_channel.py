from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.arf import ChannelPatch, ReferralCreate
from app.services import arf_channel as arf


def general_facts(**updates):
    return {"industry_source_label": "Restaurant", "industry_mapping_approved": True,
            "equifax_score": 600, "equifax_verified": True,
            "current_ownership_started": "2026-01-01", "monthly_sales": 20000,
            "annual_revenue": 240000, "requested_amount": 100000, **updates}


def result(product="arf_bankroll", **facts):
    return arf.evaluate_arf_facts(product, general_facts(**facts), as_of=date(2026, 10, 14))


def test_four_products_are_one_channel_and_no_rewards_or_fifth_product():
    assert len(arf.PRODUCTS) == 4
    assert {p["funding_channel"] for p in arf.PRODUCTS} == {"arf_external"}
    assert all(p["qc_role"] == "referral_partner" for p in arf.PRODUCTS)
    assert all(p["maximum_term_months"] == 24 for p in arf.PRODUCTS)


def test_missing_facts_remain_unknown_and_no_approval_probability():
    response = arf.evaluate_arf_facts("arf_bankroll", {})
    assert response["status"] == "missing_information"
    assert response["credit_decision"] == "not_submitted"
    assert "approval_probability" not in response
    assert not response["failed_rules"]


def test_general_pass_is_only_potential_match():
    response = result()
    assert response["status"] == "potentially_eligible"
    assert response["credit_decision"] == "not_submitted"
    assert response["approved"] is False


def test_revenue_disagreement_is_conflict_not_invented_and_or_rule():
    response = result(monthly_sales=17000, annual_revenue=199999)
    assert response["status"] == "lender_clarification_required"
    assert not response["failed_rules"]
    assert any(row["key"] == "revenue_combination" for row in response["conflicts"])


def test_missing_annual_history_does_not_fail_single_month_business():
    response = result(monthly_sales=16000, annual_revenue=None)
    assert response["status"] == "missing_information"
    assert not response["failed_rules"]


def test_ownership_date_uses_calendar_month_boundary():
    assert result(current_ownership_started="2026-09-14")["status"] == "potentially_eligible"
    assert result(current_ownership_started="2026-09-15")["status"] == "outside_published_criteria"


def test_flex_pay_requires_verified_beacon_and_homeownership():
    response = result("arf_io_flex_pay", equifax_score=750)
    assert {"beacon_score", "beacon_verified", "homeownership_verified"} <= set(response["missing_items"])
    assert result("arf_io_flex_pay", beacon_score=651, beacon_verified=True, homeownership_verified=True)["status"] == "potentially_eligible"
    assert result("arf_io_flex_pay", beacon_score=650, beacon_verified=True, homeownership_verified=True)["status"] == "outside_published_criteria"


def test_io_initial_draw_uses_approved_limit_not_requested_draw():
    response = result("arf_io_bankroll", approved_line_limit=250000, requested_initial_draw=62500)
    draw = next(row for row in response["rule_results"] if row["key"] == "initial_draw")
    assert draw["required"] == 62500
    assert draw["state"] == "pass"
    bad = result("arf_io_bankroll", approved_line_limit=250000, requested_initial_draw=62000)
    assert any(row["key"] == "initial_draw" for row in bad["failed_rules"])


def test_io_initial_draw_small_limit_floor_and_no_extended_term():
    response = result("arf_io_bankroll", approved_line_limit=10000, requested_initial_draw=10000)
    assert next(row for row in response["rule_results"] if row["key"] == "initial_draw")["required"] == 10000
    assert arf.PRODUCT_BY_KEY["arf_io_bankroll"]["maximum_term_months"] == 24
    assert response["status"] == "lender_clarification_required"


def trade_facts(**updates):
    return {"industry_key": "plumbing", "industry_source_label": "Plumbing Companies",
            "industry_mapping_approved": True, "beacon_score": 651, "beacon_verified": True,
            "business_started": "2023-10-14", "annual_revenue": 300000,
            "registered_entity": True, "ein_verified": True,
            "requested_amount": 250000, "w2_employees": 0, "home_based": True, **updates}


def test_trade_overlay_no_invented_homeowner_or_employee_requirements():
    response = arf.evaluate_arf_facts("arf_bankroll", trade_facts(), as_of=date(2026, 10, 14))
    assert response["status"] == "lender_clarification_required"
    assert not response["failed_rules"]
    assert "homeownership_verified" not in response["missing_items"]
    assert response["applied_overlays"] == ["arf_skilled_trades"]


def test_tier_one_remains_manual_and_known_failure_coexists_with_conflict():
    response = arf.evaluate_arf_facts("arf_bankroll", trade_facts(requested_amount=600000, beacon_score=650), as_of=date(2026, 10, 14))
    assert response["status"] == "outside_published_criteria"
    assert response["conflicts"] and response["failed_rules"]
    assert response["manual_review_required"]


def test_trade_tax_return_and_workers_comp_preserve_source_wording():
    definitions = arf.requirement_definitions()
    docs = {item["canonical_key"]: item for item in definitions if item["requirement_kind"] == "document"}
    assert set(docs) == set(arf.TRADE_OVERLAY["documents"])
    assert "return type requires confirmation" in docs["most_recent_federal_tax_return"]["label"]
    assert docs["workers_compensation_insurance"]["required_level"] == "required"
    assert all(item["applies_when"]["op"] == "in" for item in docs.values())


def test_extra_outstanding_debts_do_not_fail_only_selected_payoffs_count():
    assert result(outstanding_debt_count=3, selected_competitor_payoff_count=2)["status"] == "potentially_eligible"
    assert result(selected_competitor_payoff_count=3)["status"] == "outside_published_criteria"


def test_self_reported_financial_fact_cannot_pass_verified_floor():
    response = result(fact_sources={"annual_revenue": {"verification_status": "self_reported", "source_refs": []}})
    assert "annual_revenue" in response["missing_items"]


def test_elevate_unknown_history_not_automatically_new():
    assert "merchant_guarantor_history" in result("arf_elevate_bankroll")["missing_items"]
    assert result("arf_elevate_bankroll", days_out_of_arf_program=119)["failed_rules"]
    assert not result("arf_elevate_bankroll", days_out_of_arf_program=120)["failed_rules"]


def test_external_conflict_cannot_silently_disappear():
    response = result("arf_io_bankroll")
    assert any(row["key"] == "io_bankroll_public_conflict" for row in response["conflicts"])


def test_draft_approved_configuration_requires_written_evidence():
    with pytest.raises(ValidationError):
        ChannelPatch(intake_mode="partner_hosted_authorized", publication_state="draft_pending_approval", approvals={"hosted_intake": {"approved": True}}, permitted_fields=["annual_revenue"], reason="Configured", confirmed=True)
    with pytest.raises(ValidationError):
        ChannelPatch(intake_mode="referral_only", publication_state="published", reason="Configured", confirmed=True)


def test_referral_rejects_financial_fields_and_unknown_product():
    from uuid import uuid4
    fields = {"idempotency_key": uuid4(), "full_name": "Test Business", "email": "test@example.com", "consent": True}
    with pytest.raises(ValidationError):
        ReferralCreate(**fields, annual_revenue=1000000)
    with pytest.raises(ValidationError):
        ReferralCreate(**fields, program_key="loan_stars")


@pytest.mark.asyncio
async def test_referral_only_guard_applies_server_side():
    db = AsyncMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as exc:
        await arf.require_hosted_permission(db, requested_fields=["bank_data"])
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_hosted_scope_is_explicit_and_capabilities_independent():
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(intake_mode="partner_hosted_authorized", approvals={"hosted_intake": {"approved": True, "evidence_reference": "written authorization"}}, permitted_fields=["annual_revenue"], permitted_documents=[])
    await arf.require_hosted_permission(db, requested_fields=["annual_revenue"])
    with pytest.raises(HTTPException):
        await arf.require_hosted_permission(db, requested_fields=["credit_data"])
    with pytest.raises(HTTPException):
        await arf.require_hosted_permission(db, capability="credit_pull")


@pytest.mark.asyncio
async def test_unpublished_channel_public_catalog_has_no_terms_or_industries():
    db = AsyncMock()
    db.get.return_value = None
    response = await arf.public_catalog(db)
    assert response["intake_mode"] == "referral_only"
    assert response["products"] == []
    assert response["industries"] == []
    assert response["source_taxonomy_complete"] is False
    db.execute.assert_not_awaited()


def test_evaluation_does_not_mutate_catalog_source():
    before = deepcopy(arf.PRODUCTS)
    result("arf_io_flex_pay", beacon_score=700, beacon_verified=True, homeownership_verified=True)
    assert arf.PRODUCTS == before
