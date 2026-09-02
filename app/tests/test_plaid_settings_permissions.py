from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app.dealer_os.router import update_dealer_plaid_settings
from app.dealer_os.schemas import PlaidSettingsPatch
from app.enums import Role
from app.routers.application_profiles import update_application_plaid_settings
from app.schemas.application_profile import ApplicationPlaidSettingsPatch


@pytest.mark.parametrize("role", [Role.FIELD_REP, Role.LOAN_EXEC, Role.BROKER])
@pytest.mark.asyncio
async def test_dealer_plaid_settings_require_super_admin(role: Role) -> None:
    payload = PlaidSettingsPatch(
        assets_enabled=True,
        statements_enabled=False,
        acknowledged=True,
    )

    with pytest.raises(HTTPException) as exc:
        await update_dealer_plaid_settings(
            uuid4(),
            payload,
            BackgroundTasks(),
            SimpleNamespace(role=role),
            None,
        )

    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", [Role.FIELD_REP, Role.LOAN_EXEC, Role.BROKER])
@pytest.mark.asyncio
async def test_application_plaid_settings_require_super_admin(role: Role) -> None:
    payload = ApplicationPlaidSettingsPatch(
        assets_enabled=False,
        statements_enabled=True,
        acknowledged=True,
    )

    with pytest.raises(HTTPException) as exc:
        await update_application_plaid_settings(
            uuid4(),
            payload,
            BackgroundTasks(),
            SimpleNamespace(role=role),
            None,
        )

    assert exc.value.status_code == 403


@pytest.mark.parametrize("schema", [PlaidSettingsPatch, ApplicationPlaidSettingsPatch])
def test_plaid_settings_require_at_least_one_product(schema) -> None:
    with pytest.raises(ValidationError, match="At least one Plaid product"):
        schema(
            assets_enabled=False,
            statements_enabled=False,
            acknowledged=True,
        )


@pytest.mark.parametrize("schema", [PlaidSettingsPatch, ApplicationPlaidSettingsPatch])
def test_plaid_settings_require_explicit_acknowledgement(schema) -> None:
    with pytest.raises(ValidationError):
        schema(
            assets_enabled=True,
            statements_enabled=True,
            acknowledged=False,
        )
