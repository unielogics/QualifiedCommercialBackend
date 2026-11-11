from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.models.client import Client
from app.routers import application_profiles as rooms
from app.schemas.application_profile import ApplicationRoomAccess
from app.schemas.client_application import ClientApplicationFields, ClientApplicationPatch
from app.services import client_application as service
from app.tests._business_profile_fixture import BUSINESS_PROFILE


def intake(**changes):
    return SimpleNamespace(id=uuid4(), bucket_id=uuid4(), full_name="Original Owner", email="", phone=None,
                           business_name=None, loan_purpose=None, requested_loan_amount=None, asset_rows=[],
                           intake_state={"application_completion": "client", "unrelated": {"preserved": True}}, **changes)


def profile(row, vertical="dealer"):
    return SimpleNamespace(id=uuid4(), intake_id=row.id, client_id=uuid4(), vertical=vertical, funding_category=None)


def complete(**changes):
    return ClientApplicationFields.model_validate({**BUSINESS_PROFILE, "full_name": "Updated Owner", "email": "owner@example.com", "phone": "3055550123",
        "business_name": "Example LLC", "business_website": "N/A", "business_description": "We maintain commercial equipment.",
        "loan_purpose": "Purchase tools", "requested_loan_amount": 75000, **changes})


def request():
    return Request({"type": "http", "headers": [], "client": ("198.51.100.10", 1234)})


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("update", [False, True])
async def test_read_and_save_require_the_existing_room_pin_guard(monkeypatch, status, update):
    db = SimpleNamespace(get=AsyncMock(), commit=AsyncMock())
    authorize = AsyncMock(side_effect=HTTPException(status, "Invalid room"))
    monkeypatch.setattr(rooms, "_public_application_room", authorize)
    with pytest.raises(HTTPException) as caught:
        if update:
            await rooms.public_room_update_application("token", ClientApplicationPatch(passcode="000000"), request(), db)
        else:
            await rooms.public_room_application("token", ApplicationRoomAccess(passcode="000000"), request(), db)
    assert caught.value.status_code == status
    db.get.assert_not_called()
    db.commit.assert_not_called()


async def test_room_cannot_read_an_intake_from_another_bucket():
    row = intake()
    with pytest.raises(HTTPException) as caught:
        await rooms._room_application_intake(SimpleNamespace(get=AsyncMock(return_value=row)), profile(row), SimpleNamespace(bucket_id=uuid4()))
    assert caught.value.status_code == 404


async def test_operator_created_historical_room_does_not_expose_client_edit_form():
    row = intake()
    row.intake_state["application_completion"] = "operator"
    with pytest.raises(HTTPException) as caught:
        await rooms._room_application_intake(SimpleNamespace(get=AsyncMock(return_value=row)), profile(row), SimpleNamespace(bucket_id=row.bucket_id))
    assert caught.value.status_code == 404


