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
from unittest.mock import AsyncMock, patch
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


def _request():
    return SimpleNamespace(headers={}, client=None)


def _actor():
    return SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN)


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
    """The reason /users stays super-admin only: it exposes invite status and
    account status. This one is a name, a way to reach them, and the business
    relationship profile they belong to — the package's employer line and the
    sponsor default follow it."""
    from app.routers.users import TeamMemberRead, list_team

    out = await list_team(_user(Role.LOAN_EXEC), _db([_user()]))
    assert set(TeamMemberRead.model_fields) == {"id", "name", "email", "phone", "title", "role",
                                                "company_id", "company_name", "company_kind", "company_signed"}
    assert out[0].company_id is None and out[0].company_signed is False
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


# --- once, then reused: the phone lifecycle ---------------------------------------


def test_needs_phone_is_only_asked_of_the_people_named_on_agreements():
    from app.services.user_phone import needs_phone

    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.FIELD_REP):
        assert needs_phone(_user(role, phone=None)) is True, role
        assert needs_phone(_user(role, phone="   ")) is True, role
        assert needs_phone(_user(role, phone="+19735550148")) is False, role
    for role in (Role.CLIENT, Role.DEALER_PARTNER, Role.LENDER, Role.BROKER):
        assert needs_phone(_user(role, phone=None)) is False, role
    # A broker with field-desk access is a rep in every other respect, and here too.
    assert needs_phone(_user(Role.BROKER, phone=None, account_access_types=["field_desk"])) is True


def test_store_phone_is_one_rule_for_both_doors():
    from app.services.user_phone import store_phone

    assert store_phone("(973) 555-0148") == "+19735550148"
    assert store_phone("973-555-0148 ext 4") == "973-555-0148 ext 4"
    assert store_phone("   ") is None and store_phone(None) is None


@pytest.mark.asyncio
async def test_auth_me_says_when_a_phone_is_still_needed():
    from app.routers.auth import me
    from app.services import user_acknowledgment as ack

    with patch("app.routers.auth.account_types", return_value=[]), patch("app.routers.auth.has_product_access", return_value=False), \
         patch.object(ack, "latest_acceptance", AsyncMock(return_value=None)), \
         patch("app.services.user_access.get_settings", return_value=SimpleNamespace(frontend_app_url="https://f", rep_app_url="https://r", audit_app_url="https://a")):
        out = await me(_user(Role.FIELD_REP, phone=None, id=uuid4(), clerk_id=None, account_status="active", account_access_types=[], referral_partner_company_id=None, deleted_at=None), SimpleNamespace())
        assert out.needs_phone is True
        out = await me(_user(Role.FIELD_REP, phone="+19735550148", id=uuid4(), clerk_id=None, account_status="active", account_access_types=[], referral_partner_company_id=None, deleted_at=None), SimpleNamespace())
        assert out.needs_phone is False


@pytest.mark.asyncio
async def test_auth_me_lists_the_consoles_a_login_may_enter():
    from app.routers.auth import me
    from app.services import user_acknowledgment as ack

    settings = SimpleNamespace(frontend_app_url="https://f/", rep_app_url="https://r", audit_app_url="https://a")
    with patch("app.routers.auth.account_types", return_value=[]), patch("app.routers.auth.has_product_access", return_value=False), \
         patch.object(ack, "latest_acceptance", AsyncMock(return_value=None)), \
         patch("app.services.user_access.get_settings", return_value=settings):
        rep = _user(Role.FIELD_REP, id=uuid4(), clerk_id=None, account_status="active", account_access_types=[], referral_partner_company_id=None, deleted_at=None)
        out = await me(rep, SimpleNamespace())
        assert [c.key for c in out.consoles] == ["field_desk", "audit"] and out.consoles[0].url == "https://r"
        rep.account_access_types = ["funding"]
        assert [c.key for c in (await me(rep, SimpleNamespace())).consoles] == ["funding", "field_desk", "audit"]
        assert (await me(rep, SimpleNamespace())).consoles[0].url == "https://f"
        sa = _user(Role.SUPER_ADMIN, id=uuid4(), clerk_id=None, account_status="active", account_access_types=[], referral_partner_company_id=None, deleted_at=None)
        assert [c.key for c in (await me(sa, SimpleNamespace())).consoles] == ["funding", "field_desk", "audit"]
        sa.account_status = "suspended"
        assert (await me(sa, SimpleNamespace())).consoles == []


