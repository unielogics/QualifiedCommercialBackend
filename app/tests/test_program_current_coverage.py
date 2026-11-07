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


def account_banks(last4, deposits=10000, withdrawals=8000, *, accepted=True):
    rows = banks(accepted=accepted)
    for row in rows:
        row["file_id"] = f"{last4}-" + row["file_id"]
        row["analysis_id"] = "analysis-" + row["file_id"]
        row["key_facts"].update(account_last4=last4, total_deposits_and_credits=deposits,
                                 total_withdrawals_and_debits=withdrawals)
    return rows


def estimate_rows(rows, **scope):
    return programs.program_bank_estimate_rows(rows, **scope)


def test_program_bank_proxy_sums_distinct_accounts_in_same_month_with_stable_sources():
    rows = account_banks("1234") + account_banks("5678", 5000, 3000)
    result = estimate_rows(rows)
    assert result == estimate_rows(list(reversed(rows)))
    monthly, refs, warnings = result
    assert len(monthly) == 6 and len(refs) == 12 and not warnings
    assert all(row["deposits"] == 15000 and row["withdrawals"] == 11000 for row in monthly)
    assert programs.estimate_annualized_deposits(monthly) == 180000
    assert programs.estimate_cash_flow_from_statements(monthly) == 48000
    target = {"annual_revenue": None, "annualized_deposits": None, "cash_flow": None}
    sources = {}
    programs.apply_statement_estimates(target, sources, accepted_months=6, bank_rows=monthly, bank_source_refs=refs)
    assert target["annual_revenue"] == 180000
    assert sources["annual_revenue"]["verification_status"] == "estimated_from_deposits"
    assert sources["annual_revenue"]["source_refs"] == refs


def test_same_account_identical_duplicates_do_not_double_count_or_change_refs_on_reorder():
    rows = account_banks("1234")
    duplicate = deepcopy(rows[0])
    duplicate["file_id"], duplicate["analysis_id"] = "identical-duplicate", "duplicate-analysis"
    rows.append(duplicate)
    monthly, refs, warnings = estimate_rows(rows)
    assert (monthly, refs, warnings) == estimate_rows(list(reversed(rows)))
    assert programs.estimate_annualized_deposits(monthly) == 120000
    assert len(refs) == 7 and not warnings


@pytest.mark.parametrize("richer", [False, True])
def test_conflicting_accepted_duplicates_require_review_instead_of_order_or_richer_metadata_priority(richer):
    rows = account_banks("1234")
    duplicate = deepcopy(rows[0])
    duplicate["file_id"] = "conflicting-duplicate"
    duplicate["key_facts"]["total_deposits_and_credits"] = 999999
    if richer:
        duplicate["key_facts"]["beginning_balance"] = 200
    rows.append(duplicate)
    result = estimate_rows(rows)
    assert result == estimate_rows(list(reversed(rows)))
    monthly, refs, warnings = result
    assert len(monthly) == 5 and len(refs) == 5
    assert warnings[0]["reason"] == "conflicting_counted_account_month"


def test_counted_scope_is_resolved_before_acceptance_and_unaccepted_totals_are_excluded():
    first, second = account_banks("1234"), account_banks("5678", 999999, 0, accepted=False)
    monthly, refs, warnings = estimate_rows(first + second)
    assert programs.estimate_annualized_deposits(monthly) == 120000
    assert len(refs) == 6 and all(ref["accepted"] for ref in refs)
    second_key = financial_cockpit.account_key_of(second[0]["key_facts"])
    scoped, scoped_refs, warnings = estimate_rows(first + second, counted_accounts=[second_key])
    assert scoped == scoped_refs == []
    assert all(warning["reason"] == "counted_account_source_unaccepted" for warning in warnings)
    missing, missing_refs, warnings = estimate_rows(first, counted_accounts=["Missing Bank ••0000"])
    assert missing == missing_refs == [] and warnings == [{"reason": "configured_counted_accounts_unavailable"}]


def test_counted_aliases_use_one_physical_account_without_counting_its_duplicate_twice():
    first, second = account_banks("1234"), account_banks("5678")
    first_key = financial_cockpit.account_key_of(first[0]["key_facts"])
    second_key = financial_cockpit.account_key_of(second[0]["key_facts"])
    scope = {"counted_accounts": [first_key], "account_aliases": {first_key: second_key}}
    result = estimate_rows(first + second, **scope)
    assert result == estimate_rows(list(reversed(first + second)), **scope)
    monthly, refs, warnings = result
    assert programs.estimate_annualized_deposits(monthly) == 120000
    assert len(refs) == 12 and not warnings and {ref["account_key"] for ref in refs} == {second_key}


