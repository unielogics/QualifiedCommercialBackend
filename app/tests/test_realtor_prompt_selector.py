"""Selector tests for `_system_prompt_for(user, thread)`.

Pure-Python — no DB. Locks down which AI persona the orchestrator
runs under for each (role, thread-scope) combination.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.enums import Role
from app.routers.ai import (
    CLIENT_SYSTEM_PROMPT,
    OPERATOR_SYSTEM_PROMPT,
    REALTOR_SYSTEM_PROMPT,
    _system_prompt_for,
)


@dataclass
class FakeUser:
    role: Role


@dataclass
class FakeThread:
    loan_id: object | None = None
    client_id: object | None = None
    # alembic 0031 — selector reads thread.phase. Default None so
    # legacy thread shapes still type-check.
    phase: str | None = None


# ── Borrowers (CLIENT role) ──────────────────────────────────────────


def test_borrower_account_wide_thread() -> None:
    assert _system_prompt_for(FakeUser(Role.CLIENT), None) == CLIENT_SYSTEM_PROMPT


def test_borrower_loan_scoped_thread() -> None:
    t = FakeThread(loan_id=uuid.uuid4())
    assert _system_prompt_for(FakeUser(Role.CLIENT), t) == CLIENT_SYSTEM_PROMPT


# ── Agents (BROKER role) ────────────────────────────────────────────


def test_agent_account_wide_thread_runs_realtor() -> None:
    """Agent's Elara (no thread scope) runs as Realtor AI by default."""
    assert _system_prompt_for(FakeUser(Role.BROKER), None) == REALTOR_SYSTEM_PROMPT


def test_agent_client_scoped_thread_runs_realtor() -> None:
    """Per-client thread always runs Realtor AI — the relationship
    surface, not the loan surface."""
    t = FakeThread(client_id=uuid.uuid4())
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == REALTOR_SYSTEM_PROMPT


def test_agent_loan_scoped_thread_runs_bank_ai() -> None:
    """Once a loan exists and the agent opens its thread, we're in
    the lending phase. Bank AI takes over."""
    t = FakeThread(loan_id=uuid.uuid4())
    assert _system_prompt_for(FakeUser(Role.BROKER), t) == OPERATOR_SYSTEM_PROMPT


# ── Super-admin / underwriter ───────────────────────────────────────


def test_super_admin_account_wide_runs_bank_ai() -> None:
    """Super-admin / underwriter always run as Bank AI — they don't
    do realtor relationship work."""
    assert _system_prompt_for(FakeUser(Role.SUPER_ADMIN), None) == OPERATOR_SYSTEM_PROMPT


def test_super_admin_loan_scoped_runs_bank_ai() -> None:
    t = FakeThread(loan_id=uuid.uuid4())
    assert _system_prompt_for(FakeUser(Role.SUPER_ADMIN), t) == OPERATOR_SYSTEM_PROMPT


def test_loan_exec_runs_bank_ai_everywhere() -> None:
    assert _system_prompt_for(FakeUser(Role.LOAN_EXEC), None) == OPERATOR_SYSTEM_PROMPT
    t = FakeThread(loan_id=uuid.uuid4())
    assert _system_prompt_for(FakeUser(Role.LOAN_EXEC), t) == OPERATOR_SYSTEM_PROMPT


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
