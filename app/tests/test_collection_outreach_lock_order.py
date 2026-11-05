"""Bucket before profile, in the order every other path takes the two row locks.

The outreach runner used to lock the profile first and reach the bucket through
readiness, the reverse. The page schedules it after every program-readiness
read, so a file opened at the same moment met it half-way and Postgres killed
one of the two as the loser of the lock cycle (a 500 or a 503 on the file).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services import missing_item_automation


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class _RecordingSession:
    """Remembers each statement as Postgres would receive it, and answers in order."""

    def __init__(self, *answers: _Result) -> None:
        self.sql: list[str] = []
        self._answers = list(answers)

    async def execute(self, statement):
        self.sql.append(" ".join(str(statement.compile(dialect=postgresql.dialect())).split()))
        return self._answers.pop(0) if self._answers else _Result()


def _locks(sql: list[str]) -> list[str]:
    """The table each locking statement takes its rows from, in the order taken."""

    tables = []
    for statement in sql:
        if "FOR UPDATE" not in statement:
            continue
        tables.append("buckets" if "FROM buckets" in statement else "application_profiles")
    return tables


async def test_the_runner_locks_the_bucket_before_the_profile(monkeypatch) -> None:
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    db = _RecordingSession(_Result(), _Result(scalar=profile))
    readiness = AsyncMock(return_value=SimpleNamespace(automation=SimpleNamespace(enabled=False)))
    monkeypatch.setattr(missing_item_automation, "get_program_readiness", readiness)

    assert await missing_item_automation.run_profile_collection_outreach(db, profile) is None

    assert _locks(db.sql) == ["buckets", "application_profiles"]
    readiness.assert_awaited_once()


async def test_a_profile_without_a_bucket_only_locks_itself(monkeypatch) -> None:
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=None)
    db = _RecordingSession(_Result(scalar=profile))
    monkeypatch.setattr(
        missing_item_automation,
        "get_program_readiness",
        AsyncMock(return_value=SimpleNamespace(automation=SimpleNamespace(enabled=False))),
    )

    await missing_item_automation.run_profile_collection_outreach(db, profile)

    assert _locks(db.sql) == ["application_profiles"]


async def test_the_scheduled_pass_locks_every_bucket_before_any_profile(monkeypatch) -> None:
    first = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    second = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    loose = SimpleNamespace(id=uuid4(), primary_bucket_id=None)
    candidates = [(row.id, row.primary_bucket_id) for row in (first, second, loose)]

    class _Rows:
        def all(self):
            return candidates

    db = _RecordingSession()
    db._answers = [_Rows(), _Result(), _Result(rows=[first, second, loose])]
    outreach = AsyncMock(return_value=None)
    monkeypatch.setattr(missing_item_automation, "run_profile_collection_outreach", outreach)

    stats = await missing_item_automation.run_missing_item_email_pass(db)

    assert _locks(db.sql) == ["buckets", "application_profiles"]
    assert stats == {"considered": 3, "sent": 0, "failed": 0, "skipped": 3}
    assert outreach.await_count == 3


async def test_the_scheduled_pass_takes_no_bucket_lock_when_nothing_is_due(monkeypatch) -> None:
    class _NoRows:
        def all(self):
            return []

    db = _RecordingSession()
    db._answers = [_NoRows()]
    monkeypatch.setattr(missing_item_automation, "run_profile_collection_outreach", AsyncMock())

    stats = await missing_item_automation.run_missing_item_email_pass(db)

    assert db.sql and _locks(db.sql) == []
    assert stats == {"considered": 0, "sent": 0, "failed": 0, "skipped": 0}
