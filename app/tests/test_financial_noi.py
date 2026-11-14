from copy import deepcopy

import pytest

from app.services.financial_noi import build_noi

ENTITY = "Example LLC"


def source(year=2025, *, key=None, kind="current_p_and_l", **facts):
    return {"file_id": key or f"pnl-{year}", "file_name": f"Business-{year}.pdf", "classification": kind,
            "analysis_id": f"analysis-{year}", "content_hash": "a" * 64, "accepted": True,
            "key_facts": {"business_name": ENTITY, "period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
                          "currency": "USD", "basis": "cash", "gross_revenue": 500000,
                          "cost_of_goods_sold": 100000, "total_operating_expenses": 350000, **facts}}


def build(rows, **kwargs):
    return build_noi(rows, entity=ENTITY, **kwargs)


def test_full_fundamental_breakdown_keeps_da_expensed_and_never_adds_interest_or_loan_payments():
    row = source(depreciation_and_amortization=20000, interest=10000, net_income=40000)
    before = deepcopy(row)
    result = build([row])
    headline = result["headline"]
    assert headline["value"] == headline["annualized_value"] == 50000
    assert headline["margin_pct"] == 10
    assert [line["signed_value"] for line in headline["fundamentals"][:3]] == [500000, -100000, -350000]
    assert result["definition"]["includes_depreciation_and_amortization"] is True
    assert headline["complete"] and not headline["provisional"]
    assert row == before


def test_negative_noi_remains_negative_and_refinance_changes_only_debt_service_residual():
    result = build([source(total_operating_expenses=450000)], current_annual_debt_service=80000,
                   plan_annual_debt_service=30000)
    assert result["headline"]["value"] == -50000
    scenario = result["scenario"]
    assert scenario["current"] == scenario["plan"] == -50000
    assert scenario["after_debt_service_current"] == -130000
    assert scenario["after_debt_service_plan"] == -80000


def test_only_explicit_operating_changes_affect_plan_noi():
    result = build([source()], current_annual_debt_service=30000, plan_annual_debt_service=0,
                   operating_adjustments=[{"id": "rent", "kind": "rent_to_owner", "label": "Reviewed annual rent saving", "amount_annual": 10000,
                                           "source": "lease", "note": "Owner agrees to reduce rent", "status": "hypothetical_unverified"}])
    assert result["scenario"]["current"] == 50000
    assert result["scenario"]["plan"] == 60000
    assert result["scenario"]["after_debt_service_plan"] == 60000
    assert result["headline"]["value"] == 50000


def test_financing_da_generic_and_unreviewed_addbacks_never_change_operating_noi():
    template = {"label": "Adjustment", "amount_annual": 10000, "source": "source", "note": "Hypothesis",
                "status": "hypothetical_unverified"}
    adjustments = [{**template, "kind": kind} for kind in ("interest_refinanced", "depreciation", "other")]
    adjustments.extend([{**template, "kind": "owner_salary", "status": "unreviewed"},
                        {"kind": "owner_salary", "label": "Arbitrary value", "amount": 900000}])
    result = build([source()], operating_adjustments=adjustments)
    assert result["scenario"]["current"] == result["scenario"]["plan"] == 50000
    assert result["scenario"]["operating_adjustments"] == []


def test_repeated_identical_operating_adjustments_count_once():
    adjustment = {"id": "unique-rent", "kind": "rent_to_owner", "label": "Rent reduction", "amount_annual": 10000,
                  "source": "lease", "note": "Owner agrees to reduce rent", "status": "hypothetical_unverified"}
    result = build([source()], operating_adjustments=[adjustment, adjustment])
    assert result["scenario"]["plan"] == 60000
    assert len(result["scenario"]["operating_adjustments"]) == 1


