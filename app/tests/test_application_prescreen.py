from app.dealer_os.services.application_prescreen import (
    applicable_business_questions,
    business_answer_blockers,
    owner_answer_complete,
    screen_application,
)


def answer(**overrides):
    data = {
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
    }
    data.update(overrides)
    return data


def screen(amount: float, *, refinance: bool = False, owner=None):
    return screen_application(
        requested_amount=amount,
        refinance_debt=refinance,
        required_owner_ids=["owner-1"],
        owner_answers={"owner-1": owner or answer()},
    )


def eligible(result):
    return set(result["eligible_program_keys"])


def test_owner_answer_requires_every_question():
    assert owner_answer_complete(answer())
    assert not owner_answer_complete({"residency_status": "citizen"})


def test_amount_boundaries_route_the_two_direct_programs():
    assert eligible(screen(14_999)) == set()
    assert eligible(screen(15_000)) == {"term_loan_10_year"}
    assert eligible(screen(24_999.99)) == {"term_loan_10_year"}
    assert eligible(screen(25_000)) == {"term_loan_3_5_year", "term_loan_10_year"}
    assert eligible(screen(50_000)) == {"term_loan_3_5_year", "term_loan_10_year"}
    assert eligible(screen(50_000.01)) == {"term_loan_3_5_year"}
    assert eligible(screen(500_000)) == {"term_loan_3_5_year"}
    assert eligible(screen(500_000.01)) == set()


def test_refinance_routes_only_to_ez_term():
    assert eligible(screen(40_000, refinance=True)) == {"term_loan_3_5_year"}


def test_owner_exclusions_apply_per_program():
    assert eligible(screen(40_000, owner=answer(bankruptcy_timing="within_3_years"))) == set()
    assert eligible(screen(40_000, owner=answer(bankruptcy_timing="4_to_7_years"))) == {"term_loan_10_year"}
    assert eligible(screen(40_000, owner=answer(felony_timing="more_than_10_years"))) == {"term_loan_3_5_year"}
    assert eligible(screen(40_000, owner=answer(foreclosure_within_3_years=True))) == {"term_loan_3_5_year"}
    assert eligible(screen(40_000, owner=answer(residency_status="other"))) == set()
    assert eligible(screen(40_000, owner=answer(credit_660_or_higher=False))) == set()


def test_step_four_business_questions_enforce_only_visible_followups():
    groups = applicable_business_questions(naics_code="541611", routing_result=None)
    answers = {
        question["key"]: False
        for group in groups
        for question in group["questions"]
        if not question.get("show_when") and not question.get("show_when_any")
    }
    assert business_answer_blockers(groups, answers) == []

    answers["tax_liability_over_10000"] = True
    assert business_answer_blockers(groups, answers) == [
        "Is the disclosed tax balance on a current payment plan?"
    ]
    answers["tax_payment_plan_current"] = True
    assert business_answer_blockers(groups, answers) == []