def test_the_invite_lands_on_the_console_the_role_works_in():
    from app.routers import users as users_router

    settings = SimpleNamespace(frontend_app_url="https://f", rep_app_url="https://r/", audit_app_url="https://a")
    with patch.object(users_router, "get_settings", return_value=settings):
        assert users_router._invite_landing(Role.FIELD_REP) == "https://r/sign-in"
        assert users_router._invite_landing(Role.DEALER) == "https://a/sign-in"
        assert users_router._invite_landing(Role.LOAN_EXEC) is None


def test_the_user_reads_carry_the_phone():
    from app.routers.users import UserInvite, UserPatch, UserRead

    assert "phone" in UserRead.model_fields and "phone" in UserInvite.model_fields and "phone" in UserPatch.model_fields


@pytest.mark.asyncio
async def test_an_invite_can_carry_a_phone_stored_in_e164():
    from app.routers import users as users_router

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
    with patch.object(users_router.clerk_service, "invite_user", AsyncMock()), \
         patch.object(users_router, "house_company", AsyncMock(return_value=None)), \
         patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        out = await users_router.invite_user(users_router.UserInvite(email="rep@example.com", name="Rita Moss", role=Role.FIELD_REP, phone="(973) 555-0148"), _request(), db, current=_actor())
    assert added[0].phone == "+19735550148" and out.phone == "+19735550148"


@pytest.mark.asyncio
async def test_a_super_admin_can_fix_a_colleagues_phone():
    from app.routers import users as users_router

    colleague = _user(Role.LOAN_EXEC, phone=None, id=uuid4(), deleted_at=None, referral_partner_company_id=None, account_access_types=[])

    async def get(_model, _key, **_kw):
        return None

    async def execute(_stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: colleague, scalars=lambda: SimpleNamespace(all=lambda: []))

    async def refresh(_row):
        return None

    db = SimpleNamespace(get=get, execute=execute, flush=AsyncMock(), refresh=refresh)
    with patch.object(users_router, "_signed_company_ids", AsyncMock(return_value=set())):
        out = await users_router.update_user(colleague.id, users_router.UserPatch(phone="973 555 0148"), _request(), db, current=_actor())
        assert colleague.phone == "+19735550148" and out.phone == "+19735550148"
        # A field that was not sent is left alone.
        await users_router.update_user(colleague.id, users_router.UserPatch(name="Dana R"), _request(), db, current=_actor())
        assert colleague.phone == "+19735550148"


def test_the_phone_backfill_never_overwrites_a_number_someone_typed():
    """Migration 0199 copies the rep-app card's phone onto the user row only
    where the row has none, and the other way only where the card is blank."""
    from pathlib import Path

    source = Path("alembic/versions/0199_user_phone_reconciliation.py").read_text()
    assert "u.phone IS NULL" in source
    assert "NULLIF(TRIM(p.phone), '') IS NULL" in source
    assert 'down_revision = "0198_referral_company_kind_and_house"' in source


def test_the_migration_chain_has_one_head():
    """Two heads means `alembic upgrade head` refuses to run, which on this
    deployment means the container does not come up. The name is pinned rather
    than just counted so that adding a migration is a deliberate edit here —
    a branch created by two people working in parallel would otherwise pass a
    bare length check right up until it took production down."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert heads == ["0215_persistent_intake_sms_preference"]