def test_filed_return_supplies_operating_income_and_bank_deposits_never_do():
    tax = source(kind="tax_return", tax_form="1120-S", ordinary_business_income=250000,
                 operating_income=None, net_income=250000)
    bank = source(kind="bank_statement", deposits=500000, withdrawals=100000)
    result = build([tax, bank])
    headline = result["headline"]
    assert headline["kind"] == "tax_derived_noi" and headline["annualized_value"] == 250000
    # Interest the reader did not find is not added back, and is named.
    assert headline["missing_inputs"] == ["interest"] and "interest_not_read" in result["source_limitations"]
    assert result["candidates"] == [] and result["status"] == "source_supported"
    assert result["scenario"]["current"] == 250000
    only_bank = build([bank])
    assert only_bank["headline"] is None and only_bank["scenario"]["current"] is None


def test_personal_and_other_business_sources_never_contribute_or_override_latest_period():
    business = source()
    personal = source(2026, key="personal", kind="tax_return", tax_form="1040", filers="Household",
                      business_name=None, ordinary_business_income=800000)
    other = source(2026, key="other", business_name="Other LLC")
    result = build([business, personal, other])
    assert result["headline"]["source_refs"][0]["file_id"] == "pnl-2025"
    assert len(result["candidates"]) == 1


def test_newest_incomplete_period_does_not_fall_back_to_profitable_prior_year():
    result = build([source(2024), source(2025, total_operating_expenses=None)])
    assert result["headline"] is None
    assert "missing operating-income inputs" in result["reason"] and "older period is not substituted" in result["reason"]
    assert result["years"][-1]["value"] is None
    assert not result["trend"]["comparable"]


def test_newer_filed_return_supplies_noi_and_the_prior_pl_stays_history():
    latest_tax = source(2025, key="tax-2025", kind="tax_return", tax_form="1120-S", ordinary_business_income=200000)
    result = build([source(2024), latest_tax])
    assert result["headline"]["source_refs"][0]["file_id"] == "tax-2025"
    assert result["headline"]["annualized_value"] == 200000
    assert result["years"][0]["value"] == 50000


def test_latest_printed_calendar_year_tax_label_supplies_the_annual_noi():
    latest_tax = source(2025, key="tax-2025", kind="tax_return", tax_form="1120-S", tax_year="Calendar year 2025",
                        period_start=None, period_end=None, ordinary_business_income=200000)
    result = build([source(2024), latest_tax])
    assert result["headline"]["year"] == "2025" and result["headline"]["annualized_value"] == 200000
    assert result["years"][0]["year"] == "2024"
    assert result["scenario"]["current"] == 200000


def test_printed_year_without_pl_dates_does_not_invent_an_annual_noi_period():
    result = build([source(tax_year="Calendar year 2025", period_start=None, period_end=None)])
    assert result["headline"]["value"] == 50000
    assert result["status"] == "provisional" and result["review_needed"]
    assert result["candidates"][0]["months_covered"] is None
    assert result["candidates"][0]["annualized_value"] is None
    assert result["years"] == []


def test_ebitda_exclusions_do_not_change_independent_latest_noi():
    result = build([source(2024), source(2025)], source_selection={"excluded_file_ids": ["pnl-2025"]})
    assert result["headline"]["source_refs"][0]["file_id"] == "pnl-2025"


def test_stale_ebitda_source_review_does_not_block_independent_noi():
    result = build([source()], source_selection={"stale": True})
    assert result["headline"]["value"] == 50000
    assert result["status"] == "source_supported"


@pytest.mark.parametrize("status", ["denied", "stale"])
def test_newest_denied_or_stale_same_business_source_does_not_restore_old_noi(status):
    latest = source(2025)
    latest["identity_association"] = {"status": status}
    result = build([source(2024), latest])
    assert result["headline"] is None


def test_unresolved_newest_business_identity_does_not_restore_old_noi():
    result = build([source(2024), source(2025, business_name=None)])
    assert result["headline"] is None


def test_source_bound_current_correction_applies_without_rewriting_history():
    rows = [source(2024), source(2025)]
    before = deepcopy(rows)
    review = {"inputs": [{"key": "profit_and_loss.total_operating_expenses", "value": 300000,
                          "correction": {"action": "correct"}, "source_refs": [{"file_id": "pnl-2025"}]}]}
    result = build(rows, metric_review=review)
    assert result["headline"]["value"] == 100000
    assert result["years"][0]["value"] == 50000
    assert "profit_and_loss.total_operating_expenses" in result["headline"]["input_keys"]
    assert result["headline"]["fundamentals"][2]["corrected"]
    assert rows == before


