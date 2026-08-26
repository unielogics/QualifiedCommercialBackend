from app.dealer_os.services import application_prescreen
from app.dealer_os.services.lender_neutral_routing import (
    TERM_PROGRAM_KEY,
    WORKING_CAPITAL_PROGRAM_KEY,
    evaluate_direct_programs,
)
from app.dealer_os.services.product_finder import screen_products


def _facts(**overrides):
    facts = {
        "requested_amount": 50_000,
        "use_of_funds": "Purchase inventory and support payroll.",
        "years_in_business": 4,
        "annual_revenue": 1_200_000,
        "owner_count": 1,
        "owners": [{
            "owner_id": "owner-1",
            "residency_status": "citizen",
            "credit_660_or_higher": True,
            "bankruptcy_timing": "none",
            "foreclosure_within_3_years": False,
            "felony_timing": "none",
            "misdemeanor_within_5_years": False,
            "misdemeanor_involving_minor": False,
            "arrest_within_6_months": False,
            "financial_related_crime": False,
            "active_legal_charges": False,
            "ofac_match": False,
        }],
        "mca_count": 0,
        "youngest_mca_days": 120,
        "positive_month_end_count": 3,
        "official_bank_statements": True,
        "nsf_count": 0,
        "negative_balance_days": 0,
        "active_ucc_count": 0,
        "annualized_bank_sales": 1_200_000,
        "annual_cash_flow_available_for_debt": 180_000,
        "monthly_debt_payments": 10_000,
        "open_tax_liens": False,
        "tax_liability_over_10000": False,
        "open_judgments": False,
        "open_civil_actions_as_defendant": False,
        "civil_action_financial_institution_within_10_years": False,
        "judgment_over_2000_within_12_months": False,
        "judgment_over_50000_within_7_years": False,
        "aggregate_liens_judgments_over_25000_within_7_years": False,
        "taxonomy_status": "official",
        "naics_code": "541611",
        "state": "NJ",
    }
    facts.update(overrides)
    return facts


def _program(result, key):
    return next(row for row in result["programs"] if row["program_key"] == key)


def _rule_ids(result, key):
    return {row["rule_id"] for row in _program(result, key)["matched_rules"]}


