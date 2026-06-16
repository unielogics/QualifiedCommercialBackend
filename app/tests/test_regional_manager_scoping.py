from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.enums import Role
from app.models.activity import Activity
from app.models.event import CalendarEvent
from app.models.client import Client
from app.models.loan import Loan
from app.routers.calendar import _scope_activity_for_audience, _scope_calendar_for_audience
from app.scoping import scope_client_query, scope_loan_query


def test_regional_manager_client_scope_uses_membership_not_global_fallthrough():
    user = SimpleNamespace(id=uuid4(), role=Role.REGIONAL_MANAGER)
    sql = str(scope_client_query(user, select(Client)).compile(compile_kwargs={"literal_binds": False}))

    assert "regional_manager_agents" in sql
    assert "brokers" in sql
    assert "clients.broker_id IN" in sql


def test_regional_manager_loan_scope_uses_membership_not_global_fallthrough():
    user = SimpleNamespace(id=uuid4(), role=Role.REGIONAL_MANAGER)
    sql = str(scope_loan_query(user, select(Loan)).compile(compile_kwargs={"literal_binds": False}))

    assert "regional_manager_agents" in sql
    assert "brokers" in sql
    assert "loans.broker_id IN" in sql


def test_regional_manager_calendar_scope_uses_membership_not_global_fallthrough():
    user = SimpleNamespace(id=uuid4(), role=Role.REGIONAL_MANAGER)
    sql = str(_scope_calendar_for_audience(user, select(CalendarEvent)).compile(compile_kwargs={"literal_binds": False}))

    assert "regional_manager_agents" in sql
    assert "brokers" in sql
    assert "calendar_events.loan_id IN" in sql


def test_client_activity_scope_uses_client_and_loan_filters():
    client_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role=Role.CLIENT, client=SimpleNamespace(id=client_id))
    sql = str(_scope_activity_for_audience(user, select(Activity)).compile(compile_kwargs={"literal_binds": False}))

    assert "activities.client_id" in sql
    assert "activities.loan_id IN" in sql
    assert "loans.client_id" in sql