def test_marking_a_corrected_operand_unknown_does_not_reuse_the_raw_value():
    review = {"inputs": [{"key": "profit_and_loss.cost_of_goods_sold", "value": None,
                          "correction": {"action": "correct"}, "source_refs": [{"file_id": "pnl-2025"}]}]}
    result = build([source()], metric_review=review)
    assert result["headline"] is None


def test_reported_operating_income_is_retained_with_reconciliation_difference():
    result = build([source(operating_income=75000)])
    headline = result["headline"]
    assert headline["value"] == headline["reported_value"] == 75000
    assert headline["computed_value"] == 50000 and headline["reconciliation_difference"] == 25000
    assert headline["provisional"] and not headline["complete"]


def test_reported_noi_without_full_fundamentals_is_visible_but_provisional():
    result = build([source(operating_income=75000, cost_of_goods_sold=None)])
    assert result["headline"]["value"] == 75000
    assert result["headline"]["computed_value"] is None
    assert result["headline"]["provisional"]


def test_algebraically_derived_review_value_cannot_reconcile_the_reported_noi_that_created_it():
    review = {"inputs": [{"key": "profit_and_loss.total_operating_expenses", "value": 325000,
                          "correction": None, "source_refs": [{"file_id": "pnl-2025", "fact_present": False}]}]}
    result = build([source(operating_income=75000, total_operating_expenses=None)], metric_review=review)
    assert result["headline"]["value"] == result["headline"]["reported_value"] == 75000
    assert result["headline"]["computed_value"] is None
    assert not result["headline"]["complete"] and result["headline"]["provisional"]


def test_current_explicit_staff_correction_can_complete_a_missing_expense_operand():
    review = {"inputs": [{"key": "profit_and_loss.total_operating_expenses", "value": 325000,
                          "correction": {"action": "correct"}, "source_refs": [{"file_id": "pnl-2025", "fact_present": False}]}]}
    result = build([source(operating_income=75000, total_operating_expenses=None)], metric_review=review)
    assert result["headline"]["computed_value"] == 75000
    assert result["headline"]["complete"]


def test_fundamental_correction_changes_working_noi_while_retaining_reported_source_amount():
    review = {"inputs": [{"key": "profit_and_loss.total_operating_expenses", "value": 360000,
                          "correction": {"action": "correct"}, "source_refs": [{"file_id": "pnl-2025"}]}]}
    result = build([source(operating_income=20000, total_operating_expenses=380000)], metric_review=review)
    headline = result["headline"]
    assert headline["value"] == headline["reviewed_value"] == headline["computed_value"] == 40000
    assert headline["source_value"] == headline["reported_value"] == 20000
    assert headline["staff_adjusted"] and headline["provisional"]
    assert headline["reconciliation_difference"] == -20000
    assert headline["input_keys"] == ["profit_and_loss.total_operating_expenses"]
    assert headline["active_input_keys"] == ["profit_and_loss.total_operating_expenses"]


def test_explicit_reported_operating_income_correction_takes_priority_over_component_review():
    review = {"inputs": [
        {"key": "profit_and_loss.total_operating_expenses", "value": 360000, "correction": {"action": "correct"},
         "source_refs": [{"file_id": "pnl-2025"}]},
        {"key": "profit_and_loss.reported_operating_income", "value": 45000, "correction": {"action": "correct"},
         "source_refs": [{"file_id": "pnl-2025"}]},
    ]}
    headline = build([source(operating_income=20000, total_operating_expenses=380000)], metric_review=review)["headline"]
    assert headline["value"] == headline["reviewed_value"] == 45000
    assert headline["computed_value"] == 40000 and headline["reported_value"] == 20000
    assert headline["input_keys"] == ["profit_and_loss.total_operating_expenses", "profit_and_loss.reported_operating_income"]
    assert headline["active_input_keys"] == ["profit_and_loss.reported_operating_income"]


