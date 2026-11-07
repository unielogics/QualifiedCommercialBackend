"""Tax operands retain period, business identity and missing component meaning."""

import pytest

from app.services.financial_tax_earnings import (
    entities_match,
    normalize_tax_earnings,
    tax_source_is_business,
)


@pytest.mark.parametrize("observed", [
    "Diversity Capital Partners LLC", "Diversity Capital Partners, L.L.C.",
    "DIVERSITY CAPITAL PARTNERS INCORPORATED", "Diversity Capital Partners Ltd.",
    "Diversity Capital Partners Limited Liability Company",
])
def test_business_matching_normalizes_only_legal_suffixes(observed):
    assert entities_match("Diversity Capital Partners", observed)


@pytest.mark.parametrize("expected,observed", [
    ("Diversity Capital Partners", "Diversity Real Estate Partners LLC"),
    ("Diversity Capital Partners", "Orchard - Bradburn"),
    ("Diversity Capital Partners", "Steven and Paige Nelson"),
    ("Name", "Name Holdings LLC"),
    ("", "LLC"),
])
def test_business_matching_does_not_invent_trade_name_or_personal_associations(expected, observed):
    assert not entities_match(expected, observed)


def test_conjunction_and_punctuation_match_without_fuzzy_words():
    assert entities_match("A & B Company", "A and B Company LLC")
    assert not entities_match("A and B", "A B LLC")


@pytest.mark.parametrize("form", ["Form 1040", "1040-SR", "1040NR", "1040-X", "1040 with Schedule C", "1065 Schedule K-1", "1120-S K1"])
def test_personal_returns_are_not_business_earnings(form):
    facts = {"form": form, "entity_name": "Diversity Capital Partners LLC",
             "ordinary_business_income": 100, "taxable_income": 27911}
    assert not tax_source_is_business(facts)
    assert normalize_tax_earnings(facts)["business_tax_source"] is False


@pytest.mark.parametrize("form,normalized", [
    ("Form 1065", "1065"), ("1120", "1120"), ("1120-S", "1120S"), ("Form 1120S", "1120S"),
])
def test_us_business_forms_supply_currency_and_accounting_method(form, normalized):
    result = normalize_tax_earnings({"tax_form": form, "accounting_method": "Cash"})
    assert result["business_tax_source"] is True
    assert result["form"] == normalized
    assert result["currency"] == "USD"
    assert result["basis"] == "cash"


def test_explicit_foreign_currency_is_never_overridden_by_form_default():
    assert normalize_tax_earnings({"form": "1065", "currency": "CAD"})["currency"] == "CAD"
    assert normalize_tax_earnings({"form": "1065", "currency": "XXX"})["currency"] is None
    assert normalize_tax_earnings({"entity_name": "Example LLC"})["currency"] is None


def test_business_2024_current_amortization_includes_known_amount_without_inventing_depreciation():
    result = normalize_tax_earnings({
        "form": "1065", "interest": 72275,
        "amortization_within_other_deductions": 53333,
        "accumulated_amortization": 177777, "franchise_fee_within_other_deductions": 141434,
        "payroll_taxes_within_taxes_and_licenses": 302692,
    })
    assert result["interest"] == 72275
    assert result["depreciation"] == 53333
    assert result["depreciation_complete"] is False
    assert result["missing_da_components"] == ["depreciation"]
    assert result["component_fact_paths"]["depreciation"] == ["amortization_within_other_deductions"]
    assert 5720 + result["interest"] + result["depreciation"] == 131328


def test_business_2025_nested_amortization_and_zero_depreciation_are_complete():
    result = normalize_tax_earnings({
        "tax_form": "Form 1065", "interest_expense": "163,121",
        "depreciation": 0, "other_deductions_detail": {"amortization": 53333, "franchise_fee": 143421},
        "accumulated_amortization": 231110,
    })
    assert result["depreciation"] == 53333
    assert result["depreciation_complete"] is True
    assert result["missing_da_components"] == []
    assert result["component_fact_paths"]["depreciation"] == ["depreciation", "other_deductions_detail.amortization"]
    assert -410155 + result["interest"] + result["depreciation"] == -193701


def test_combined_da_wins_without_summing_overlapping_components():
    result = normalize_tax_earnings({"depreciation_and_amortization": 10000,
                                      "depreciation": 7000, "amortization": 3000})
    assert result["depreciation"] == 10000
    assert result["depreciation_complete"] is True
    assert result["component_fact_paths"]["depreciation"] == ["depreciation_and_amortization"]
    explicit_zero = normalize_tax_earnings({"depreciation_and_amortization": 0,
                                           "depreciation": 7000, "amortization": 3000})
    assert explicit_zero["depreciation"] == 0


def test_aliases_select_once_and_support_wrapped_facts():
    result = normalize_tax_earnings({
        "interest": {"value": "$1,000.00"}, "interest_expense": 1000,
        "current_year_depreciation": {"value": 2000},
        "amortization": 3000, "amortization_within_other_deductions": 3000,
        "other_deductions_detail": {"amortization": 3000},
    })
    assert result["interest"] == 1000
    assert result["depreciation"] == 5000
    assert result["component_fact_paths"]["depreciation"] == ["current_year_depreciation", "amortization"]


def test_missing_components_and_accumulated_balances_are_never_zeros_or_addbacks():
    result = normalize_tax_earnings({"accumulated_amortization": 231110,
                                     "accumulated_depreciation": 10000,
                                     "taxes_and_licenses": 276661, "franchise_fee": 143421})
    assert result["interest"] is None
    assert result["depreciation"] is None
    assert result["depreciation_complete"] is False
    assert result["component_fact_paths"]["depreciation"] == []


@pytest.mark.parametrize("invalid", [True, "NaN", "Infinity", "not read", {"value": None}])
def test_invalid_financial_amounts_remain_unknown(invalid):
    result = normalize_tax_earnings({"interest": invalid, "depreciation_and_amortization": invalid})
    assert result["interest"] is None
    assert result["depreciation"] is None


def test_bare_tax_year_is_not_assumed_to_be_a_calendar_year():
    result = normalize_tax_earnings({"form": "1065", "tax_year": "2025"})
    assert result["period_start"] is None and result["period_end"] is None


def test_explicit_calendar_year_phrase_provides_twelve_month_bounds():
    result = normalize_tax_earnings({"tax_year": "2025 (calendar year Jan 1 – Dec 31, 2025)"})
    assert (result["period_start"], result["period_end"]) == ("2025-01-01", "2025-12-31")


def test_explicit_short_and_fiscal_periods_are_preserved():
    facts = {"tax_year": "2025 calendar year", "period_start": "2025-04-01", "period_end": "2025-12-31"}
    result = normalize_tax_earnings(facts)
    assert (result["period_start"], result["period_end"]) == ("2025-04-01", "2025-12-31")
    assert facts["period_start"] == "2025-04-01"
    fiscal = normalize_tax_earnings({"period_start": "2024-07-01", "period_end": "2025-06-30"})
    assert (fiscal["period_start"], fiscal["period_end"]) == ("2024-07-01", "2025-06-30")
