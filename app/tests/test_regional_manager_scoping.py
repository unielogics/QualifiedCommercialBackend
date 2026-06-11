from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.enums import Role
from app.models.client import Client
from app.models.loan import Loan
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
