"""The business website and a short description, asked on the first screen."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.routers import dealer_ai_intake as core
from app.routers import foreclosure_rescue
from app.routers.dealer_ai_intake_routes import (
    admin,
    client,
    funding_review,
    mca_refinance,
    public_intake,
)
from app.services import business_profile, industry_suggestion


def _payload(website=None, description=None):
    return SimpleNamespace(business_website=website, business_description=description)


def _intake(state=None):
    return SimpleNamespace(intake_state=state or {}, id="intake-1")


_EMPTY_ADDRESS = {"street": None, "city": None, "state": None, "zip": None, "industry": None}


# --- the website --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("example.com", ("https://example.com", False)),
        ("  Example.COM  ", ("https://example.com", False)),
        ("https://Example.com/", ("https://example.com", False)),
        ("http://shop.example.co.uk/menu/", ("http://shop.example.co.uk/menu/", False)),
        ("www.joe.com/About", ("https://www.joe.com/About", False)),
        ("facebook.com/mybiz", ("https://facebook.com/mybiz", False)),
        ("HTTPS://EXAMPLE.COM/Path", ("https://example.com/Path", False)),
        ("", (None, False)),
        ("   ", (None, False)),
        (None, (None, False)),
    ],
)
def test_an_address_becomes_a_link_and_blank_stays_blank(typed, expected) -> None:
    assert business_profile.normalize_website(typed) == expected


@pytest.mark.parametrize("typed", ["N/A", "n/a", "n/a.", "NA", "None", "no website", "No", "Not applicable", "-", "no aplica", "Ninguno"])
def test_na_and_its_relatives_mean_the_business_has_no_website(typed) -> None:
    assert business_profile.normalize_website(typed) == (None, True)


@pytest.mark.parametrize("typed", ["javascript:alert(1)", "not a site", "localhost", "a.b", "example", "ftp://example.com", "https://exa mple.com", "data:text/html,x", "x" * 300 + ".com"])
def test_anything_that_is_not_a_web_address_is_refused_with_the_sentence_to_show(typed) -> None:
    with pytest.raises(ValueError, match="Enter your website address"):
        business_profile.normalize_website(typed)


# --- the description -----------------------------------------------------------------------------


def test_a_description_is_trimmed_and_must_say_something() -> None:
    assert business_profile.clean_description("  We repair   commercial HVAC systems. ") == "We repair commercial HVAC systems."
    assert business_profile.clean_description("") is None
    for too_short in ("N/A", "none", "hvac"):
        with pytest.raises(ValueError, match="sentence or two"):
            business_profile.clean_description(too_short)
    with pytest.raises(ValueError):
        business_profile.clean_description("x" * 1001)


# --- the payload ---------------------------------------------------------------------------------------


def test_the_fields_are_optional_on_the_wire_and_validated_when_sent() -> None:
    fields = business_profile.BusinessProfileFields()
    assert fields.business_website is None and fields.business_description is None

    sent = business_profile.BusinessProfileFields(business_website="N/A", business_description="We run two coffee shops.")
    assert sent.business_website == "N/A"

    with pytest.raises(ValidationError, match="Enter your website address"):
        business_profile.BusinessProfileFields(business_website="not a site")
    with pytest.raises(ValidationError, match="sentence or two"):
        business_profile.BusinessProfileFields(business_description="n/a")


def test_every_ai_application_start_takes_the_two_answers() -> None:
    for model in (core.DealerIntakeStart, core.FundingReviewStart, core.McaRefiStart, core.CapitalReadinessClaimRequest, foreclosure_rescue.ForeclosureRescueIntakeCreate):
        assert {"business_website", "business_description"} <= set(model.model_fields), model.__name__
        assert model.model_fields["business_website"].default is None, model.__name__


# --- what is kept --------------------------------------------------------------------------------------


def test_the_record_keeps_the_address_the_none_flag_and_the_words() -> None:
    kept = business_profile.record(_payload("example.com", "We run two coffee shops."))
    assert kept["website"] == "https://example.com" and kept["website_none"] is False
    assert kept["description"] == "We run two coffee shops." and kept["captured_at"]

    none = business_profile.record(_payload("N/A", "We run two coffee shops."))
    assert none["website"] is None and none["website_none"] is True

    assert business_profile.record(_payload()) is None
    assert business_profile.state_entry(_payload()) == {}
    assert business_profile.state_entry(_payload("example.com", None))["business_profile"]["website"] == "https://example.com"


def test_the_file_reads_what_the_client_said_and_the_desks_correction_wins() -> None:
    client_only = _intake({"business_profile": {"website": "https://example.com", "website_none": False, "description": "Coffee shops."}})
    assert business_profile.of_intake(client_only) == {"website": "https://example.com", "website_none": False, "description": "Coffee shops.", **_EMPTY_ADDRESS}

    corrected = _intake(
        {
            "business_profile": {"website": "https://old.example.com", "description": "Coffee shops."},
            "desk_facts": {"business_website": "https://new.example.com", "business_website_none": False, "business_description": "Two coffee shops and a roastery."},
        }
    )
    assert business_profile.of_intake(corrected) == {"website": "https://new.example.com", "website_none": False, "description": "Two coffee shops and a roastery.", **_EMPTY_ADDRESS}

    desk_na = _intake({"business_profile": {"website": "https://old.example.com"}, "desk_facts": {"business_website": None, "business_website_none": True}})
    assert business_profile.of_intake(desk_na)["website"] is None and business_profile.of_intake(desk_na)["website_none"] is True

    assert business_profile.of_intake(_intake()) == {"website": None, "website_none": False, "description": None, **_EMPTY_ADDRESS}
    assert business_profile.of_intake(SimpleNamespace(intake_state=None)) == {"website": None, "website_none": False, "description": None, **_EMPTY_ADDRESS}


@pytest.mark.parametrize(
    "handler",
    [
        public_intake.start_dealer_intake,
        mca_refinance.start_mca_refinance,
        funding_review.start_funding_review,
        client.claim_capital_readiness_diagnostic,
        foreclosure_rescue._create_foreclosure_rescue,
    ],
)
def test_each_start_keeps_the_answers_on_the_intake(handler) -> None:
    assert "business_profile.state_entry(payload)" in inspect.getsource(handler)


# --- who reads them ------------------------------------------------------------------------------------


def test_the_ai_is_told_what_the_client_said_in_every_variant() -> None:
    for builder in (core._dealer_context, core._main_street_context, core._funding_review_context, core._mca_context):
        assert '"business_profile": _business_profile(intake) or None' in inspect.getsource(builder), builder.__name__

    asked = _intake({"business_profile": {"website": None, "website_none": True, "description": "We run two coffee shops."}})
    assert core._business_profile(asked) == {"website": None, "has_no_website": True, "description": "We run two coffee shops.", **_EMPTY_ADDRESS}
    assert core._business_profile(_intake()) == {}


def test_the_desk_reads_and_corrects_them_through_the_existing_contact_routes() -> None:
    assert {"business_website", "business_description"} <= set(core.DealerIntakeRead.model_fields)
    assert {"business_website", "business_description"} <= set(core.AdminLeadContactUpdate.model_fields)
    update = inspect.getsource(admin.update_lead_contact)
    assert 'desk_facts' in update and "normalize_website" in update and "clean_description" in update
    assert "business_profile.apply_read(intake_read, intake)" in inspect.getsource(core._response)
    with pytest.raises(ValidationError, match="Enter your website address"):
        core.AdminLeadContactUpdate(business_website="nope nope")


async def test_the_industry_suggestion_uses_the_clients_words_only_when_no_document_spoke() -> None:
    profile = SimpleNamespace(id="p1", intake_id="i1")
    intake = _intake({"business_profile": {"description": "We run two coffee shops and a roastery."}})

    def db_with(facts):
        result = Mock()
        result.scalars.return_value.all.return_value = facts
        return SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=intake))

    assert await industry_suggestion._activity_text(db_with([]), profile) == "We run two coffee shops and a roastery."

    fact = SimpleNamespace(status="accepted", normalized_value="health and wellness studio", value={})
    assert await industry_suggestion._activity_text(db_with([fact]), profile) == "health and wellness studio"

    rejected = SimpleNamespace(status="rejected", normalized_value="x", value={})
    assert await industry_suggestion._activity_text(db_with([rejected]), profile) == "We run two coffee shops and a roastery."

    no_intake = SimpleNamespace(id="p2", intake_id=None)
    assert await industry_suggestion._activity_text(db_with([]), no_intake) is None