def test_reported_noi_exposes_all_reviewable_fundamentals_but_uses_only_the_reported_amount():
    values = {"gross_revenue": 500000, "cost_of_goods_sold": 100000,
              "total_operating_expenses": 350000, "reported_operating_income": 50000}
    review = {"inputs": [{"key": f"profit_and_loss.{key}", "value": value, "correction": None,
                          "source_refs": [{"file_id": "pnl-2025", "fact_key": "operating_income" if key == "reported_operating_income" else key}]}
                         for key, value in values.items()]}
    headline = build([source(operating_income=50000)], metric_review=review)["headline"]
    assert headline["input_keys"] == [f"profit_and_loss.{key}" for key in values]
    assert headline["active_input_keys"] == ["profit_and_loss.reported_operating_income"]
    assert headline["value"] == 50000
    assert headline["computed_value"] == 50000
    assert [row["source_refs"][0]["fact_key"] for row in headline["fundamentals"]] == [
        "gross_revenue", "cost_of_goods_sold", "total_operating_expenses", "operating_income"]


def test_marking_reported_noi_unknown_does_not_restore_its_old_source_amount():
    review = {"inputs": [{"key": "profit_and_loss.reported_operating_income", "value": None,
                          "correction": {"action": "correct"}, "source_refs": [{"file_id": "pnl-2025"}]}]}
    result = build([source(operating_income=50000)], metric_review=review)
    assert result["headline"] is None
    assert result["candidates"][0]["reported_value"] == 50000
    assert result["candidates"][0]["computed_value"] == 50000


@pytest.mark.parametrize("prior,current,direction,pct", [(-100000, -50000, "up", 50), (50000, -50000, "down", -200), (50000, 50000, "neutral", 0)])
def test_yearly_trend_uses_prior_only_for_signed_comparison(prior, current, direction, pct):
    result = build([source(2024, total_operating_expenses=400000 - prior),
                    source(2025, total_operating_expenses=400000 - current)])
    assert result["headline"]["value"] == current
    assert result["trend"]["direction"] == direction and result["trend"]["change_pct"] == pct
    assert result["trend"]["revenue_direction"] == "neutral"


def test_incompatible_yearly_currency_or_reporting_period_never_produces_a_trend():
    result = build([source(2024, currency="EUR"), source(2025)])
    assert not result["trend"]["comparable"] and result["trend"]["direction"] is None


def test_dated_half_year_pl_has_explicit_run_rate_and_is_not_a_full_annual_history_row():
    result = build([source(2026, period_end="2026-06-30", months_covered=6)], current_annual_debt_service=30000)
    assert result["headline"]["value"] == 50000 and result["headline"]["annualized_value"] == 100000
    assert result["headline"]["annualized_run_rate"]
    assert result["years"] == []
    assert result["scenario"]["after_debt_service_current"] == 70000


@pytest.mark.parametrize("currency", [None, "unknown", "EUR"])
def test_debt_service_comparison_requires_same_usd_currency(currency):
    result = build([source(currency=currency)], current_annual_debt_service=10000, plan_annual_debt_service=5000)
    assert result["headline"]["value"] == 50000
    assert result["scenario"]["after_debt_service_current"] is None
    assert result["scenario"]["after_debt_service_plan"] is None


def test_loan_payments_in_operating_expenses_do_not_get_deducted_twice():
    result = build([source(notable_expense_lines={"bank_loan_payments_ytd": 20000})], current_annual_debt_service=20000)
    assert result["headline"]["reconciliation_required"]
    assert result["scenario"]["after_debt_service_current"] is None


def test_explicit_zero_costs_and_expenses_are_known_values_not_missing():
    result = build([source(cost_of_goods_sold=0, total_operating_expenses=0)])
    assert result["headline"]["value"] == 500000
    assert result["headline"]["missing_inputs"] == []