def test_missing_accepted_account_operands_stay_unknown_and_zero_deposits_are_preserved():
    first, second = account_banks("1234"), account_banks("5678", 5000, 3000)
    second[0]["key_facts"]["total_deposits_and_credits"] = None
    monthly, _, _ = estimate_rows(first + second)
    assert monthly[0]["deposits"] is None and monthly[0]["withdrawals"] == 11000
    target = {"annual_revenue": None, "annualized_deposits": None}
    programs.apply_statement_estimates(target, {}, accepted_months=6, bank_rows=monthly)
    assert target["annual_revenue"] is None  # Six evidence months, only five complete deposit months.
    zero_rows, _, _ = estimate_rows(account_banks("1234", 0, 0))
    zero = {"annual_revenue": None, "annualized_deposits": None}
    programs.apply_statement_estimates(zero, {}, accepted_months=6, bank_rows=zero_rows)
    assert zero["annual_revenue"] == 0 and zero["annualized_deposits"] == 0


def test_complementary_accepted_duplicates_merge_only_observed_agreeing_fields():
    left = account_banks("1234")[0]
    right = deepcopy(left)
    left["key_facts"].pop("total_deposits_and_credits")
    left["key_facts"].update(beginning_balance=20, nsf_or_overdraft_count=0)
    right["key_facts"].update(beginning_balance=20)
    right["file_id"], right["analysis_id"] = "complementary", "complementary-analysis"
    result = estimate_rows([left, right])
    assert result == estimate_rows([right, left])
    monthly, refs, warnings = result
    assert monthly[0]["deposits"] == 10000 and monthly[0]["withdrawals"] == 8000
    assert monthly[0]["nsf"] == 0 and len(refs) == 2 and not warnings


def test_richer_pending_duplicate_neither_promotes_nor_suppresses_accepted_bank_math():
    accepted = account_banks("1234")[0]
    pending = deepcopy(accepted)
    pending["accepted"] = False
    pending["file_id"] = "pending"
    pending["key_facts"].update(total_deposits_and_credits=999999, beginning_balance=200)
    original_rows, original_refs, _ = estimate_rows([accepted])
    rows, refs, warnings = estimate_rows([accepted, pending])
    assert rows == original_rows and refs == original_refs
    assert warnings[0]["reason"] == "counted_account_source_unaccepted"
    assert (rows, refs, warnings) == estimate_rows([pending, accepted])


def test_counted_bank_only_member_folds_into_its_known_account_without_other_banks():
    rows = account_banks("1234")
    bank_only = deepcopy(rows[0])
    bank_only["key_facts"].pop("account_last4")
    orphan_key = financial_cockpit.account_key_of(bank_only["key_facts"])
    other = account_banks("5678", 999999)
    for row in other:
        row["key_facts"]["bank"] = "Other Bank"
    monthly, refs, warnings = estimate_rows(rows + other, counted_accounts=[orphan_key])
    # The bank-only selected key can be mapped to a known account only when
    # its source is present in the full inventory's inferred membership.
    assert monthly == refs == [] and warnings
    bank_only["file_id"], bank_only["analysis_id"] = "bank-only", "bank-only-analysis"
    monthly, refs, warnings = estimate_rows(rows + [bank_only] + other, counted_accounts=[orphan_key])
    assert programs.estimate_annualized_deposits(monthly) == 120000
    assert len(refs) == 7 and not warnings


def test_unresolved_bank_account_basis_cannot_sneak_back_through_a_cockpit_proxy_but_tax_basis_is_unchanged():
    target = context()
    programs._apply_current_business_coverage(target, build(banks()), accepted_bank_months=6, bank_basis_unresolved=True)
    assert target["dscr"] is None and target["cash_flow"] is None
    assert target["fact_sources"]["dscr"]["verification_status"] == "unknown"
    assert "Resolve conflicting" in target["fact_sources"]["dscr"]["reason"]
    tax_target = context()
    programs._apply_current_business_coverage(tax_target, build([tax()]), accepted_bank_months=6, bank_basis_unresolved=True)
    assert tax_target["dscr"] == build([tax()])["dscr"]["headline"]["value"]
