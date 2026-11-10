"""Starting an MCA file preserves CRM identity and prevents duplicate rooms."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services.admin_client_adoption import client_for_admin_mca_application


def setup(*, existing=None, role=Role.SUPER_ADMIN, **changes):
    client = SimpleNamespace(id=uuid4(), email="owner@example.test", name="Owner")
    payload = SimpleNamespace(client_id=client.id, variant="main_street", intent="merchant_cash_advance",
                              email="OWNER@example.test", force_new=False)
    for key, value in changes.items():
        setattr(payload, key, value)
    def result(value):
        return SimpleNamespace(scalar_one_or_none=lambda: value)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result(client), result(existing)]))
    return db, SimpleNamespace(role=role), payload, client


async def adopt(db, user, payload):
    return await client_for_admin_mca_application(db, user, payload, intake_variant="main_street_v1")


@pytest.mark.asyncio
async def test_no_client_id_keeps_the_original_new_file_flow():
    db, user, payload, _ = setup(client_id=None)
    assert await adopt(db, user, payload) is None
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_adoption_returns_the_existing_identity_without_creating_a_client():
    db, user, payload, client = setup()
    assert await adopt(db, user, payload) is client
    assert client.email == "owner@example.test" and client.name == "Owner"
    lock_query = str(db.execute.await_args_list[0].args[0])
    duplicate_query = str(db.execute.await_args_list[1].args[0])
    assert "FOR UPDATE" in lock_query
    assert "client_id" in duplicate_query and "archived_at IS NULL" in duplicate_query


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.LOAN_EXEC, Role.FIELD_REP])
async def test_client_id_never_expands_non_admin_access(role):
    db, user, payload, _ = setup(role=role)
    with pytest.raises(HTTPException) as error:
        await adopt(db, user, payload)
    assert error.value.status_code == 403
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"variant": "dealer"}, {"intent": "mca_refinance"}, {"intent": "term_loan_10_year"}])
async def test_existing_client_adoption_is_scoped_to_this_product(changes):
    db, user, payload, _ = setup(**changes)
    with pytest.raises(HTTPException) as error:
        await adopt(db, user, payload)
    assert error.value.status_code == 422
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_a_changed_email_requires_identity_review_before_creating_a_room():
    db, user, payload, _ = setup(email="different@example.test")
    with pytest.raises(HTTPException) as error:
        await adopt(db, user, payload)
    assert error.value.status_code == 409
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_retry_points_to_the_existing_mca_file():
    existing = uuid4()
    db, user, payload, _ = setup(existing=existing)
    with pytest.raises(HTTPException) as error:
        await adopt(db, user, payload)
    assert error.value.status_code == 409
    assert error.value.detail["intake_id"] == str(existing)


@pytest.mark.asyncio
async def test_an_explicit_separate_application_retains_existing_force_new_behavior():
    db, user, payload, client = setup(force_new=True)
    assert await adopt(db, user, payload) is client
    assert db.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.LOAN_EXEC, Role.SUPER_ADMIN])
async def test_create_route_checks_governance_before_adoption_or_creation(monkeypatch, role):
    from app.routers import dealer_ai_intake as core
    from app.routers.dealer_ai_intake_routes.admin import create_admin_ai_lead
    from app.services import admin_client_adoption as adoption

    db, user, payload, client = setup(role=role)
    lookup = AsyncMock(return_value=client)
    create = AsyncMock(return_value="created")
    monkeypatch.setattr(adoption, "client_for_admin_mca_application", lookup)
    monkeypatch.setattr(core, "_create_admin_ai_lead_core", create)
    request = SimpleNamespace()
    if role != Role.SUPER_ADMIN:
        with pytest.raises(HTTPException) as error:
            await create_admin_ai_lead(payload, request, user, db)
        assert error.value.status_code == 403
        lookup.assert_not_called()
        create.assert_not_called()
    else:
        assert await create_admin_ai_lead(payload, request, user, db) == "created"
        lookup.assert_awaited_once_with(db, user, payload, intake_variant=core._ADMIN_VARIANT_CONSTANTS["main_street"])
        create.assert_awaited_once_with(payload, request=request, user=user, db=db, client_override=client)


@pytest.mark.asyncio
async def test_create_route_preserves_legacy_core_arguments_without_client_id(monkeypatch):
    from app.routers import dealer_ai_intake as core
    from app.routers.dealer_ai_intake_routes.admin import create_admin_ai_lead

    db, user, payload, _ = setup(client_id=None)
    create = AsyncMock(return_value="legacy-created")
    monkeypatch.setattr(core, "_create_admin_ai_lead_core", create)
    request = SimpleNamespace()
    assert await create_admin_ai_lead(payload, request, user, db) == "legacy-created"
    create.assert_awaited_once_with(payload, request=request, user=user, db=db)
    db.execute.assert_not_called()
