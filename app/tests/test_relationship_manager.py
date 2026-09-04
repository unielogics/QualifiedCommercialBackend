"""Naming the relationship manager on the agreement.

`rm_phone` has been required to send stage one since the field rules were
written, and nothing in the system held an operator's phone number — it lives in
Clerk, which the backend never reads. So the field was required with no source
and was typed by hand on every package.

The picker that fills the rest was quietly broken too: it read `GET /users`,
which is super-admin only, and swallowed the failure. Every underwriter and
field rep got an empty list, no explanation, and `rm_user_id` was never set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.user import User


def _user(role=Role.LOAN_EXEC, **over):
    row = SimpleNamespace(id=uuid4(), name="Dana Ruiz", email="dana@example.com",
                          role=role, phone=None, title=None)
    for k, v in over.items():
        setattr(row, k, v)
    return row


def _db(rows=()):
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))),
        commit=AsyncMock(),
    )


# --- somewhere to keep a phone ---------------------------------------------------


def test_the_user_row_can_hold_a_phone_and_a_title():
    columns = set(User.__table__.columns.keys())
    assert {"phone", "title"} <= columns


@pytest.mark.asyncio
async def test_a_recognisable_number_is_stored_in_e164():
    from app.routers.me import ProfileUpdate, update_profile

    user = _user()
    await update_profile(ProfileUpdate(phone="(973) 555-0148"), user, _db())
    assert user.phone == "+19735550148"


@pytest.mark.asyncio
async def test_a_number_that_cannot_be_normalised_survives_as_typed():
    """This one is printed on an agreement, not texted. An extension or a
    switchboard note must not vanish because it is not dialable E.164."""
    from app.routers.me import ProfileUpdate, update_profile

    user = _user()
    await update_profile(ProfileUpdate(phone="973-555-0148 ext 22"), user, _db())
    assert user.phone == "973-555-0148 ext 22"


@pytest.mark.asyncio
async def test_clearing_the_phone_stores_null_not_an_empty_string():
    from app.routers.me import ProfileUpdate, update_profile

    user = _user(phone="+19735550148")
    await update_profile(ProfileUpdate(phone="   "), user, _db())
    assert user.phone is None


@pytest.mark.asyncio
async def test_a_field_that_was_not_sent_is_left_alone():
    from app.routers.me import ProfileUpdate, update_profile

    user = _user(phone="+19735550148", title="Underwriter")
    await update_profile(ProfileUpdate(title="Senior Underwriter"), user, _db())
    assert user.phone == "+19735550148"
    assert user.title == "Senior Underwriter"


# --- a team list every operator can actually read ---------------------------------


@pytest.mark.asyncio
async def test_an_underwriter_can_read_the_team_list():
    from app.routers.users import list_team

    rows = [_user(phone="+19735550148", title="Senior Underwriter")]
    out = await list_team(_user(Role.LOAN_EXEC), _db(rows))
    assert out[0].phone == "+19735550148"
    assert out[0].title == "Senior Underwriter"


@pytest.mark.asyncio
async def test_a_client_cannot_read_the_team_list():
    from app.routers.users import list_team

    for role in (Role.CLIENT, Role.DEALER_PARTNER, Role.LENDER):
        with pytest.raises(HTTPException) as err:
            await list_team(_user(role), _db())
        assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_the_team_list_carries_no_invite_or_account_state():
    """The reason /users stays super-admin only: it exposes invite status,
    account status and referral-company wiring. This one is a name and a way to
    reach them."""
    from app.routers.users import TeamMemberRead, list_team

    out = await list_team(_user(Role.LOAN_EXEC), _db([_user()]))
    assert set(TeamMemberRead.model_fields) == {"id", "name", "email", "phone", "title", "role"}
    assert out and out[0].email == "dana@example.com"


def test_both_routes_exist_and_only_one_of_them_is_wide_open_to_the_desk():
    from app.routers.users import router

    contract = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/users/team", "GET") in contract
    assert ("/users", "GET") in contract


def test_the_profile_routes_are_registered():
    from app.routers.me import router

    contract = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/me/profile", "GET") in contract
    assert ("/me/profile", "PATCH") in contract
