"""Shared role-aware query scopes.

Each helper takes a `user` (CurrentUser-ish, with `.role`, `.client`, `.broker`)
and a SQLAlchemy `Select` statement, and returns the statement with an
appropriate `WHERE` clause appended.

Defense-in-depth: when role is CLIENT or BROKER but the linked record
(``user.client`` / ``user.broker``) is missing, the helpers return
``where(false())`` so the gate never silently falls through to "see
everything". Without this an orphaned broker user (no Broker row) was
historically able to see every record in the firm.

Phase 2 introduces this module and migrates the two existing helpers
(``_scope_query`` in routers/loans.py, ``_scope`` in routers/clients.py)
to live here. Phase 3+ adds deal/agent_task scopes as those models
land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import false as sql_false

from app.enums import Role
from app.models.client import Client
from app.models.loan import Loan

if TYPE_CHECKING:
    from sqlalchemy.sql import Select


def scope_client_query(user, stmt: "Select") -> "Select":
    """Scope a `select(Client)` to rows the calling user may see."""
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(sql_false())
        return stmt.where(Client.id == user.client.id)
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(sql_false())
        return stmt.where(Client.broker_id == user.broker.id)
    return stmt


def scope_loan_query(user, stmt: "Select") -> "Select":
    """Scope a `select(Loan)` to rows the calling user may see.

    SUPER_ADMIN and LOAN_EXEC see everything; BROKER is restricted to
    their assigned loans; CLIENT is restricted to their own loans.
    """
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(sql_false())
        return stmt.where(Loan.client_id == user.client.id)
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(sql_false())
        return stmt.where(Loan.broker_id == user.broker.id)
    return stmt


# Funding files are stored on the Loan table (Loan IS the FundingFile —
# see Phase 4 plan). Visibility is identical to scope_loan_query, so we
# alias rather than duplicate.
scope_funding_query = scope_loan_query


__all__ = [
    "scope_client_query",
    "scope_loan_query",
    "scope_funding_query",
]
