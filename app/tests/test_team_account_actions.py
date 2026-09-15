from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app import deps
from app.enums import Role
from app.routers import users as users_router
from app.services.email.ses_client import SesSendResult


def _request(path: str = "/api/v1/users") -> SimpleNamespace:
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers={"x-forwarded-for": "203.0.113.7", "user-agent": "pytest"},
        client=None,
    )


def _actor() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)


def _team_user(*, clerk_id: str | None = "user_target") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="Auto Agent",
        email="agent+auto@example.com",
        role=Role.DEALER_PARTNER,
        clerk_id=clerk_id,
        deleted_at=None,
        account_status="active",
        account_access_types=[],
        last_invited_at=None,
        last_invite_status=None,
        last_invite_error=None,
        suspended_at=None,
        suspended_by_user_id=None,
    )


def _db(user: SimpleNamespace) -> SimpleNamespace:
    result = SimpleNamespace(scalar_one_or_none=lambda: user)
    return SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock())


def test_removed_accounts_are_blocked_even_on_the_identity_status_route() -> None:
    removed = SimpleNamespace(deleted_at=object(), account_status="active")
    with pytest.raises(HTTPException) as exc:
        deps._enforce_account_active(removed, _request("/api/v1/auth/me"))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "account_removed"


def test_suspended_accounts_may_read_identity_status_but_nothing_else() -> None:
    suspended = SimpleNamespace(deleted_at=None, account_status="suspended")
    deps._enforce_account_active(suspended, _request("/api/v1/auth/me"))
    with pytest.raises(HTTPException) as exc:
        deps._enforce_account_active(suspended, _request())
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "account_suspended"


@pytest.mark.asyncio
async def test_team_suspend_blocks_backend_and_revokes_clerk_sessions() -> None:
    user = _team_user()
    actor = _actor()
    db = _db(user)
    with (
        patch.object(users_router.clerk_service, "set_user_suspended", AsyncMock(return_value=True)) as suspend,
        patch.object(users_router.clerk_service, "revoke_user_sessions", AsyncMock(return_value=True)) as revoke,
        patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock(return_value=True)) as metadata,
        patch.object(users_router, "record_access_event") as record,
    ):
        result = await users_router.update_team_account_status(
            user.id,
            users_router.TeamAccountStatusPatch(
                account_status="suspended",
                reason="Operator requested immediate access suspension.",
            ),
            _request(),
            db,
            current=actor,
        )

    assert result.account_status == "suspended" and result.sessions_revoked is True
    assert user.account_status == "suspended" and user.suspended_by_user_id == actor.id
    suspend.assert_awaited_once_with("user_target", True)
    revoke.assert_awaited_once_with("user_target")
    assert metadata.await_args.kwargs["account_status"] == "suspended"
    assert record.call_args.kwargs["action"] == "team_access.suspended"


@pytest.mark.asyncio
async def test_team_invite_resend_is_only_for_an_unbound_login() -> None:
    active = _team_user()
    with pytest.raises(HTTPException) as exc:
        await users_router.resend_team_invite(active.id, _request(), _db(active), current=_actor())
    assert exc.value.status_code == 409

    invited = _team_user(clerk_id=None)
    with (
        patch.object(users_router.clerk_service, "invite_user", AsyncMock(return_value={"id": "inv_1"})) as send,
        patch.object(users_router, "record_access_event") as record,
    ):
        result = await users_router.resend_team_invite(
            invited.id,
            _request(),
            _db(invited),
            current=_actor(),
        )
    assert result.invitation_sent is True and invited.last_invite_status == "sent"
    assert send.await_args.kwargs["role"] == Role.DEALER_PARTNER
    assert record.call_args.kwargs["action"] == "team_access.invite_resent"


@pytest.mark.asyncio
async def test_password_reset_action_emails_only_a_self_service_link() -> None:
    user = _team_user()
    mailer = AsyncMock(return_value=SesSendResult(True, "message-1", "sent"))
    settings = SimpleNamespace(frontend_app_url="https://app.example.test/")
    with (
        patch("app.services.email.user_mailer.send_as_user", mailer),
        patch.object(users_router, "get_settings", return_value=settings),
        patch.object(users_router, "record_access_event") as record,
    ):
        result = await users_router.send_team_password_reset(
            user.id,
            _request(),
            _db(user),
            current=_actor(),
        )
    assert result.reset_instructions_sent is True
    body = mailer.await_args.kwargs["body_text"]
    assert "forgot-password?email=agent%2Bauto%40example.com" in body
    assert "six-digit reset code" in body
    assert "password=" not in body.casefold()
    assert record.call_args.kwargs["action"] == "team_access.password_reset_sent"
