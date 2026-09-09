"""Deleting an AI intake is the desk's: a super admin or an underwriter,
nobody else. The three deletion routes on the admin lead family share one
gate, and that gate is the intake-operator one, not the governance-only one
(which still guards creating a lead and editing its contact)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import dealer_ai_intake as intake


def _user(role: Role):
    return SimpleNamespace(role=role, id="u", email="u@example.com")


def test_the_desk_may_delete_and_nobody_else():
    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        intake._require_intake_operator(_user(role))
    for role in (Role.REGIONAL_MANAGER, Role.BROKER, Role.FIELD_REP, Role.DEALER_PARTNER, Role.DEALER, Role.CLIENT, Role.LENDER, Role.VENDOR):
        with pytest.raises(HTTPException) as err:
            intake._require_intake_operator(_user(role))
        assert err.value.status_code == 403, role


def test_the_three_deletion_routes_share_the_desk_gate():
    for handler in (intake.admin_request_lead_deletion, intake.admin_cancel_lead_deletion, intake.admin_confirm_lead_deletion):
        source = inspect.getsource(handler)
        assert "_require_intake_operator(user)" in source, handler.__name__
        assert "_require_governance_admin(user)" not in source, handler.__name__
    # Creating a lead stays governance-only: widening delete widened nothing else.
    assert "_require_governance_admin(user)" in inspect.getsource(intake.create_admin_ai_lead)


def test_the_confirm_route_is_registered_on_the_admin_family():
    contract = {(r.path, m) for r in intake.admin_router.routes for m in getattr(r, "methods", set())}
    assert any(path.endswith("/{intake_id}/confirm-deletion") and m == "POST" for path, m in contract)
