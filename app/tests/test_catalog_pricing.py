from app.dealer_os.services.catalog_pricing import normalize_catalog_pricing


def test_fixed_term_scenarios_use_requested_amount_and_amortize() -> None:
    result = normalize_catalog_pricing(
        {
            "display": {"en": "10%-20%"},
            "scenarios": [
                {
                    "term_months": 36,
                    "rate_type": "fixed",
                    "best_rate": 0.10,
                    "highest_rate": 0.20,
                },
                {
                    "term_months": 60,
                    "rate_type": "fixed",
                    "best_rate": 0.10,
                    "highest_rate": 0.20,
                },
            ],
        },
        locale="en",
        requested_amount=250_000,
    )
    assert result["illustration_amount"] == 250_000
    assert [row["term_months"] for row in result["term_scenarios"]] == [36, 60]
    for row in result["term_scenarios"]:
        assert row["calculation_available"] is True
        assert row["best"]["monthly_payment"] < row["highest_cost"]["monthly_payment"]
        assert row["best"]["total_payments"] > 250_000


def test_indexed_scenario_requires_index_value_and_effective_date() -> None:
    result = normalize_catalog_pricing(
        {
            "display": {"en": "Prime + 6.5%"},
            "scenarios": [
                {
                    "term_months": 120,
                    "rate_type": "indexed",
                    "index_name": "Prime",
                    "spread": 0.065,
                    "index_value": None,
                    "effective_date": None,
                }
            ],
        },
        locale="en",
        requested_amount=None,
    )
    scenario = result["term_scenarios"][0]
    assert result["illustration_amount"] == 100_000
    assert scenario["calculation_available"] is False
    assert scenario["best"] is None
    assert scenario["highest_cost"] is None
    assert "Index value" in scenario["unavailable_reason"]
