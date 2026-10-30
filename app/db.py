"""Async SQLAlchemy session + Base."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Single declarative base for all ORM models."""


_settings = get_settings()
engine = create_async_engine(_settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Postgres settles a lock cycle, or a serialization anomaly, by aborting one of
# the transactions involved. The aborted work was not wrong, it was merely
# second: run again once the other transaction has finished, it succeeds.
TRANSIENT_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})  # deadlock detected, serialization failure
_TRANSIENT_CONFLICT_NAMES = {"DeadlockDetectedError": "40P01", "SerializationError": "40001"}


def transient_conflict_code(exc: BaseException) -> str | None:
    """The SQLSTATE when ``exc`` is a conflict Postgres aborted, else None.

    SQLAlchemy wraps the driver's error twice — ``DBAPIError`` → adapter error
    carrying ``pgcode`` → asyncpg error carrying ``sqlstate`` — so this walks
    the chain rather than trusting any one layer.
    """

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "pgcode", None) or getattr(current, "sqlstate", None)
        if code in TRANSIENT_CONFLICT_SQLSTATES:
            return str(code)
        by_name = _TRANSIENT_CONFLICT_NAMES.get(type(current).__name__)
        if by_name is not None:
            return by_name
        current = getattr(current, "orig", None) or current.__cause__ or current.__context__
    return None


def is_transient_conflict(exc: BaseException) -> bool:
    return transient_conflict_code(exc) is not None


async def retry_transient_conflicts[T](
    db: AsyncSession,
    work: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``work``; when Postgres aborted it for a deadlock or serialization
    failure, roll the session back and run it again, up to ``attempts`` times.

    ``work`` must own its whole unit — reload what it needs and commit when it
    is done — because the rollback discards every pending change on ``db``.
    Any other error, and the last conflict, propagate unchanged.
    """

    for attempt in range(1, attempts + 1):
        try:
            return await work()
        except DBAPIError as exc:
            code = transient_conflict_code(exc)
            if code is None or attempt == attempts:
                raise
            await db.rollback()
            delay = 0.05 * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
            log.warning(
                "%s: transaction aborted (SQLSTATE %s), attempt %d of %d; retrying in %d ms",
                label, code, attempt, attempts, round(delay * 1000),
            )
            await sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")
