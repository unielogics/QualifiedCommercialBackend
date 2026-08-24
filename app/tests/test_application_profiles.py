from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.models.application_profile import ApplicationOwner, ApplicationPlaidItem
from app.routers.application_profiles import _require_profile_bank_client
from app.routers.communications import _intake_allowed_channels
from app.schemas.application_profile import FileOwnerPatch


def test_owner_credit_threshold_is_inclusive_and_requires_personal_contacts() -> None:
    below = ApplicationOwner(first_name="Alex", last_name="Rivera", ownership_pct=19.99)
    required = ApplicationOwner(first_name="Blair", last_name="Chen", ownership_pct=20.00)

    assert below.credit_required is False
    assert required.credit_required is True
    assert required.credit_contact_complete is False

    required.email = "blair@example.com"
    required.phone = "+12025550123"
    assert required.credit_contact_complete is True


def test_owner_patch_allows_omitted_names_but_rejects_clearing_them() -> None:
    assert FileOwnerPatch(email="owner@example.com").first_name is None

    with pytest.raises(ValidationError):
        FileOwnerPatch(first_name=None)
    with pytest.raises(ValidationError):
        FileOwnerPatch(last_name="   ")


def test_model_metadata_contains_partial_uniqueness_contracts() -> None:
    owner_indexes = {index.name for index in ApplicationOwner.__table__.indexes}
    bank_indexes = {index.name for index in ApplicationPlaidItem.__table__.indexes}

    assert "uq_application_owners_primary" in owner_indexes
    assert "uq_application_owners_email" in owner_indexes
    assert "uq_application_plaid_items_primary" in bank_indexes


@pytest.mark.parametrize(
    ("role", "channels"),
    [
        (Role.SUPER_ADMIN, {"underwriter_ai", "client", "partner", "internal"}),
        (Role.LOAN_EXEC, {"underwriter_ai", "client", "partner", "internal"}),
        (Role.DEALER_PARTNER, {"partner"}),
        (Role.BROKER, {"client"}),
        (Role.REGIONAL_MANAGER, {"client"}),
        (Role.CLIENT, {"client"}),
        (Role.VENDOR, set()),
    ],
)
def test_intake_channels_remain_role_confined(role: Role, channels: set[str]) -> None:
    assert _intake_allowed_channels(SimpleNamespace(role=role)) == channels


def test_application_bank_actions_are_client_owned() -> None:
    application = SimpleNamespace(dealer_id=None)
    dealer = SimpleNamespace(dealer_id="dealer-id")

    _require_profile_bank_client(dealer, SimpleNamespace(role=Role.DEALER))

    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP):
        _require_profile_bank_client(application, SimpleNamespace(role=role))

        with pytest.raises(HTTPException) as dealer_error:
            _require_profile_bank_client(dealer, SimpleNamespace(role=role))
        assert dealer_error.value.status_code == 403
