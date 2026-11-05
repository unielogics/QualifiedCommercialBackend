"""Postgres settles a lock cycle by aborting one transaction. The aborted work
is run again, other failures are not, and a conflict that outlives the retries
answers 503 with Retry-After instead of a 500."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import String, delete
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import is_transient_conflict, retry_transient_conflicts, transient_conflict_code


class DeadlockDetectedError(Exception):
    """Shaped like asyncpg's: the SQLSTATE rides on the instance."""

    sqlstate = "40P01"


def _deadlock(*, code_on_adapter: bool = True) -> DBAPIError:
    driver = DeadlockDetectedError("deadlock detected")
    adapter = Exception("<class 'asyncpg.exceptions.DeadlockDetectedError'>: deadlock detected")
    if code_on_adapter:
        adapter.pgcode = "40P01"  # type: ignore[attr-defined]
    adapter.__cause__ = driver
    return DBAPIError("SELECT buckets.id FROM buckets FOR UPDATE", {}, adapter)


def _unique_violation() -> DBAPIError:
    adapter = Exception("duplicate key value violates unique constraint")
    adapter.pgcode = "23505"  # type: ignore[attr-defined]
    return DBAPIError("INSERT INTO x", {}, adapter)


def test_a_conflict_is_recognised_at_any_layer_of_sqlalchemys_wrapping() -> None:
    assert transient_conflict_code(_deadlock()) == "40P01"
    assert transient_conflict_code(_deadlock(code_on_adapter=False)) == "40P01"
    assert transient_conflict_code(_unique_violation()) is None
    assert not is_transient_conflict(ValueError("not a database error"))


async def test_the_aborted_work_runs_again_after_a_rollback() -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    sleep = AsyncMock()
    outcomes = [_deadlock(), "done"]

    async def work() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert await retry_transient_conflicts(db, work, label="t", sleep=sleep) == "done"
    db.rollback.assert_awaited_once()
    sleep.assert_awaited_once()
    assert 0.05 <= sleep.await_args.args[0] < 0.2


async def test_other_database_errors_are_not_retried() -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    work = AsyncMock(side_effect=_unique_violation())
    with pytest.raises(DBAPIError):
        await retry_transient_conflicts(db, work, label="t", sleep=AsyncMock())
    assert work.await_count == 1
    db.rollback.assert_not_awaited()


async def test_it_gives_up_after_the_last_attempt() -> None:
    db = SimpleNamespace(rollback=AsyncMock())
    work = AsyncMock(side_effect=_deadlock())
    with pytest.raises(DBAPIError):
        await retry_transient_conflicts(db, work, label="t", attempts=3, sleep=AsyncMock())
    assert work.await_count == 3
    assert db.rollback.await_count == 2


async def test_a_conflict_that_outlives_the_retries_answers_503_and_anything_else_stays_a_500() -> None:
    from app.main import database_conflict_handler

    request = SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/v1/x"))
    response = await database_conflict_handler(request, _deadlock())
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    with pytest.raises(DBAPIError):
        await database_conflict_handler(request, _unique_violation())


class _Base(DeclarativeBase):
    pass


class _Caller(_Base):
    """Stands in for the signed-in user: loaded before the route's work starts."""

    __tablename__ = "retry_callers"

    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


@asynccontextmanager
async def _session_holding_the_caller() -> AsyncIterator[tuple[AsyncSession, _Caller]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        db.add(_Caller(id=1, role="super_admin"))
        await db.commit()
        caller = await db.get(_Caller, 1)  # what the sign-in dependency hands the route
        assert caller is not None
        yield db, caller
    await engine.dispose()


async def test_the_caller_can_still_be_read_when_the_retry_runs() -> None:
    """A rollback expires every loaded instance, and an async session can only
    reload one inside an await. The visibility check reads ``user.role`` as a
    plain attribute, so the second attempt raised MissingGreenlet and a deadlock
    the retry had healed still answered 500 (seen on evidence-workspace)."""

    async with _session_holding_the_caller() as (db, caller):
        roles: list[str] = []

        async def work() -> str:
            roles.append(caller.role)
            if len(roles) == 1:
                raise _deadlock()
            return "done"

        assert await retry_transient_conflicts(db, work, label="t", sleep=AsyncMock()) == "done"
        assert roles == ["super_admin", "super_admin"]


async def test_a_caller_removed_while_waiting_does_not_break_the_retry() -> None:
    async with _session_holding_the_caller() as (db, caller):
        attempts = 0

        async def removed_meanwhile(_delay: float) -> None:
            await db.execute(delete(_Caller).where(_Caller.id == 1))
            await db.commit()

        async def work() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                assert caller.role == "super_admin"
                raise _deadlock()
            return "done"

        assert await retry_transient_conflicts(db, work, label="t", sleep=removed_meanwhile) == "done"
        assert attempts == 2
