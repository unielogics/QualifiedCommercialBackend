"""Every form that starts a file has to collect a number we can reach.

It is the only channel that still works when an email bounces or goes unread,
and the pre-call sequence, the consent grant and the room PIN all depend on
having one. Numbers are stored as E.164 because the consent grant, the opt-out
suppression list and the SMS ledger all look them up that way — "(973) 555-0148"
and "+19735550148" being the same person has to be true in the database, not
just to a reader.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.phone import normalize

# --- normalising --------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("9735550148", "+19735550148"),
        ("(973) 555-0148", "+19735550148"),
        ("973.555.0148", "+19735550148"),
        ("+1 973 555 0148", "+19735550148"),
        ("19735550148", "+19735550148"),
        ("  973-555-0148  ", "+19735550148"),
        ("+44 7700 900123", "+447700900123"),
    ],
)
def test_a_number_is_stored_the_way_the_sms_side_looks_it_up(typed, stored):
    assert normalize(typed) == stored


@pytest.mark.parametrize("typed", [None, "", "   ", "555-0148", "call me", "12", "+1", "abcdefghij"])
def test_anything_we_cannot_be_confident_about_is_refused_rather_than_guessed(typed):
    # Texting the wrong person is worse than asking someone to retype.
    assert normalize(typed) is None


def test_the_same_number_typed_two_ways_is_one_number():
    assert normalize("(973) 555-0148") == normalize("+1 973-555-0148") == normalize("9735550148")


# --- the forms that start a file ----------------------------------------------


def _creation_schemas():
    from app.routers.dealer_ai_intake import (
        AdminLeadCreate,
        AdminLeadFromBucketCreate,
        BrokerLeadCreate,
        DealerIntakeStart,
        FundingReviewStart,
        McaRefiStart,
    )
    from app.routers.public import InvestorInquiry, PublicBookingCreate, SupportInquiry

    return [
        (DealerIntakeStart, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (FundingReviewStart, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (McaRefiStart, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (AdminLeadCreate, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (BrokerLeadCreate, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (AdminLeadFromBucketCreate, {"full_name": "Loyd Bradley", "email": "l@example.com"}),
        (InvestorInquiry, {"name": "Loyd Bradley", "email": "l@example.com"}),
        (SupportInquiry, {"name": "Loyd Bradley", "email": "l@example.com", "message": "hello"}),
        (PublicBookingCreate, {"name": "Loyd Bradley", "email": "l@example.com"}),
    ]


def test_no_form_that_starts_a_file_accepts_a_missing_number():
    for schema, base in _creation_schemas():
        with pytest.raises(ValidationError) as err:
            schema(**base)
        assert "phone" in str(err.value), schema.__name__


def test_no_form_that_starts_a_file_accepts_a_blank_number():
    for schema, base in _creation_schemas():
        with pytest.raises(ValidationError):
            schema(**base, phone="")


def test_no_form_that_starts_a_file_accepts_an_incomplete_number():
    for schema, base in _creation_schemas():
        with pytest.raises(ValidationError):
            schema(**base, phone="555-0148")


def test_a_good_number_is_normalised_on_the_way_in():
    for schema, base in _creation_schemas():
        try:
            row = schema(**base, phone="(973) 555-0148")
        except ValidationError as err:  # a schema needing more required fields
            assert "phone" not in str(err), schema.__name__
            continue
        assert row.phone == "+19735550148", schema.__name__


def test_the_refusal_says_what_to_do_about_it():
    from app.routers.dealer_ai_intake import DealerIntakeStart

    with pytest.raises(ValidationError) as missing:
        DealerIntakeStart(full_name="x", email="e@example.com", phone=None)
    assert "mobile number is required" in str(missing.value)

    with pytest.raises(ValidationError) as unparseable:
        DealerIntakeStart(full_name="x", email="e@example.com", phone="nope")
    assert "does not look complete" in str(unparseable.value)


# --- editing an existing file -------------------------------------------------


def test_an_edit_may_leave_the_number_alone():
    # These are PATCH bodies where absent means "no change"; requiring a phone
    # here would force every unrelated edit to resend one.
    from app.routers.dealer_ai_intake import AdminLeadContactUpdate, DealerIntakePatch

    assert AdminLeadContactUpdate().phone is None
    assert DealerIntakePatch().phone is None


def test_an_edit_still_cannot_store_an_unreachable_number():
    from app.routers.dealer_ai_intake import AdminLeadContactUpdate

    assert AdminLeadContactUpdate(phone="9735550148").phone == "+19735550148"
    with pytest.raises(ValidationError):
        AdminLeadContactUpdate(phone="555-0148")


def test_the_intake_column_still_fits_a_normalised_number():
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    longest = normalize("+" + "9" * 15)
    assert longest is not None
    assert len(longest) <= PublicUnderwritingIntake.__table__.columns["phone"].type.length
