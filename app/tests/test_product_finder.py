from app.dealer_os.services.product_finder import screen_products


def complete_answers(**overrides):
    values = {
        "requested_amount": 500_000,
        "use_of_funds": "Working capital and debt refinance",
        "industry": "professional services",
        "years_in_business": 4,
        "annual_revenue": 1_200_000,
        "credit_tier": 700,
        "owner_count": 2,
        "mca_count": 1,
        "youngest_mca_days": 120,
        "positive_month_end_count": 3,
        "nsf_count": 1,
        "negative_balance_days": 2,
        "business_dscr": 1.25,
        "citizen_or_lpr": True,
        "bankruptcy_7y": False,
        "bankruptcy_or_foreclosure_3y": False,
        "felony_10y": False,
        "misdemeanor_5y": False,
        "open_tax_liens_or_judgments": False,
        "ofac_match": False,
        "active_legal_charges": False,
        "any_felony": False,
        "active_ucc_count": 2,
        "loan_to_revenue_pct": 40,
        "official_bank_statements": True,
    }
    values.update(overrides)
    return values


def by_key(result, key):
    return next(row for row in result["evaluated_programs"] if row["program_key"] == key)


def test_exact_quidity_thresholds_are_inclusive():
    result = screen_products(complete_answers(years_in_business=2, annual_revenue=50_000, credit_tier=660, business_dscr=1.1))
    assert by_key(result, "term_loan_3_5_year")["status"] == "recommended"
    assert by_key(result, "term_loan_10_year")["status"] == "recommended"


def test_one_program_block_does_not_stop_alternatives():
    result = screen_products(complete_answers(mca_count=2))
    assert by_key(result, "term_loan_3_5_year")["status"] == "blocked"
    assert by_key(result, "term_loan_10_year")["status"] == "recommended"


def test_request_is_preserved_when_screening_recommends_less():
    result = screen_products(complete_answers(mca_count=2, requested_amount=500_000))
    assert result["client_requested_amount"] == 500_000
    assert result["recommended_amount"] == 50_000
    assert result["amount_adjustment_required"] is True


def test_industry_gate_is_program_specific_and_safe():
    result = screen_products(complete_answers(industry="restaurant / food service"))
    assert by_key(result, "term_loan_10_year")["status"] == "blocked"
    assert by_key(result, "term_loan_3_5_year")["status"] == "recommended"
    assert all("NAICS" not in reason for reason in by_key(result, "term_loan_10_year")["borrower_safe_reasons"])


def test_missing_answers_return_one_progressive_question():
    result = screen_products({"requested_amount": 100_000})
    assert result["next_question"]["key"] == "use_of_funds"
    assert by_key(result, "term_loan_3_5_year")["status"] == "potential"


def test_spanish_result_uses_localized_borrower_copy():
    result = screen_products(complete_answers(years_in_business=1), "es")
    reasons = by_key(result, "term_loan_3_5_year")["borrower_safe_reasons"]
    assert any("requiere" in reason.lower() for reason in reasons)
