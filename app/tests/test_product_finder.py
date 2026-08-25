from app.dealer_os.services.product_finder import screen_products


def complete_answers(**overrides):
    values = {
        "requested_amount": 500_000,
        "use_of_funds": "Working capital and debt refinance",
        "industry": "54",
        "industry_label": "Professional, Scientific, and Technical Services",
        "subindustry": "541",
        "subindustry_label": "Professional, Scientific, and Technical Services",
        "naics_code": "541611",
        "naics_label": "Administrative Management and General Management Consulting Services",
        "taxonomy_status": "official",
        "years_in_business": 4,
        "annual_revenue": 1_200_000,
        "primary_owner_credit_660_or_higher": True,
        "owner_count": 2,
        "mca_count": 1,
        "youngest_mca_days": 120,
        "positive_month_end_count": 3,
        "nsf_count": 1,
        "negative_balance_days": 2,
        "business_dscr": 1.25,
        "citizen_or_lpr": True,
        "bankruptcy_timing": "none",
        "foreclosure_within_3_years": False,
        "felony_timing": "none",
        "misdemeanor_5y": False,
        "open_tax_liens_or_judgments": False,
        "ofac_match": False,
        "active_legal_charges": False,
        "active_ucc_count": 2,
        "official_bank_statements": True,
        "debt_refinance": False,
        "real_estate_involved": False,
    }
    values.update(overrides)
    return values


def by_key(result, key):
    return next(row for row in result["evaluated_programs"] if row["program_key"] == key)


def test_exact_quidity_thresholds_are_inclusive():
    result = screen_products(complete_answers(requested_amount=50_000, years_in_business=2, annual_revenue=50_000, primary_owner_credit_660_or_higher=True, business_dscr=1.1))
    assert by_key(result, "term_loan_3_5_year")["status"] == "recommended"
    assert by_key(result, "term_loan_10_year")["status"] == "recommended"


def test_one_program_block_does_not_stop_alternatives():
    result = screen_products(complete_answers(requested_amount=50_000, mca_count=2))
    assert by_key(result, "term_loan_3_5_year")["status"] == "blocked"
    assert by_key(result, "term_loan_10_year")["status"] == "recommended"


def test_request_is_preserved_when_no_direct_program_supports_it():
    result = screen_products(complete_answers(mca_count=2, requested_amount=500_000))
    assert result["client_requested_amount"] == 500_000
    assert result["recommended_amount"] is None
    assert result["amount_adjustment_required"] is False
    assert by_key(result, "term_loan_3_5_year")["status"] == "blocked"
    assert by_key(result, "term_loan_10_year")["status"] == "blocked"


def test_industry_gate_is_program_specific_and_safe():
    result = screen_products(complete_answers(
        requested_amount=50_000,
        industry="72",
        industry_label="Accommodation and Food Services",
        subindustry="722",
        subindustry_label="Food Services and Drinking Places",
        naics_code="722511",
        naics_label="Full-Service Restaurants",
    ))
    assert by_key(result, "term_loan_10_year")["status"] == "blocked"
    assert by_key(result, "term_loan_3_5_year")["status"] == "recommended"
    assert all("NAICS" not in reason for reason in by_key(result, "term_loan_10_year")["borrower_safe_reasons"])


def test_pending_custom_taxonomy_does_not_create_a_deterministic_exclusion():
    result = screen_products(complete_answers(
        requested_amount=50_000,
        taxonomy_status="pending",
        naics_code="722511",
        naics_label="Pending custom restaurant activity",
    ))
    assert by_key(result, "term_loan_10_year")["status"] == "recommended"
    assert result["canonical_naics_code"] is None


def test_real_estate_rows_calculate_unverified_equity_and_ltv():
    result = screen_products(complete_answers(
        real_estate_involved=True,
        real_estate_purpose="cash_out",
        owned_real_estate_available=True,
        properties=[{
            "address": "100 Main St, Newark, NJ 07102",
            "property_type": "commercial",
            "estimated_value": 1_000_000,
            "amount_owed": 400_000,
        }],
    ))
    analysis = result["real_estate_analysis"]
    assert analysis["total_stated_equity"] == 600_000
    assert analysis["portfolio_ltv"] == 0.4
    assert analysis["verification"] == "Self-reported and unverified"


def test_debt_refinance_blocks_microcap_but_not_ez_term():
    result = screen_products(complete_answers(requested_amount=50_000, debt_refinance=True))
    assert by_key(result, "term_loan_10_year")["status"] == "blocked"
    assert by_key(result, "term_loan_3_5_year")["status"] == "recommended"


def test_missing_answers_return_one_progressive_question():
    result = screen_products({"requested_amount": 100_000})
    assert result["next_question"]["key"] == "use_of_funds"
    assert by_key(result, "term_loan_3_5_year")["status"] == "potential"


def test_spanish_result_uses_localized_borrower_copy():
    result = screen_products(complete_answers(years_in_business=1), "es")
    reasons = by_key(result, "term_loan_3_5_year")["borrower_safe_reasons"]
    assert any("requiere" in reason.lower() for reason in reasons)