def test_standard_restaurant_only_blocks_ten_year_path():
    result = evaluate_direct_programs(_facts(naics_code="722511"))
    assert _program(result, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "blocked"
    assert _program(result, TERM_PROGRAM_KEY)["status"] == "recommended"
    assert f"{WORKING_CAPITAL_PROGRAM_KEY}.naics_7225" in _rule_ids(
        result, WORKING_CAPITAL_PROGRAM_KEY
    )


def test_special_food_service_only_blocks_three_to_five_year_path():
    result = evaluate_direct_programs(_facts(naics_code="722320"))
    assert _program(result, TERM_PROGRAM_KEY)["status"] == "blocked"
    assert _program(result, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "recommended"
    assert f"{TERM_PROGRAM_KEY}.naics_7223" in _rule_ids(result, TERM_PROGRAM_KEY)


def test_laboratory_and_home_health_do_not_expand_to_general_clinics():
    for excluded_code, prefix in (("621511", "6215"), ("621610", "6216")):
        excluded = evaluate_direct_programs(_facts(naics_code=excluded_code))
        assert f"{TERM_PROGRAM_KEY}.naics_{prefix}" in _rule_ids(excluded, TERM_PROGRAM_KEY)

    for allowed_code in ("621111", "621210"):
        allowed = evaluate_direct_programs(_facts(naics_code=allowed_code))
        assert _program(allowed, TERM_PROGRAM_KEY)["status"] == "recommended"


def test_homebuilding_exclusion_is_exact_and_state_specific():
    for state in ("FL", "AZ", "CO"):
        result = evaluate_direct_programs(_facts(naics_code="236115", state=state))
        assert f"{TERM_PROGRAM_KEY}.naics_homebuilding_{state.lower()}" in _rule_ids(
            result, TERM_PROGRAM_KEY
        )

    outside_states = evaluate_direct_programs(_facts(naics_code="236115", state="NJ"))
    remodeler = evaluate_direct_programs(_facts(naics_code="236118", state="FL"))
    commercial_builder = evaluate_direct_programs(_facts(naics_code="236220", state="AZ"))
    assert _program(outside_states, TERM_PROGRAM_KEY)["status"] == "recommended"
    assert _program(remodeler, TERM_PROGRAM_KEY)["status"] == "recommended"
    assert _program(commercial_builder, TERM_PROGRAM_KEY)["status"] == "recommended"


def test_exact_auto_codes_and_transport_sector_are_independent_matches():
    for code in ("441110", "441120", "441210", "441222"):
        exact = evaluate_direct_programs(_facts(naics_code=code))
        assert f"{WORKING_CAPITAL_PROGRAM_KEY}.naics_{code}" in _rule_ids(
            exact, WORKING_CAPITAL_PROGRAM_KEY
        )
        expected_term_prefix = "4411" if code.startswith("4411") else "4412"
        assert f"{TERM_PROGRAM_KEY}.naics_{expected_term_prefix}" in _rule_ids(
            exact, TERM_PROGRAM_KEY
        )

    parts = evaluate_direct_programs(_facts(naics_code="441330"))
    assert f"{TERM_PROGRAM_KEY}.naics_4413" in _rule_ids(parts, TERM_PROGRAM_KEY)

    transportation = evaluate_direct_programs(_facts(naics_code="484121"))
    assert f"{TERM_PROGRAM_KEY}.naics_sector_48_49" in _rule_ids(
        transportation, TERM_PROGRAM_KEY
    )
    assert f"{WORKING_CAPITAL_PROGRAM_KEY}.naics_sector_48_49" in _rule_ids(
        transportation, WORKING_CAPITAL_PROGRAM_KEY
    )


def test_permanent_resident_can_use_term_path_but_not_citizen_only_path():
    result = evaluate_direct_programs(_facts(owners=[{
        **_facts()["owners"][0],
        "residency_status": "legal_permanent_resident",
    }]))
    assert _program(result, TERM_PROGRAM_KEY)["status"] == "recommended"
    assert _program(result, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "blocked"
    assert f"{WORKING_CAPITAL_PROGRAM_KEY}.citizen_only" in _rule_ids(
        result, WORKING_CAPITAL_PROGRAM_KEY
    )


def test_ten_year_amount_is_capped_at_half_of_annualized_bank_sales():
    result = evaluate_direct_programs(
        _facts(requested_amount=40_000, annualized_bank_sales=60_000)
    )
    program = _program(result, WORKING_CAPITAL_PROGRAM_KEY)
    assert program["status"] == "potential"
    assert program["estimated_max_amount"] == 30_000
    assert result["recommended_amount"] == 40_000
    assert result["amount_adjustment_required"] is False
    assert any("supports up to $30,000" in item for item in program["unresolved"])


def test_ten_year_sales_cap_below_program_minimum_is_a_hard_block():
    result = evaluate_direct_programs(
        _facts(requested_amount=15_000, annualized_bank_sales=20_000)
    )
    assert f"{WORKING_CAPITAL_PROGRAM_KEY}.annualized_sales_cap" in _rule_ids(
        result, WORKING_CAPITAL_PROGRAM_KEY
    )


def test_program_specific_legal_thresholds_do_not_cross_contaminate():
    recent_judgment = evaluate_direct_programs(
        _facts(judgment_over_2000_within_12_months=True)
    )
    assert _program(recent_judgment, TERM_PROGRAM_KEY)["status"] == "blocked"
    assert _program(recent_judgment, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "recommended"

    open_civil = evaluate_direct_programs(
        _facts(open_civil_actions_as_defendant=True)
    )
    assert _program(open_civil, TERM_PROGRAM_KEY)["status"] == "recommended"
    assert _program(open_civil, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "blocked"

    payment_plan = evaluate_direct_programs(
        _facts(tax_liability_over_10000=True, tax_payment_plan_current=True)
    )
    assert _program(payment_plan, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "recommended"

    no_plan = evaluate_direct_programs(
        _facts(tax_liability_over_10000=True, tax_payment_plan_current=False)
    )
    assert f"{WORKING_CAPITAL_PROGRAM_KEY}.tax_payment_plan" in _rule_ids(
        no_plan, WORKING_CAPITAL_PROGRAM_KEY
    )


def test_pending_taxonomy_requires_review_without_excluding_a_program():
    result = evaluate_direct_programs(
        _facts(naics_code="722511", taxonomy_status="pending")
    )
    assert result["canonical_naics_code"] is None
    assert _program(result, TERM_PROGRAM_KEY)["status"] == "potential"
    assert _program(result, WORKING_CAPITAL_PROGRAM_KEY)["status"] == "potential"
    assert not _program(result, WORKING_CAPITAL_PROGRAM_KEY)["matched_rules"]


def test_formal_application_and_product_finder_use_same_engine():
    facts = _facts(naics_code="722511")
    owner = facts.pop("owners")[0]
    direct = evaluate_direct_programs({**facts, "owners": [owner]})
    finder = screen_products({
        **facts,
        "primary_owner_credit_660_or_higher": True,
        "residency_status": "citizen",
        "bankruptcy_timing": "none",
        "foreclosure_within_3_years": False,
        "felony_timing": "none",
        "misdemeanor_within_5_years": False,
        "misdemeanor_involving_minor": False,
        "arrest_within_6_months": False,
        "financial_related_crime": False,
        "active_legal_charges": False,
        "ofac_match": False,
    })
    formal = application_prescreen.screen_application(
        requested_amount=facts["requested_amount"],
        refinance_debt=False,
        required_owner_ids=["owner-1"],
        owner_answers={"owner-1": owner},
        file_answers={
            "refinance_debt": False,
            "open_tax_liens": False,
            "tax_liability_over_10000": False,
            "open_judgments": False,
            "open_civil_actions_as_defendant": False,
            "civil_action_financial_institution_within_10_years": False,
            "judgment_over_2000_within_12_months": False,
            "judgment_over_50000_within_7_years": False,
            "aggregate_liens_judgments_over_25000_within_7_years": False,
            "speculative_real_estate_flipping": False,
            "gambling_or_bail_bonds": False,
            "lending_investment_crypto_mlm": False,
            "nonprofit_or_government": False,
            "marijuana_or_firearms": False,
            "prurient_business": False,
            "auto_or_title_asset_sales": False,
        },
        application_facts=facts,
    )
    for key in (TERM_PROGRAM_KEY, WORKING_CAPITAL_PROGRAM_KEY):
        assert _program(direct, key)["status"] == _program(finder, key)["status"]
        assert _program(direct, key)["status"] == _program(formal, key)["status"]
        assert _rule_ids(direct, key) == _rule_ids(finder, key)
        assert _rule_ids(direct, key) == _rule_ids(formal, key)
