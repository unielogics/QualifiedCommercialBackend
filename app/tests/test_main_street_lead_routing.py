"""An operating-business lead must not be screened as a dealer file.

The admin "create AI underwriter lead" flow offered two variants, dealer and
real estate, and everything that was not real estate fell into the dealer
branch. A restaurant or a machine shop created there received the dealer
document checklist, dealer AI framing, and a fundability verdict measured
against dealer programs — floorplan, reinsurance-backed, jumbo DSCR — none of
which it could ever qualify for.

These tests pin the routing, not the arithmetic: which screen runs for which
variant, and that the labels rendered alongside a screen belong to it. The
thresholds themselves are covered in test_main_street_programs.py.

No DB here. The router functions under test read plain attributes off the
intake, so a stub carries exactly the fields they touch and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers.dealer_ai_intake import (
    DEALER_VARIANT,
    MAIN_STREET_VARIANT,
    PROGRAM_LABELS,
    _apply_loan_program_fit,
    _program_labels_for,
)
from app.services.main_street_programs import MAIN_STREET_PROGRAM_LABELS

# Keys that exist ONLY in the dealer catalogue. If one appears on an operating
# business, the wrong screen ran.
#
# Deliberately narrow: real_estate_backed, merchant_processing and
# transportation_factoring are in both catalogues as internal-only signals, and
# the dealer's jumbo_dscr is a different key from Main Street's jumbo_term_loan.
# Asserting against the overlap would fail on a correct screen.
DEALER_ONLY = {"reinsurance_backed", "jumbo_dscr", "floorplan"}


def _bucket(*documents):
    """A bucket with no files against it — both screens walk these collections,
    so they have to exist even when empty."""
    return SimpleNamespace(
        files=[],
        requested_documents=[
            SimpleNamespace(id=i, name=name, status=status)
            for i, (name, status) in enumerate(documents)
        ],
    )


def _intake(variant: str, documents=(), **state):
    """A real (unpersisted) intake row.

    The dealer screen reads a wide surface — asset rows, owner declarations,
    document state — and a hand-rolled stub turns into a game of adding whichever
    attribute the next line happens to touch. Constructing the model gives every
    column its real default for free; only the relationships need filling in,
    since nothing is attached to a session.
    """
    intake = PublicUnderwritingIntake(
        variant=variant,
        requested_loan_amount=120_000,
    )
    intake.bucket = _bucket(*documents)
    # _key_metrics reads the latest review first, then this snapshot.
    intake.latest_review = None
    intake.result_snapshot = {
            "key_metrics": {
                "ytd_annualized_revenue": 800_000,
                "estimated_dscr": 1.6,
                "estimated_ebitda_or_cash_flow": 150_000,
                "bank_statement_months": 6,
                "tax_return_years": 2,
                "nsf_or_overdraft_count": 0,
            }
        }
    intake.intake_state = {
        "messages": [],
        "dealer_details": {
            "years_in_business": 6,
            "estimated_credit_score": 720,
        },
        "main_street_details": {"intent": "working_capital", "industry": "manufacturing"},
        **state,
    }
    return intake


def test_main_street_lead_is_screened_against_main_street_programs():
    intake = _intake(MAIN_STREET_VARIANT)
    _apply_loan_program_fit(intake)
    fit = intake.intake_state["loan_program_fit"]

    assert fit, "an operating business must still get a screen"
    assert not (set(fit) & DEALER_ONLY), (
        f"dealer-only programs leaked onto an operating business: {set(fit) & DEALER_ONLY}"
    )
    # It should be screening the catalogue this business can actually reach.
    assert "term_loan_3_5_year" in fit
    assert "line_of_credit" in fit


def test_dealer_lead_still_gets_the_dealer_screen():
    """The fix must not cost the path it was carved out of."""
    intake = _intake(DEALER_VARIANT)
    _apply_loan_program_fit(intake)
    fit = intake.intake_state["loan_program_fit"]

    assert fit
    assert set(fit) & DEALER_ONLY, "dealer programs disappeared from a dealer file"


def test_labels_match_whichever_screen_ran():
    """Mismatched labels render raw keys to an operator, which reads as a bug
    even when the screen underneath was right."""
    assert _program_labels_for(_intake(MAIN_STREET_VARIANT)) is MAIN_STREET_PROGRAM_LABELS
    assert _program_labels_for(_intake(DEALER_VARIANT)) is PROGRAM_LABELS


def test_every_main_street_program_screened_has_a_label():
    intake = _intake(MAIN_STREET_VARIANT)
    _apply_loan_program_fit(intake)
    labels = _program_labels_for(intake)
    unlabelled = [
        k
        for k, row in intake.intake_state["loan_program_fit"].items()
        if isinstance(row, dict) and k not in labels
    ]
    # Internal-only signals are deliberately unlabelled; borrower-facing ones
    # must never be.
    from app.services.main_street_programs import BORROWER_SUGGESTABLE_PROGRAMS

    assert not (set(unlabelled) & BORROWER_SUGGESTABLE_PROGRAMS)


def test_a_non_lending_intent_is_not_measured_as_a_loan_file():
    """Someone asking about point-of-sale software has no programs to fail."""
    intake = _intake(
        MAIN_STREET_VARIANT,
        main_street_details={"intent": "business_systems", "industry": "retail_ecommerce"},
    )
    _apply_loan_program_fit(intake)
    fit = intake.intake_state["loan_program_fit"]
    eligible = [k for k, r in fit.items() if isinstance(r, dict) and r.get("eligible")]
    assert not eligible, f"a non-lending enquiry was offered programs: {eligible}"


def test_industry_and_intent_actually_reach_the_screen():
    """Trucking is excluded from the 10-year band. If the industry were being
    dropped on the way in, this would pass eligible and nobody would notice."""
    trucking = _intake(
        MAIN_STREET_VARIANT,
        main_street_details={"intent": "working_capital", "industry": "trucking_logistics"},
    )
    _apply_loan_program_fit(trucking)
    row = trucking.intake_state["loan_program_fit"]["term_loan_10_year"]
    assert row["eligible"] is False
    assert any("sector" in b for b in row["blocked_by"])
