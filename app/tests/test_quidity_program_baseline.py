"""The Quidity Direct 10-year term loan, as the fit engine reads it.

The baseline is a transcription of a lender's guideline. What these tests pin
is that the transcription is the engine's dialect (every rule validates under
the strict field check, every document row validates as a requirement) and
that the eligibility arithmetic lands where the guide says: a file with the
tangible evidence passes, a missing filed return is "unknown" rather than a
failure, the thresholds bite at the guide's numbers, and a missing credit
score never decides anything — the soft pull is the last step by design.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.funding_program import (
    FundingProgramCreate,
    FundingProgramRequirementWrite,
    FundingProgramScopeWrite,
)
from app.scripts import onboard_program_from_baseline as script
from app.services import funding_program_drafts
from app.services.funding_program_baselines import CATALOG, QUIDITY_SOURCE, suggested_baseline
from app.services.program_rules import (
    evaluate_recommendation_preferences,
    evaluate_rules,
    validate_rules,
)

KEY = "quidity_10_year_term"


def eligible_context(**over) -> dict:
    """A main-street file holding exactly what the guide asks for."""
    context = {
        "vertical": "main_street",
        "industry_key": "manufacturing",
        "business_age_years": 6,
        "annual_revenue": 420000.0,
        "revenue": 420000.0,
        "tax_returns_available": True,
        "cash_flow": 38000.0,
        "bank_statement_months": 6,
        "nsf_or_overdraft_count": 2,
        "requested_amount": 150000.0,
        "credit_score": None,
        "intent": "working_capital",
        "mca_obligations_present": False,
        "dscr": None,
    }
    context.update(over)
    return context


@pytest.fixture(scope="module")
def baseline() -> dict:
    return suggested_baseline(KEY)


# ── the transcription is the engine's dialect ───────────────────────────────


def test_rules_validate_under_the_strict_field_check(baseline):
    validate_rules(baseline["rules"], enforce_supported_fields=True)
    assert baseline["rules"]["fit"]["all"], "the guide has hard minimums; the fit block must not be empty"


def test_every_document_row_is_a_valid_requirement(baseline):
    rows = [FundingProgramRequirementWrite(**row) for row in baseline["requirements"]]
    by_key = {row.requirement_key: row for row in rows}
    assert {
        "business_tax_returns_1_years",
        "business_bank_statements_6_months",
        "business_debt_schedule",
        "use_of_proceeds",
        "quidity_sizing_pathways",
        "quidity_eligibility_review",
        "guarantor_credit_review",
        "ytd_p_and_l_balance_sheet",
    } <= set(by_key)
    # The three submission items the guide names are required at underwriting.
    for key in ("business_tax_returns_1_years", "business_bank_statements_6_months", "business_debt_schedule"):
        assert (by_key[key].required_level, by_key[key].blocks_stage) == ("required", "underwriting")
    # Credit comes last: it blocks closing, not the stage where the screen says "fits".
    assert by_key["guarantor_credit_review"].blocks_stage == "closing"
    assert by_key["guarantor_credit_review"].category == "credit"
    # The P&L is helpful, not demanded by the guide.
    assert by_key["ytd_p_and_l_balance_sheet"].required_level == "recommended"
    # Sizing is a staff calculation, written out for the AI.
    assert by_key["quidity_sizing_pathways"].requirement_kind == "human_review"
    assert "40% of annual tax-return revenue" in (by_key["quidity_sizing_pathways"].completion_criteria or "")


def test_scopes_cover_main_street_and_mca_and_exclude_real_estate(baseline):
    scopes = [FundingProgramScopeWrite(**scope) for scope in baseline["scopes"]]
    assert {scope.vertical for scope in scopes} == {"main_street", "mca"}
    for scope in scopes:
        assert {"531", "2372"} <= set(scope.excluded_naics_prefixes)


def test_the_catalog_row_payload_validates():
    spec = CATALOG[KEY]
    baseline = suggested_baseline(KEY)
    FundingProgramCreate(
        program_key=KEY,
        public_slug=spec["public_slug"],
        name=spec["name"],
        short_description=spec["short_description"],
        scopes=[FundingProgramScopeWrite(**scope) for scope in baseline["scopes"]],
        confirmed=True,
    )


def test_the_baseline_carries_its_own_source_note_and_description(baseline):
    assert baseline["source_urls"][0] == QUIDITY_SOURCE
    assert baseline["source_notes"][0].startswith("Lender guideline: Quidity Direct")
    assert "Website: source snapshot" not in baseline["source_notes"][0]
    assert baseline["description"].startswith("Quidity Direct 10 Year Term Loan")
    assert len(baseline["description"]) <= 4000


def test_a_draft_made_from_the_baseline_carries_the_programs_own_words():
    program = SimpleNamespace(id=None, program_key=KEY, name=CATALOG[KEY]["name"], short_description=None)
    payload, source, error = funding_program_drafts.build_review_draft_payload(
        program, legacy_draft=None, scopes=[]
    )
    assert error is None and source == "baseline"
    assert payload is not None and payload.description.startswith("Quidity Direct 10 Year Term Loan")


# ── the arithmetic lands where the guide says ───────────────────────────────


def test_a_file_with_the_tangible_evidence_passes_without_a_credit_score(baseline):
    result = evaluate_rules(baseline["rules"], eligible_context(credit_score=None))
    assert result.state == "pass" and result.matched


def test_a_missing_filed_return_is_unknown_not_a_failure(baseline):
    context = eligible_context(tax_returns_available=False, annual_revenue=None, revenue=None, cash_flow=None)
    assert evaluate_rules(baseline["rules"], context).state == "unknown"


@pytest.mark.parametrize(
    "over",
    [
        {"annual_revenue": 150000.0, "revenue": 150000.0},   # below $175,000
        {"cash_flow": -1200.0},                               # EBITDA not positive
        {"business_age_years": 1.5},                          # under two years
        {"industry_key": "construction_trades", "business_age_years": 3},  # construction needs four
        {"industry_key": "Construction", "business_age_years": 3},         # the NAICS sector label too
        {"nsf_or_overdraft_count": 6},                        # more than five NSF items
        {"requested_amount": 400000.0},                       # above $350,000
        {"requested_amount": 25000.0},                        # below $50,000
    ],
)
def test_the_thresholds_bite_at_the_guides_numbers(baseline, over):
    assert evaluate_rules(baseline["rules"], eligible_context(**over)).state == "fail"


@pytest.mark.parametrize(
    "over",
    [
        {"industry_key": "construction_trades", "business_age_years": 4},
        {"industry_key": "Health Care and Social Assistance", "business_age_years": 3},  # a sector label that is not construction
        {"industry_key": None, "business_age_years": 4},   # industry unknown, but old enough either way
        {"requested_amount": 350000.0},
        {"requested_amount": 50000.0},
    ],
)
def test_the_boundaries_pass(baseline, over):
    assert evaluate_rules(baseline["rules"], eligible_context(**over)).state == "pass"


@pytest.mark.parametrize(
    "over",
    [
        {"nsf_or_overdraft_count": None},     # statements not analysed yet
        {"requested_amount": None},           # amount not stated yet
        {"business_age_years": None},
        {"cash_flow": None},
        {"industry_key": None, "business_age_years": 3},   # could be construction, could not
    ],
)
def test_a_fact_not_known_yet_is_a_gap_never_a_failure(baseline, over):
    # This is what lets the screen say "fits once we have X" while documents
    # are still arriving, instead of marking the file not eligible.
    assert evaluate_rules(baseline["rules"], eligible_context(**over)).state == "unknown"


def test_document_counts_are_not_fit_gates(baseline):
    # A count of zero reads as a failure in this engine, so a file that has not
    # uploaded its return or statements yet would be "not eligible" rather than
    # "needs information". Those documents are requirement rows instead.
    text = str(baseline["rules"]["fit"])
    for field in ("bank_statement_months", "tax_return_years", "tax_returns_available", "evidence_available"):
        assert field not in text


def test_no_rule_reads_the_credit_score(baseline):
    text = str(baseline["rules"]["fit"])
    assert "credit_score" not in text and "estimated_credit_score" not in text


def test_preferences_rank_mca_payoff_refinance_intent_and_the_dscr_pathway(baseline):
    rules = baseline["rules"]
    score, reasons = evaluate_recommendation_preferences(
        rules, eligible_context(mca_obligations_present=True, intent="refinance_debt", dscr=1.25)
    )
    assert score == 65 and len(reasons) == 3
    score, _ = evaluate_recommendation_preferences(rules, eligible_context())
    assert score == 0


# ── the onboarding script ───────────────────────────────────────────────────


def test_the_script_refuses_an_unknown_program_key(capsys):
    assert script.main(["--program-key", "no_such_program", "--user-email", "x@y.z", "--dry-run"]) == 2
    assert "No baseline spec" in capsys.readouterr().err


async def test_the_screen_counts_states_and_names_the_missing_facts(baseline, monkeypatch):
    profiles = [SimpleNamespace(id=i) for i in range(3)]
    contexts = [
        eligible_context(),
        eligible_context(annual_revenue=100000.0, revenue=100000.0),
        eligible_context(tax_returns_available=False, annual_revenue=None, revenue=None, cash_flow=None),
    ]

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: profiles)

    class _DB:
        async def execute(self, _stmt):
            return _Result()

    async def fake_context(_db, profile):
        return contexts[profile.id]

    monkeypatch.setattr(script.application_programs, "profile_fit_context", fake_context)
    report = await script.screen(_DB(), baseline["rules"], ["main_street"])
    assert report["files"] == 3
    assert report["states"] == {"pass": 1, "fail": 1, "unknown": 1}
    assert any(field in {"annual_revenue", "tax_returns_available", "cash_flow"} for field, _ in report["unknown_fields"])
    assert report["fail_reasons"]