async def test_draft_patch_merges_existing_answers_and_preserves_other_application_state(monkeypatch):
    row = intake()
    row.email = "original@example.com"
    row.business_name = "Original LLC"
    row.loan_purpose = "Inventory"
    row.requested_loan_amount = 80000
    application_profile = profile(row)
    link = SimpleNamespace(id=uuid4(), bucket_id=row.bucket_id)
    client = SimpleNamespace(name=row.full_name, email=row.email, phone=None, lead_intake={"source": "internal"})
    db = SimpleNamespace(get=AsyncMock(side_effect=lambda model, _id: client if model is Client else None), refresh=AsyncMock(side_effect=lambda *_args, **_kwargs: setattr(row, "business_name", "Concurrently Updated LLC")), commit=AsyncMock())
    monkeypatch.setattr(rooms, "_public_application_room", AsyncMock(return_value=(link, application_profile)))
    monkeypatch.setattr(rooms, "_room_application_intake", AsyncMock(return_value=row))
    monkeypatch.setattr(rooms.profiles, "owner_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rooms.profiles, "log_profile_action", AsyncMock())
    result = await rooms.public_room_update_application("token", ClientApplicationPatch(passcode="123456", phone="3055550123"), request(), db)
    assert result.phone == "+13055550123"
    assert row.business_name == "Concurrently Updated LLC"
    assert row.requested_loan_amount == 80000
    assert client.email == "original@example.com"
    assert row.intake_state["unrelated"] == {"preserved": True}
    assert "client_application_submitted_at" not in row.intake_state
    db.commit.assert_awaited_once()
    assert db.refresh.await_args_list[0].args == (application_profile,)
    assert db.refresh.await_args_list[1].args == (row,)
    assert all(call.kwargs == {"with_for_update": True} for call in db.refresh.await_args_list)


def test_missing_contact_draft_can_be_read_and_submission_requires_the_rest():
    row = intake()
    state = service.read_application(row, profile(row))
    assert state.email is None and state.phone is None
    with pytest.raises(HTTPException) as caught:
        service.validate_submission(ClientApplicationFields(full_name="Original Owner"), "dealer")
    assert "Email" in caught.value.detail and "Legal business name" in caught.value.detail


def test_mca_product_stays_fixed_when_the_borrower_saves_application_answers():
    row = intake()
    row.intake_state["funding_intent"] = "merchant_cash_advance"
    row.intake_state["main_street_details"] = {"intent": "merchant_cash_advance", "industry": "other"}
    application_profile = profile(row, "main_street")
    application_profile.funding_category = "merchant_cash_advance"
    read = service.read_application(row, application_profile)
    assert read.funding_need == "merchant_cash_advance" and not read.funding_need_editable
    with pytest.raises(HTTPException) as error:
        service.apply_application(row, application_profile, SimpleNamespace(), None,
                                  complete(funding_need="working_capital"), submit=False)
    assert error.value.status_code == 403
    service.apply_application(row, application_profile, SimpleNamespace(), None,
                              complete(funding_need="merchant_cash_advance"), submit=False)
    assert application_profile.funding_category == "merchant_cash_advance"
    assert row.intake_state["main_street_details"]["intent"] == "merchant_cash_advance"


def test_historical_partial_answers_are_readable_for_client_correction():
    row = intake()
    row.phone = "unknown"
    row.intake_state["business_profile"] = {"description": "Old"}
    state = service.read_application(row, profile(row))
    assert state.phone == "unknown"
    assert state.business_description == "Old"


@pytest.mark.parametrize("field,value", [("email", "invalid"), ("phone", "123"), ("requested_loan_amount", -1), ("requested_loan_amount", float("nan")), ("funding_need", "anything")])
def test_invalid_contact_financing_and_classification_are_rejected(field, value):
    with pytest.raises(ValidationError):
        complete(**{field: value})


def test_submission_updates_intake_crm_room_recipient_without_finalizing_underwriting():
    row = intake()
    application_profile = profile(row)
    application_profile.is_draft = True
    client = SimpleNamespace(lead_intake={"origin": "desk"})
    link = SimpleNamespace()
    answers = complete()
    service.validate_submission(answers, "dealer")
    service.apply_application(row, application_profile, link, client, answers, submit=True)
    assert row.email == client.email == link.recipient_email == "owner@example.com"
    assert row.full_name == client.name == link.recipient_name == "Updated Owner"
    assert row.intake_state["business_profile"]["website_none"] is True
    assert row.intake_state["client_application_submitted_at"]
    assert application_profile.is_draft is True


@pytest.mark.parametrize("locked", [False, True])
def test_only_unlocked_seeded_primary_owner_identity_is_updated(locked):
    row = intake()
    previous = service.read_application(row, profile(row))
    owner = SimpleNamespace(is_primary=True, full_name=row.full_name, first_name="Original", last_name="Owner", email=None, phone=None, has_invite=locked, credit_complete=False)
    service.sync_seeded_owner(owner, previous, complete())
    assert owner.first_name == ("Original" if locked else "Updated")
    assert owner.email == (None if locked else "owner@example.com")
    owner.has_invite = False
    owner.full_name = "Explicitly Edited"
    service.sync_seeded_owner(owner, previous, complete())
    assert owner.first_name == ("Original" if locked else "Updated")


def test_real_estate_submission_requires_property_and_preserves_additional_assets():
    answers = complete()
    with pytest.raises(HTTPException) as caught:
        service.validate_submission(answers, "real_estate")
    assert "Property address" in caught.value.detail
    answers = complete(target_property_address="123 Main Street", transaction_type="purchase", estimated_value_or_purchase_price=500000)
    service.validate_submission(answers, "real_estate")
    row = intake()
    row.asset_rows = [{"address": "Old", "notes": "retain"}, {"address": "Second asset"}]
    service.apply_application(row, profile(row, "real_estate"), SimpleNamespace(), None, answers, submit=True)
    assert row.asset_rows[0]["notes"] == "retain"
    assert row.asset_rows[1]["address"] == "Second asset"


async def test_main_street_classification_rebuild_preserves_custom_requests_and_evidence():
    from app.services.main_street_programs import documents_for

    row = intake()
    application_profile = profile(row, "main_street")
    application_profile.industry = "Staff confirmed industry"
    row.intake_state["main_street_details"] = {"industry": "other", "intent": "working_capital"}
    previous = service.read_application(row, application_profile)
    doc = documents_for("working_capital", "other")[0]
    initial = SimpleNamespace(name=doc["name"], is_custom=False, required=True)
    custom = SimpleNamespace(name="Staff custom request", is_custom=True, required=True)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [initial, custom]
    db = SimpleNamespace(execute=AsyncMock(return_value=result), add=MagicMock())
    answers = complete(industry="retail_ecommerce", funding_need="business_systems", requested_loan_amount=None, loan_purpose=None)
    service.validate_submission(answers, "main_street")
    service.apply_application(row, application_profile, SimpleNamespace(), None, answers, submit=True)
    await service.sync_main_street_documents(db, row, previous, answers)
    assert initial.required is False and custom.required is True
    assert application_profile.industry == "Staff confirmed industry"
    assert application_profile.funding_category == "business_systems"
    assert row.intake_state["main_street_details"]["intent"] == "business_systems"
    db.add.assert_not_called()


def test_single_name_seed_and_long_names_respect_owner_column_limits():
    row = intake()
    row.full_name = "Cher"
    owner = SimpleNamespace(is_primary=True, full_name="Cher Unknown", first_name="Cher", last_name="Unknown", email=None, phone=None, has_invite=False, credit_complete=False, backfill_needs_review=True)
    answers = complete(full_name="A" * 85 + " " + "B" * 85)
    service.sync_seeded_owner(owner, service.read_application(row, profile(row)), answers)
    assert len(owner.first_name) == len(owner.last_name) == 80
    assert owner.email == "owner@example.com"


@pytest.mark.parametrize("vertical", ["dealer", "main_street", "mca", "real_estate"])
def test_all_application_types_round_trip_structured_business_answers(vertical):
    row = intake()
    answers = complete(funding_need="working_capital")
    application_profile = profile(row, vertical)
    service.apply_application(row, application_profile, SimpleNamespace(), None, answers, submit=False)
    saved = service.read_application(row, application_profile)
    for key, value in BUSINESS_PROFILE.items():
        if key != "business_description":
            assert getattr(saved, key) == value
    assert row.intake_state["business_profile"]["street"] == BUSINESS_PROFILE["business_street"]
    assert saved.requested_amount_editable is True


@pytest.mark.parametrize("key", ["business_street", "business_city", "business_state", "business_zip", "industry"])
@pytest.mark.parametrize("vertical", ["dealer", "main_street", "mca", "real_estate"])
def test_submission_requires_business_address_and_industry_for_every_type(key, vertical):
    values = complete(**{key: None}, funding_need="working_capital", target_property_address="1 Collateral Lane", transaction_type="purchase", estimated_value_or_purchase_price=500000)
    with pytest.raises(HTTPException) as error:
        service.validate_submission(values, vertical)
    assert error.value.status_code == 422


def test_draft_amount_remains_editable_until_first_submission():
    row = intake()
    application_profile = profile(row)
    service.apply_application(row, application_profile, SimpleNamespace(), None, complete(), submit=False)
    service.apply_application(row, application_profile, SimpleNamespace(), None, complete(requested_loan_amount=80000), submit=True)
    assert row.requested_loan_amount == 80000
    assert service.read_application(row, application_profile).requested_amount_editable is False
    before = row.intake_state.copy()
    with pytest.raises(HTTPException) as error:
        service.apply_application(row, application_profile, SimpleNamespace(), None, complete(requested_loan_amount=90000), submit=False)
    assert error.value.status_code == 403
    assert row.requested_loan_amount == 80000 and row.intake_state == before


@pytest.mark.parametrize("mirror", [False, True])
def test_reviewed_request_is_projected_without_rewriting_original_submission(mirror):
    row = intake()
    row.requested_loan_amount = 60000
    row.intake_state["funding_review_basics"] = {"requested_amount": 60000}
    application_profile = profile(row, "real_estate")
    application_profile.current_funding_request = {"requested_amount": "80000.00", "revision": 1}
    if mirror:
        row.intake_state["current_funding_request"] = application_profile.current_funding_request.copy()
    state = service.read_application(row, application_profile)
    assert state.requested_loan_amount == 80000 and state.requested_amount_editable is False
    with pytest.raises(HTTPException) as error:
        service.apply_application(row, application_profile, SimpleNamespace(), None, complete(requested_loan_amount=90000), submit=True)
    assert error.value.status_code == 403 and "client_application_submitted_at" not in row.intake_state
    service.apply_application(row, application_profile, SimpleNamespace(), None, complete(requested_loan_amount=80000), submit=False)
    assert row.requested_loan_amount == 60000
    assert row.intake_state["funding_review_basics"]["requested_amount"] == 60000


async def test_failed_submission_keeps_saved_draft_unsubmitted_and_amount_editable(monkeypatch):
    row = intake()
    application_profile = profile(row)
    link = SimpleNamespace(bucket_id=row.bucket_id, status="active", expires_at=None,
        passcode_set_by_client_at=datetime.now(UTC))
    db = SimpleNamespace(get=AsyncMock(), refresh=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(rooms, "_public_application_room", AsyncMock(return_value=(link, application_profile)))
    monkeypatch.setattr(rooms, "_room_application_intake", AsyncMock(return_value=row))
    with pytest.raises(HTTPException) as error:
        await rooms.public_room_update_application("token", ClientApplicationPatch(passcode="123456", submit=True, requested_loan_amount=75000), request(), db)
    assert error.value.status_code == 422
    assert row.requested_loan_amount is None and "client_application_submitted_at" not in row.intake_state
    assert service.read_application(row, application_profile).requested_amount_editable is True
    db.commit.assert_not_awaited()
    db.get.assert_not_awaited()


def test_linked_credit_identity_is_not_rewritten_before_report_import():
    row = intake()
    owner = SimpleNamespace(is_primary=True, full_name=row.full_name, first_name="Original", last_name="Owner", email=None, phone=None, has_invite=False, credit_complete=False, credit_pull_id=uuid4())
    service.sync_seeded_owner(owner, service.read_application(row, profile(row)), complete())
    assert owner.first_name == "Original" and owner.email is None
