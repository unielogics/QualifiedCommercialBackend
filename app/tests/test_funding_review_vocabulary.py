"""Write models fold real-estate answers; the chat merge never stores garbage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.routers.dealer_ai_intake as core
from app.schemas.client_application import ClientApplicationPatch, ClientApplicationRead
from app.services.business_profile import BusinessProfileFields, RequiredBusinessProfileFields
from app.tests._business_profile_fixture import APPLICATION_START_DETAILS, BUSINESS_PROFILE

ANSWERS = {**BUSINESS_PROFILE, **APPLICATION_START_DETAILS}

START = {
    "full_name": "Stephen Bronowski",
    "email": "stephen@example.com",
    "phone": "7045550123",
    **ANSWERS,
    "terms_accepted": True,
    "privacy_accepted": True,
}


def test_the_start_form_folds_legacy_labels_and_new_purposes() -> None:
    start = core.FundingReviewStart(**START, transaction_type="refi", estimated_credit_tier="good")
    assert start.transaction_type == "Refinance"
    assert start.estimated_credit_tier == "Strong Credit"
    heloc = core.FundingReviewStart(
        **START,
        transaction_type="Home equity (HELOC)",
        occupancy="Primary residence",
        business_purpose=True,
        purpose_description="  Working capital   for the dealership ",
    )
    assert heloc.transaction_type == "Home equity (HELOC)"
    assert heloc.occupancy == "primary_residence"
    assert heloc.business_purpose is True
    assert heloc.purpose_description == "Working capital for the dealership"


def test_the_start_form_refuses_values_that_name_nothing() -> None:
    with pytest.raises(ValidationError, match="Choose a transaction type"):
        core.FundingReviewStart(**START, transaction_type="whatever")
    with pytest.raises(ValidationError, match="credit score between 300 and 850"):
        core.FundingReviewStart(**START, estimated_credit_tier="XXX")
    with pytest.raises(ValidationError, match="Choose who uses the property"):
        core.FundingReviewStart(**START, occupancy="somewhere")


def test_an_old_cached_page_still_starts_a_file_without_the_new_questions() -> None:
    start = core.FundingReviewStart(**START, transaction_type="Purchase", estimated_credit_tier="Not sure yet")
    assert start.occupancy is None
    assert start.business_purpose is None


def test_business_phone_is_checked_on_writes_and_returned_as_stored_on_reads() -> None:
    with pytest.raises(ValidationError, match="does not look complete"):
        RequiredBusinessProfileFields(**{**ANSWERS, "business_phone": "Stephen Bronowski"})
    assert RequiredBusinessProfileFields(**{**ANSWERS, "business_phone": "(704) 555-0199"}).business_phone == "+17045550199"
    with pytest.raises(ValidationError):
        core.AdminLeadContactUpdate(business_phone="Stephen Bronowski")
    with pytest.raises(ValidationError):
        core.AdminLeadCreate(
            full_name="Owner",
            email="owner@example.com",
            phone="7045550123",
            secure_room_pin="123456",
            application_completion="client",
            business_phone="Stephen Bronowski",
        )
    # The shared optional shape (used for reads and older files) is untouched.
    assert BusinessProfileFields(business_phone="Stephen Bronowski").business_phone == "Stephen Bronowski"


def test_staff_create_folds_transaction_and_credit() -> None:
    lead = core.AdminLeadCreate(
        full_name="Owner",
        email="owner@example.com",
        phone="7045550123",
        secure_room_pin="123456",
        application_completion="client",
        variant="real_estate",
        transaction_type="cash out",
        estimated_credit_tier="e.g. 700+",
    )
    assert lead.transaction_type == "Cash-out refinance"
    assert lead.estimated_credit_tier == "700+"


def test_the_room_application_folds_on_write_and_reads_as_stored() -> None:
    patch = ClientApplicationPatch(transaction_type="buying", passcode="123456")
    assert patch.transaction_type == "Purchase"
    with pytest.raises(ValidationError):
        ClientApplicationPatch(transaction_type="whatever", passcode="123456")
    read = ClientApplicationRead(vertical="real_estate", transaction_type="whatever an old file says")
    assert read.transaction_type == "whatever an old file says"


def _intake(**state) -> SimpleNamespace:
    return SimpleNamespace(
        id="intake-1",
        variant=core.FUNDING_VARIANT,
        loan_purpose=state.pop("loan_purpose", None),
        requested_loan_amount=None,
        intake_state=state,
        current_requested_loan_amount=None,
        latest_review=None,
        result_snapshot=None,
        bucket=None,
    )


def test_the_chat_merge_routes_an_occupancy_out_of_property_type_and_drops_garbage() -> None:
    intake = _intake()
    accepted = core._merge_funding_review_details(intake, {"property_type": "Primaary"})
    assert accepted == {"occupancy": "primary_residence"}
    details = intake.intake_state["funding_review_details"]
    assert "property_type" not in details
    assert details["occupancy"] == "primary_residence"
    assert details["sources"] == {"occupancy": "chat"}

    assert core._merge_funding_review_details(intake, {"property_type": "weird thing"}) == {}
    assert "property_type" not in intake.intake_state["funding_review_details"]

    accepted = core._merge_funding_review_details(
        intake, {"property_type": "duplex", "business_purpose": "yes", "existing_payoff_amount": 292991.2}
    )
    assert accepted == {"property_type": "multi_family", "business_purpose": True, "existing_payoff_amount": 292991.2}


def test_an_explicit_occupancy_wins_over_one_read_from_property_type() -> None:
    intake = _intake()
    core._merge_funding_review_details(intake, {"property_type": "primary", "occupancy": "investment"})
    assert intake.intake_state["funding_review_details"]["occupancy"] == "investment"


def test_a_restated_transaction_updates_the_basics_and_loan_purpose() -> None:
    intake = _intake(loan_purpose="Purchase", funding_review_basics={"transaction_type": "Purchase"})
    accepted = core._merge_funding_review_details(intake, {"transaction_type": "it is a heloc"})
    assert accepted == {"transaction_type": "Home equity (HELOC)"}
    assert intake.intake_state["funding_review_basics"]["transaction_type"] == "Home equity (HELOC)"
    assert intake.loan_purpose == "Home equity (HELOC)"
    assert "transaction_type" not in intake.intake_state["funding_review_details"]
    assert intake.intake_state["funding_review_details"]["sources"]["transaction_type"] == "chat"
    # "Not sure" never overwrites what the form said.
    assert core._merge_funding_review_details(intake, {"transaction_type": "not sure"}) == {}


def test_the_dscr_screen_reads_every_tier_the_form_offers() -> None:
    def estimate(tier: str) -> int | None:
        intake = _intake(funding_review_basics={"estimated_credit_tier": tier})
        intake.estimated_credit_score = None
        return core._dscr_fico_estimate(intake)[0]

    assert estimate("Strong Credit") == 740
    assert estimate("Mid Credit") == 700
    assert estimate("Below Threshold") == 620
    assert estimate("700+") == 700


def test_the_context_says_owner_occupied_collateral_and_what_is_still_needed() -> None:
    intake = _intake(
        loan_purpose="Home equity (HELOC)",
        funding_review_basics={"transaction_type": "Home equity (HELOC)"},
        funding_review_details={"occupancy": "primary_residence", "business_purpose": True},
    )
    intake.business_name = None
    intake.full_name = "Stephen Bronowski"
    intake.estimated_credit_score = None
    context = core._funding_review_context(intake)
    assert context["collateral_type"] == "primary residence pledged for a business-purpose loan"
    assert context["occupancy"] == "Primary residence"
    assert context["business_purpose"] is True
    assert context["personal_use_review"] is None
    assert "down_payment_amount" not in context["details_still_needed"]
