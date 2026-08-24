from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os.router import _rep_production_access_scope
from app.enums import Role


def _user(role: Role):
    return SimpleNamespace(id=uuid4(), role=role)


def test_field_rep_production_is_scoped_to_owner() -> None:
    user = _user(Role.FIELD_REP)

    scope, owner_user_id = _rep_production_access_scope(user)

    assert scope == "own"
    assert owner_user_id == user.id


@pytest.mark.parametrize("role", [Role.LOAN_EXEC, Role.SUPER_ADMIN])
def test_team_production_is_firm_wide(role: Role) -> None:
    scope, owner_user_id = _rep_production_access_scope(_user(role))

    assert scope == "firm"
    assert owner_user_id is None


@pytest.mark.parametrize("role", [Role.DEALER, Role.CLIENT, Role.BROKER])
def test_non_field_desk_roles_cannot_read_production(role: Role) -> None:
    with pytest.raises(HTTPException) as exc:
        _rep_production_access_scope(_user(role))

    assert exc.value.status_code == 403
