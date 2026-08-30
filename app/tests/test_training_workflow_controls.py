from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os import router
from app.dealer_os.schemas import DealerCreate, DealerWorkflowSettingsPatch
from app.enums import Role
from app.routers import application_profiles as application_profiles_router
from app.services import application_profiles as application_profiles_service


def _user(role: Role) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        role=role,
        name="Test user",
        email="test@qualifiedcommercial.com",
    )


def _request(header: str | None = None) -> SimpleNamespace:
    headers = {"x-qc-training-live-action": header} if header else {}
    return SimpleNamespace(headers=headers)


def test_new_files_default_to_live_and_gated() -> None:
    payload = DealerCreate(
        name="Example LLC",
        entity_type="Limited liability company",
        funding_goal=100_000,
        funding_purpose="working_capital",
        use_of_proceeds_note="Purchase inventory and support payroll.",
    )

    assert payload.is_training is False
    with pytest.raises(ValidationError, match="Provide is_training or workflow_ungated"):
        DealerWorkflowSettingsPatch()


def test_workflow_settings_payloads_remain_partial() -> None:
    training = DealerWorkflowSettingsPatch(is_training=True)
    ungated = DealerWorkflowSettingsPatch(workflow_ungated=True)

    assert training.model_dump(exclude_none=True) == {"is_training": True}
    assert ungated.model_dump(exclude_none=True) == {"workflow_ungated": True}


def test_enabling_training_automatically_ungates_workflow() -> None:
    dealer = SimpleNamespace(is_training=False, workflow_ungated=False)

    requested = router._effective_workflow_settings(
        dealer, DealerWorkflowSettingsPatch(is_training=True)
    )

    assert requested == {"is_training": True, "workflow_ungated": True}


def test_training_file_cannot_be_gated() -> None:
    dealer = SimpleNamespace(is_training=True, workflow_ungated=True)

    requested = router._effective_workflow_settings(
        dealer, DealerWorkflowSettingsPatch(workflow_ungated=False)
    )

    assert requested == {"workflow_ungated": True}


def test_returning_training_file_to_live_preserves_ungated_state() -> None:
    dealer = SimpleNamespace(is_training=True, workflow_ungated=True)

    requested = router._effective_workflow_settings(
        dealer, DealerWorkflowSettingsPatch(is_training=False)
    )

    assert requested == {"is_training": False}


@pytest.mark.asyncio
async def test_training_file_is_hidden_from_non_super_admin_direct_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dealer = SimpleNamespace(id=uuid4(), is_training=True)
    monkeypatch.setattr(router, "load_dealer", AsyncMock(return_value=dealer))

    with pytest.raises(HTTPException) as exc:
        await router._load_visible_dealer(AsyncMock(), dealer.id, _user(Role.LOAN_EXEC))

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_super_admin_can_directly_load_training_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dealer = SimpleNamespace(id=uuid4(), is_training=True)
    monkeypatch.setattr(router, "load_dealer", AsyncMock(return_value=dealer))

    loaded = await router._load_visible_dealer(
        AsyncMock(), dealer.id, _user(Role.SUPER_ADMIN)
    )

    assert loaded is dealer


@pytest.mark.asyncio
async def test_training_live_action_requires_explicit_confirmation() -> None:
    dealer = SimpleNamespace(id=uuid4(), is_training=True)

    with pytest.raises(HTTPException) as exc:
        await router._require_training_live_action(
            AsyncMock(),
            dealer=dealer,
            user=_user(Role.SUPER_ADMIN),
            request=_request(),
            action="Send invitation",
            provider="SES",
            recipient="client@example.com",
            effect="Send a real email.",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "training_live_action_confirmation_required"
    assert exc.value.detail["provider"] == "SES"


@pytest.mark.asyncio
async def test_confirmed_training_live_action_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dealer = SimpleNamespace(id=uuid4(), is_training=True)
    audit = AsyncMock()
    monkeypatch.setattr(router, "log_action", audit)
    user = _user(Role.SUPER_ADMIN)

    await router._require_training_live_action(
        AsyncMock(),
        dealer=dealer,
        user=user,
        request=_request("confirmed"),
        action="Send invitation",
        provider="SES",
        recipient="client@example.com",
        effect="Send a real email.",
    )

    audit.assert_awaited_once()
    assert audit.await_args.args[3] == "training.live_action_confirmed"


@pytest.mark.asyncio
async def test_live_files_do_not_require_external_action_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = AsyncMock()
    monkeypatch.setattr(router, "log_action", audit)

    await router._require_training_live_action(
        AsyncMock(),
        dealer=SimpleNamespace(id=uuid4(), is_training=False),
        user=_user(Role.FIELD_REP),
        request=_request(),
        action="Send invitation",
        provider="SES",
        recipient="client@example.com",
        effect="Send a real email.",
    )

    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_training_application_profile_action_uses_same_confirmation_contract() -> None:
    dealer_id = uuid4()
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=dealer_id, is_training=True)

    with pytest.raises(HTTPException) as exc:
        await application_profiles_router._require_training_live_action(
            db,
            profile=SimpleNamespace(id=uuid4(), dealer_id=dealer_id),
            user=_user(Role.SUPER_ADMIN),
            request=_request(),
            action="Send document request",
            provider="SES",
            recipient="client@example.com",
            effect="Send a real secure-room request.",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "training_live_action_confirmation_required"


@pytest.mark.asyncio
async def test_training_application_profile_is_hidden_from_loan_executive() -> None:
    db = AsyncMock()
    result = SimpleNamespace(scalar_one_or_none=lambda: True)
    db.execute.return_value = result

    visible = await application_profiles_service._profile_is_visible(
        db,
        SimpleNamespace(dealer_id=uuid4()),
        _user(Role.LOAN_EXEC),
    )

    assert visible is False
