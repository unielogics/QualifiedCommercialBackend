"""The Team table's console chips are real, and the Team routes tell Clerk.

PATCH /users/{id} changed a role but never told Clerk, so the edge claim the
funding app's middleware reads kept the invite-time role. Now a change to the
console state syncs Clerk (best-effort, only once the row is bound to a Clerk
id) and lands one row in the access-event trail; a name-only patch touches
neither. Grants are validated against what the role allows.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import users as users_router


def _request():
    return SimpleNamespace(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1", "user-agent": "pytest"}, client=None)


def _actor():
    return SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)


def _staffer(role=Role.BROKER, grants=(), clerk_id="user_abc"):
    return SimpleNamespace(id=uuid4(), name="Rita Moss", email="rita@example.com", role=role, clerk_id=clerk_id,
                           deleted_at=None, account_status="active", account_access_types=list(grants),
                           referral_partner_company_id=None, phone=None, title=None)


def _db(existing_user):
    async def get(_model, _key, **_kw):
        return None

    async def execute(_stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: existing_user, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def refresh(_row):
        return None

    return SimpleNamespace(get=get, execute=execute, flush=AsyncMock(), refresh=refresh, add=lambda row: None)


@pytest.mark.asyncio
async def test_a_console_change_syncs_clerk_and_records_an_access_event():
    staffer = _staffer()
    db = _db(staffer)
    with patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock(return_value=True)) as sync, \
         patch.object(users_router, "record_access_event") as rec, \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        out = await users_router.update_user(staffer.id, users_router.UserPatch(account_types=["field_desk"]), _request(), db, current=_actor())
    assert out.account_types == ["audit", "field_desk", "funding"] and out.inherited_account_types == ["funding"]
    assert sync.await_args.args == ("user_abc",)
    assert sync.await_args.kwargs == {"role": Role.BROKER, "account_types": ["audit", "field_desk", "funding"], "account_status": "active"}
    kwargs = rec.call_args.kwargs
    assert kwargs["action"] == "team_access.updated" and kwargs["user_id"] == staffer.id
    assert kwargs["before_state"]["account_types"] == ["funding"] and kwargs["after_state"]["account_types"] == ["audit", "field_desk", "funding"]
    assert kwargs["metadata"] == {"ip_address": "203.0.113.9", "user_agent": "pytest"}


@pytest.mark.asyncio
async def test_a_role_change_reaches_clerk_and_a_name_only_patch_does_not():
    staffer = _staffer()
    db = _db(staffer)
    with patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock(return_value=True)) as sync, \
         patch.object(users_router, "record_access_event") as rec, \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        await users_router.update_user(staffer.id, users_router.UserPatch(name="Dana R"), _request(), db, current=_actor())
        assert sync.await_count == 0 and rec.call_count == 0
        await users_router.update_user(staffer.id, users_router.UserPatch(role=Role.LOAN_EXEC), _request(), db, current=_actor())
    assert sync.await_args.kwargs["role"] == Role.LOAN_EXEC
    assert sync.await_args.kwargs["account_types"] == ["audit", "field_desk", "funding"]
    assert rec.call_args.kwargs["before_state"]["role"] == "broker" and rec.call_args.kwargs["after_state"]["role"] == "loan_exec"


@pytest.mark.asyncio
async def test_an_unbound_row_records_the_event_but_does_not_call_clerk():
    staffer = _staffer(clerk_id=None)
    db = _db(staffer)
    with patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock(return_value=True)) as sync, \
         patch.object(users_router, "record_access_event") as rec, \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        await users_router.update_user(staffer.id, users_router.UserPatch(account_types=["field_desk"]), _request(), db, current=_actor())
    assert sync.await_count == 0 and rec.call_count == 1


@pytest.mark.asyncio
async def test_an_invite_carries_the_console_grants_and_lands_on_the_rep_app():
    added = []

    async def get(_model, _key, **_kw):
        return None

    async def execute(_stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def refresh(_row):
        return None

    def add(row):
        row.id = uuid4()
        added.append(row)

    db = SimpleNamespace(get=get, execute=execute, flush=AsyncMock(), refresh=refresh, add=add)
    settings = SimpleNamespace(frontend_app_url="https://f", rep_app_url="https://r", audit_app_url="https://a")
    with patch.object(users_router.clerk_service, "invite_user", AsyncMock()) as invite, \
         patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock()) as sync, \
         patch.object(users_router, "record_access_event") as rec, \
         patch.object(users_router, "get_settings", return_value=settings), \
         patch.object(users_router, "house_company", AsyncMock(return_value=None)), \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        out = await users_router.invite_user(users_router.UserInvite(email="rep@example.com", name="Rita Moss", role=Role.FIELD_REP, account_types=["funding"]), _request(), db, current=_actor())
    assert invite.await_args.kwargs["redirect_url"] == "https://r/sign-in"
    assert invite.await_args.kwargs["account_types"] == ["audit", "field_desk", "funding"]
    assert invite.await_args.kwargs["account_status"] == "active"
    assert sync.await_count == 0  # not bound to a Clerk id yet; the invitation carries the metadata
    assert rec.call_args.kwargs["action"] == "team_access.invited" and rec.call_args.kwargs["before_state"] is None
    assert out.inherited_account_types == ["audit", "field_desk"]


@pytest.mark.asyncio
async def test_console_grants_are_refused_where_the_role_does_not_allow_them():
    db = _db(_staffer(role=Role.DEALER_PARTNER))
    with pytest.raises(HTTPException) as err:
        await users_router.update_user(uuid4(), users_router.UserPatch(account_types=["funding"]), _request(), db, current=_actor())
    assert err.value.status_code == 422 and "not available" in err.value.detail
    db = _db(_staffer(role=Role.BROKER))
    with pytest.raises(HTTPException) as err:
        await users_router.update_user(uuid4(), users_router.UserPatch(account_types=["audit"]), _request(), db, current=_actor())
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        await users_router.update_user(uuid4(), users_router.UserPatch(account_types=["nope"]), _request(), db, current=_actor())
    assert err.value.status_code == 422 and "Unknown" in err.value.detail
    # Echoing an inherited key back is fine — the Team table always did.
    with patch.object(users_router.clerk_service, "update_user_access_metadata", AsyncMock()), \
         patch.object(users_router, "record_access_event"), \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        out = await users_router.update_user(uuid4(), users_router.UserPatch(account_types=["funding", "field_desk"]), _request(), db, current=_actor())
    assert out.account_types == ["audit", "field_desk", "funding"]


def test_no_console_vocabulary_is_left_in_the_router():
    import inspect

    assert "_ACCOUNT_ACCESS_TYPES" not in inspect.getsource(users_router)
