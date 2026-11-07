"""What the AI fills in on its own so a person only corrects.

Body Good Studio had six accepted statements and still read "needs
information" for revenue, cash flow, NSF count, years in business and
industry, so no program could be pinned.
"""

from types import SimpleNamespace

from app.routers.dealer_ai_intake import McaRefiQualification, McaRefiStart, _merge_mca_details
from app.services.application_programs import (
    apply_statement_estimates,
    estimate_annualized_deposits,
    estimate_cash_flow_from_statements,
)
from app.services.industry_suggestion import rank_candidates, tokens

ROWS = [
    {"sort": "2026-05", "deposits": 58687.07, "withdrawals": 50000.0},
    {"sort": "2026-06", "deposits": 77944.77, "withdrawals": 60000.0},
    {"sort": "2026-07", "deposits": 47536.53, "withdrawals": 50000.0},
    {"sort": "2026-08", "deposits": 55716.38, "withdrawals": 40000.0},
]


def test_statements_estimate_deposits_and_cash_flow_from_three_months_or_more() -> None:
    assert estimate_annualized_deposits(ROWS) == round(sum(r["deposits"] for r in ROWS) / 4 * 12, 2)
    assert estimate_cash_flow_from_statements(ROWS) == round(sum(r["deposits"] - r["withdrawals"] for r in ROWS) / 4 * 12, 2)
    assert estimate_annualized_deposits(ROWS[:2]) is None
    assert estimate_cash_flow_from_statements([{"deposits": 1.0}] * 5) is None


def test_estimates_fill_revenue_and_cash_flow_only_when_nothing_better_exists() -> None:
    context = {"annualized_deposits": 690395.0, "annual_revenue": None, "revenue": None, "cash_flow": None}
    sources = {}
    six_rows = [
        {"sort": "2026-03", "deposits": 50000.0, "withdrawals": 40000.0},
        {"sort": "2026-04", "deposits": 60000.0, "withdrawals": 50000.0},
        *ROWS,
    ]
    estimated = apply_statement_estimates(context, sources, accepted_months=6, bank_rows=six_rows)
    assert context["annual_revenue"] == 690395.0 and context["revenue"] == 690395.0
    assert sources["annual_revenue"]["verification_status"] == "estimated_from_deposits"
    assert context["cash_flow"] == estimate_cash_flow_from_statements(six_rows)
    assert sources["cash_flow"]["verification_status"] == "estimated_from_statements"
    assert set(estimated) == {"annual_revenue", "revenue", "cash_flow"}

    reviewed = {"annualized_deposits": 690395.0, "annual_revenue": 500000.0, "revenue": 500000.0, "cash_flow": 80000.0}
    assert apply_statement_estimates(reviewed, {}, accepted_months=6, bank_rows=ROWS) == []
    assert reviewed["annual_revenue"] == 500000.0

    short = {"annualized_deposits": None, "annual_revenue": None, "revenue": None, "cash_flow": None}
    apply_statement_estimates(short, {}, accepted_months=4, bank_rows=ROWS)
    assert short["annualized_deposits"] == estimate_annualized_deposits(ROWS)
    assert short["annual_revenue"] is None  # six months of statements before deposits stand in for revenue


def test_candidates_are_the_catalog_activities_whose_words_the_description_shares() -> None:
    entries = [
        SimpleNamespace(code="812199", label="Other Personal Care Services", aliases=["wellness studio", "spa"]),
        SimpleNamespace(code="713940", label="Fitness and Recreational Sports Centers", aliases=["gym", "fitness studio"]),
        SimpleNamespace(code="236115", label="New Single-Family Housing Construction", aliases=[]),
    ]
    ranked = rank_candidates("health/wellness studio with online membership platform", entries)
    assert [entry.code for entry in ranked][:1] == ["812199"]
    assert "236115" not in [entry.code for entry in ranked]
    assert tokens("Health & Wellness Studio, LLC") == {"health", "wellness"}
    assert rank_candidates("", entries) == []


def test_years_in_business_is_asked_at_the_start_and_accepted_from_the_chat() -> None:
    assert McaRefiStart.model_fields["years_in_business"].default is None
    assert McaRefiQualification(years_in_business=3.5, manual_review_requested=True).years_in_business == 3.5
    intake = SimpleNamespace(intake_state={})
    _merge_mca_details(intake, {"years_in_business": 4, "advance_count": 2, "bogus": "x"})
    assert intake.intake_state["mca_details"]["years_in_business"] == 4.0
    assert intake.intake_state["mca_details"]["advance_count"] == 2
    assert "bogus" not in intake.intake_state["mca_details"]
    _merge_mca_details(intake, {"years_in_business": 900})
    assert intake.intake_state["mca_details"]["years_in_business"] == 4.0


def test_rules_evaluate_provisional_and_estimated_facts_and_say_so() -> None:
    from app.services.application_programs import estimate_notes, evaluation_values

    context = {"annual_revenue": 690395.0, "cash_flow": -29364.3, "nsf_or_overdraft_count": 0.0, "business_age_years": 3.0, "credit_score": 700, "dscr": 1.4, "vertical": "mca"}
    sources = {
        "annual_revenue": {"verification_status": "estimated_from_deposits"},
        "cash_flow": {"verification_status": "estimated_from_statements"},
        "nsf_or_overdraft_count": {"verification_status": "provisional"},
        "business_age_years": {"verification_status": "provisional"},
        "credit_score": {"verification_status": "self_reported_unverified"},
        "dscr": {"verification_status": "conflicting"},
    }
    values = evaluation_values(context, sources)
    assert values["annual_revenue"] == 690395.0 and values["cash_flow"] == -29364.3
    assert values["nsf_or_overdraft_count"] == 0.0 and values["business_age_years"] == 3.0
    assert values["credit_score"] is None and values["dscr"] is None
    assert values["vertical"] == "mca"
    assert estimate_notes(sources, {"annual_revenue", "cash_flow", "business_age_years"}) == [
        "Annual revenue estimated from bank deposits; a person can replace it",
        "Cash flow estimated from bank statements; a person can replace it",
    ]


def test_the_desk_can_record_years_in_business_and_the_file_reads_it_back() -> None:
    from app.routers.dealer_ai_intake import AdminLeadContactUpdate, _years_in_business

    assert AdminLeadContactUpdate(years_in_business=3.5).years_in_business == 3.5
    desk = SimpleNamespace(intake_state={"desk_facts": {"years_in_business": 4.0}, "mca_qualification": {"years_in_business": 2}}, result_snapshot={})
    assert _years_in_business(desk) == 4.0  # the desk's entry wins over the declaration
    declared = SimpleNamespace(intake_state={"mca_qualification": {"years_in_business": "2.5"}}, result_snapshot={})
    assert _years_in_business(declared) == 2.5
    chat = SimpleNamespace(intake_state={"mca_details": {"years_in_business": 6, "advance_count": 2}}, result_snapshot={})
    assert _years_in_business(chat) == 6.0
    assert _years_in_business(SimpleNamespace(intake_state={}, result_snapshot=None)) is None
