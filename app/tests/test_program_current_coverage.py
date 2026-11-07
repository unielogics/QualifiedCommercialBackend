"""Program rules use factual current annual operands, never cached AI math."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.services import application_programs as programs
from app.services import financial_cockpit
from app.tests.test_financial_metric_review import build, pnl, tax


def context(**extra):
    return {"dscr": 99, "cash_flow": 999999,
            "fact_sources": {"dscr": {"verification_status": "provisional"},
                             "cash_flow": {"verification_status": "provisional"}}, **extra}


def apply(cockpit, *, accepted_months=0, original=None):
    result = context() if original is None else original
    programs._apply_current_business_coverage(result, cockpit, accepted_bank_months=accepted_months)
    return result


def test_current_tax_estimate_replaces_cached_dscr_and_cash_flow_with_matching_provenance():
    cockpit = build([tax(ordinary_business_income=-410155, interest=163121, depreciation_and_amortization=53333)])
    before = deepcopy(cockpit)
    result = apply(cockpit, original=context(estimated_fit_facts=["cash_flow", "annual_revenue"]))
    assert result["dscr"] == -16.14 and result["cash_flow"] == -193701
    for field in ("dscr", "cash_flow"):
        source = result["fact_sources"][field]
        assert source["verification_status"] == "provisional" and source["basis"] == "tax_return"
        assert source["source_refs"][0]["file_id"] == "tax"
        assert source["annual_debt_service"] == 12000
        assert source["calculation_signature"] == result["financial_calculation_authority"]["signature"]
    assert cockpit == before
    assert result["estimated_fit_facts"] == ["annual_revenue"]


def test_known_zero_coverage_and_complete_zero_annual_earnings_remain_verified():
    cockpit = build([pnl(net_income=0, interest=0, income_taxes=0, depreciation_and_amortization=0)])
    result = apply(cockpit)
    assert result["dscr"] == 0 and result["cash_flow"] == 0
    assert all(result["fact_sources"][field]["verification_status"] == "verified" for field in ("dscr", "cash_flow"))
    assert programs.evaluation_values(result, result["fact_sources"])["dscr"] == 0


def test_current_income_survives_unknown_debt_without_manufacturing_a_ratio():
    cockpit = financial_cockpit.build_cockpit([pnl()], [], business_entity="Example LLC")
    result = apply(cockpit)
    assert result["dscr"] is None and result["cash_flow"] == 52000
    assert result["fact_sources"]["dscr"]["verification_status"] == "unknown"
    assert result["fact_sources"]["cash_flow"]["verification_status"] == "verified"


@pytest.mark.parametrize("source", [pnl(currency="EUR"), tax(entity_name="Other LLC"),
    {"file_id": "owner", "classification": "tax_return", "accepted": True,
     "key_facts": {"tax_form": "Form 1040", "taxable_income": 999999, "tax_year": "2025"}}])
def test_ineligible_or_personal_sources_cannot_resurrect_cached_program_math(source):
    result = apply(build([source]))
    assert result["dscr"] is None and result["cash_flow"] is None
    assert all(result["fact_sources"][field]["verification_status"] == "unknown" for field in ("dscr", "cash_flow"))


def test_hypothetical_plan_does_not_become_a_factual_program_qualification_input():
    cockpit = build([pnl()], plan={"overrides": {"income_annual": 900000, "debt_service_monthly": 1}})
    assert cockpit["plan"]["evaluation"]["headline_after"] != cockpit["dscr"]["headline"]["value"]
    result = apply(cockpit)
    assert result["dscr"] == 4.33 and result["cash_flow"] == 52000


def test_reviewed_scalar_is_retained_as_an_assumption_rather_than_overriding_current_math():
    prior = context(fact_sources={field: {"verification_status": "verified", "source_refs": [{"id": "scalar"}]}
                                   for field in ("dscr", "cash_flow")})
    result = apply(build(), original=prior)
    assert result["dscr"] == 4.33 and result["cash_flow"] == 52000
    assert result["fact_sources"]["dscr"]["reviewed_assumption"]["value"] == 99
    assert result["fact_sources"]["cash_flow"]["reviewed_assumption"]["value"] == 999999


def test_failed_current_source_read_remains_unknown_even_with_a_reviewed_old_scalar():
    prior = context(fact_sources={"dscr": {"verification_status": "verified", "source_refs": [{"id": "old"}]},
                                   "cash_flow": {"verification_status": "provisional"}})
    result = apply(None, original=prior)
    assert result["dscr"] is None and result["cash_flow"] is None
    assert programs.evaluation_values(result, result["fact_sources"])["dscr"] is None
    assert result["fact_sources"]["dscr"]["reviewed_assumption"]["value"] == 99


def banks(count=6, *, accepted=True):
    return [{"file_id": f"bank-{month}", "classification": "bank_statement", "accepted": accepted,
             "key_facts": {"bank": "Example Bank", "currency": "USD", "account_last4": "1234",
                           "statement_period": f"2026-{month:02d}", "total_deposits_and_credits": 10000,
                           "total_withdrawals_and_debits": 8000}} for month in range(1, count + 1)]


def test_program_bank_proxy_keeps_its_three_accepted_month_minimum_and_labels_its_basis():
    cockpit = build(banks())
    result = apply(cockpit, accepted_months=6)
    assert result["dscr"] == 3 and result["cash_flow"] == 36000
    assert all(result["fact_sources"][field]["verification_status"] == "estimated_from_statements" for field in ("dscr", "cash_flow"))
    assert result["estimated_fit_facts"] == ["dscr", "cash_flow"]
    minimum = apply(build(banks(3)), accepted_months=3)
    assert minimum["dscr"] == 3 and minimum["cash_flow"] == 36000
    short = apply(build(banks(3)), accepted_months=2)
    assert short["dscr"] is None and short["cash_flow"] is None
    assert "Three accepted" in short["fact_sources"]["dscr"]["reason"]


def test_unaccepted_bank_source_in_current_combined_basis_cannot_qualify_a_program():
    rows = banks()
    rows[-1]["accepted"] = False
    result = apply(build(rows), accepted_months=6)
    assert result["dscr"] is None and result["cash_flow"] is None
    assert all(result["fact_sources"][field]["verification_status"] == "unknown" for field in ("dscr", "cash_flow"))


async def profile_context(*, vertical="main_street", load=None):
    intake = SimpleNamespace(id=uuid4(), bucket_id=None, variant="main_street_v1", business_name="Example LLC",
        estimated_credit_score=None, asset_rows=[], intake_state={},
        result_snapshot={"key_metrics": {"estimated_dscr": 0, "dscr": 99, "estimated_ebitda_or_cash_flow": 999999}})
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id, primary_bucket_id=None, vertical=vertical,
        funding_category="term", entity_type=None, industry=None, subindustry=None, naics_code=None,
        financial_metric_review={}, financial_plan={})
    query = Mock()
    query.scalars.return_value.all.return_value = []
    query.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=query), get=AsyncMock(return_value=intake))
    budget = SimpleNamespace(items=[], total=None, real_estate_equipment_amount=None, real_estate_equipment_pct=None,
                             complete=False, category_totals={"equipment": 0})
    with (patch.object(programs, "source_funding_data", AsyncMock(return_value=(None, None, "working_capital", None))),
          patch.object(programs, "summarize_use_of_funds", return_value=budget),
          patch.object(programs, "_evidence_inventory", AsyncMock(return_value=([], {}, set()))),
          patch.object(programs, "accepted_evidence_file_ids", AsyncMock(return_value=set())),
          patch.object(financial_cockpit, "load_cockpit", load or AsyncMock(return_value=build())) as read):
        result = await programs.profile_fit_context(db, profile)
    return result, read, intake


@pytest.mark.asyncio
async def test_operating_profile_context_reads_current_source_and_fails_closed_without_cached_fallback():
    result, load, intake = await profile_context()
    load.assert_awaited_once()
    assert result["dscr"] == 4.33 and result["cash_flow"] == 52000
    assert intake.result_snapshot["key_metrics"]["estimated_ebitda_or_cash_flow"] == 999999
    failed, _, _ = await profile_context(load=AsyncMock(side_effect=RuntimeError("Evidence unavailable")))
    assert failed["dscr"] is None and failed["cash_flow"] is None


@pytest.mark.asyncio
async def test_property_profile_preserves_its_distinct_basis_and_a_known_zero():
    result, load, _ = await profile_context(vertical="real_estate")
    load.assert_not_awaited()
    assert result["dscr"] == 0  # A business earnings projection must not replace property coverage.
    assert "financial_calculation_authority" not in result
