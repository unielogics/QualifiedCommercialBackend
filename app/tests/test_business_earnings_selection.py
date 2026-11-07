"""Business coverage must use the company's return, not owners' personal income."""

from copy import deepcopy

import pytest

from app.services.financial_cockpit import build_cockpit, tax_years
from app.services.financial_metric_reporting import metric_brief, validated_annual_pair


def source(key, facts):
    return {"file_id": key, "file_name": f"{key}.pdf", "classification": "tax_return",
            "accepted": True, "analysis_id": f"analysis-{key}", "key_facts": facts}


def business_tax():
    return source("business-2024", {
        "entity_name": "Example Capital Partners LLC", "tax_form": "Form 1120S",
        "tax_year": "Calendar year 2024", "basis": "cash",
        "ordinary_business_income": -600000, "interest": 200000,
        "other_deductions_detail": {"amortization": {"value": "206,299.00"}},
    })


def personal_tax(year):
    return source(f"personal-{year}", {
        "taxpayer": "Alex Example and Morgan Example", "tax_form": "Form 1040",
        "tax_year": str(year), "adjusted_gross_income": 900000,
        "ordinary_business_income": 900000, "interest": 40000, "depreciation": 50000,
    })


@pytest.mark.parametrize("reverse", [False, True])
def test_personal_returns_do_not_displace_suffix_matched_business_tax_or_nested_amortization(reverse):
    analyses = [personal_tax(2024), business_tax(), personal_tax(2025)]
    if reverse:
        analyses.reverse()
    original = deepcopy(analyses)
    debts = [{"id": "business-loan", "lender": "Example lender", "monthly_payment": 79879.55,
              "balance": 1000000, "origin": "admin", "count_in_dscr": True}]
    original_debts = deepcopy(debts)
    cockpit = build_cockpit(analyses, debts, business_entity="Example Capital Partners")

    assert [(year["year"], year["file_id"]) for year in cockpit["tax"]["years"]] == [("2024", "business-2024")]
    year = cockpit["tax"]["years"][0]
    assert year["depreciation"] == 206299 and year["ebitda"] == -193701
    assert year["component_fact_paths"]["depreciation"] == ["other_deductions_detail.amortization"]
    assert not year["depreciation_complete"]  # Known amortization never invents depreciation.
    assert year["currency"] == "USD" and year["tax_form"] == "1120S"
    assert cockpit["debt"]["service"]["annual"] == 958554.6
    current = cockpit["dscr"]["headline"]
    assert current["key"] == "tax_return" and current["numerator"] == -193701
    assert current["period"] == "2024" and current["value"] == -0.2
    assert current["currency_status"] == "comparable"
    assert cockpit["ebitda"]["headline"]["kind"] == "tax_estimate"
    assert cockpit["ebitda"]["headline"]["value"] == -193701
    assert validated_annual_pair(cockpit) is None  # Tax estimates never become factual P&L EBITDA.
    excluded = cockpit["metric_review"]["excluded_income_sources"]
    assert {item["file_id"] for item in excluded} == {"personal-2024", "personal-2025"}
    assert all("Personal" in item["reason"] for item in excluded)
    assert all("personal-" not in item["key"] for item in cockpit["metric_review"]["inputs"])
    assert metric_brief(cockpit)["primary_dscr"]["value"] == -0.2
    assert analyses == original and debts == original_debts


def test_business_selection_precedes_year_limit_with_newer_personal_returns():
    analyses = [business_tax(), personal_tax(2024), personal_tax(2025)]
    original = deepcopy(analyses)
    selected = tax_years(analyses, limit=1, business_entity="Example Capital Partners")
    assert len(selected) == 1 and selected[0]["file_id"] == "business-2024"
    assert selected[0]["year"] == "2024" and selected[0]["ebitda"] == -193701
    assert analyses == original
