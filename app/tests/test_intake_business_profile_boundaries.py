"""Required business answers apply to new AI files, never to historical edits."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.routers import dealer_ai_intake as core
from app.routers.foreclosure_rescue import ForeclosureRescueIntakeCreate
from app.services.business_profile import PROFILE_FIELDS
from app.tests._business_profile_fixture import BUSINESS_PROFILE

MODELS = [core.DealerIntakeStart, core.FundingReviewStart, core.McaRefiStart,
    core.CapitalReadinessClaimRequest, core.AdminLeadCreate, core.BrokerLeadCreate,
    core.AdminLeadFromBucketCreate, ForeclosureRescueIntakeCreate]


@pytest.mark.parametrize("model", MODELS)
def test_every_interactive_ai_creation_requires_all_business_profile_answers(model):
    if model is not core.AdminLeadCreate:
        assert all(model.model_fields[key].is_required() for key in PROFILE_FIELDS)
    for values in ({}, {key: "  " for key in PROFILE_FIELDS}):
        if model is core.AdminLeadCreate:
            # Operator completion still requires every business answer; the
            # invitation mode intentionally defers them to the applicant.
            values = {**values, "full_name": "Example Owner", "email": "owner@example.com", "phone": "2025550123", "secure_room_pin": "123456"}
        with pytest.raises(ValidationError) as error:
            model.model_validate(values)
        errors = {item["loc"][0] for item in error.value.errors()}
        assert set(PROFILE_FIELDS) <= errors


@pytest.mark.parametrize("key", PROFILE_FIELDS)
def test_operator_boundary_cannot_be_bypassed_by_omitting_a_single_business_fact(key):
    values = {**BUSINESS_PROFILE, "full_name": "Owner", "email": "owner@example.com", "phone": "2025550123", "secure_room_pin": "123456"}
    values.pop(key)
    with pytest.raises(ValidationError):
        core.AdminLeadCreate.model_validate(values)
    invited = core.AdminLeadCreate.model_validate({**values, "application_completion": "client"})
    assert getattr(invited, key) is None


@pytest.mark.parametrize("model", MODELS[:-1])
def test_complete_explicit_profile_is_accepted_by_all_create_variants(model):
    row = model.model_validate({
        **BUSINESS_PROFILE, "full_name": "Example Owner", "business_name": "Example Services LLC",
        "email": "owner@example.com", "phone": "2025550123", "secure_room_pin": "123456",
        "terms_accepted": True, "privacy_accepted": True, "idempotency_key": uuid4(),
        "capital_readiness_diagnostic": {
            "version": 1, "source": "capital_readiness_diagnostic", "verification_status": "self_reported_unverified",
            "locale": "en", "answers": {"businessType": "main_street"},
            "result": {"band": "insufficient", "score": 0, "coverage": 0},
        },
    })
    assert row.industry == "other"
    assert row.business_website == "N/A"
    assert row.business_state == "NJ" and row.business_zip == "07102"


def test_contact_correction_remains_partial_for_files_created_before_these_questions():
    assert core.AdminLeadContactUpdate(email="corrected@example.com").model_dump(exclude_unset=True) == {"email": "corrected@example.com"}
    assert core.AdminLeadContactUpdate(business_city=None).model_dump(exclude_unset=True) == {"business_city": None}
    for key in PROFILE_FIELDS:
        assert not core.AdminLeadContactUpdate.model_fields[key].is_required()
        assert not core.DealerIntakeRead.model_fields[key].is_required()


def test_business_address_is_not_replaced_by_target_property_address():
    row = core.FundingReviewStart(**BUSINESS_PROFILE, full_name="Example Owner", email="owner@example.com", phone="2025550123", target_property_address="987 Collateral Avenue, Tampa, FL")
    assert row.business_street == "123 Example Road"
    assert row.business_city == "Newark" and row.business_state == "NJ"
    assert row.target_property_address == "987 Collateral Avenue, Tampa, FL"
