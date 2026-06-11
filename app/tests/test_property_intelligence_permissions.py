from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from app.enums import Role
from app.routers.analysis import _enforce_property_lookup_access
from app.schemas.analysis import AddressParts, PropertyIntelligenceLookupRequest


def _user(role: Role):
    return SimpleNamespace(role=role)


def _payload(**overrides):
    data = {
        "address": AddressParts(full="919 Franklin Terrace, Roselle, NJ"),
        "client_id": None,
        "deal_id": None,
        "loan_id": None,
        "force_refresh": False,
    }
    data.update(overrides)
    return PropertyIntelligenceLookupRequest(**data)


def test_operators_can_run_unlinked_lookup_and_force_refresh():
    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        _enforce_property_lookup_access(_user(role), _payload(force_refresh=True))


def test_broker_and_client_lookup_requires_visible_link_scope():
    for role in (Role.BROKER, Role.CLIENT):
        _enforce_property_lookup_access(_user(role), _payload(client_id=uuid4()))


def test_broker_and_client_cannot_run_unlinked_lookup():
    with pytest.raises(HTTPException) as exc:
        _enforce_property_lookup_access(_user(Role.BROKER), _payload())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Link a client" in exc.value.detail


def test_non_operators_cannot_force_refresh_provider_data():
    with pytest.raises(HTTPException) as exc:
        _enforce_property_lookup_access(_user(Role.CLIENT), _payload(client_id=uuid4(), force_refresh=True))

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
    assert "force-refresh" in exc.value.detail


def test_lender_cannot_trigger_property_intelligence_lookup():
    with pytest.raises(HTTPException) as exc:
        _enforce_property_lookup_access(_user(Role.LENDER), _payload(client_id=uuid4()))

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
