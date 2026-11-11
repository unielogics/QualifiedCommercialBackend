"""The automatic sweep isolates failures and continues fairly across listings."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.capital_partners.services import ppm, ppm_refresh


class Session:
    def __init__(self):
        self.commit, self.rollback = AsyncMock(), AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(ppm_refresh, "_cursor", None)
    monkeypatch.setattr(ppm_refresh, "_retry_after", {})
    monkeypatch.setattr(ppm_refresh, "_lock", asyncio.Lock())
    sessions = []

    def factory():
        value = Session()
        sessions.append(value)
        return value

    return SimpleNamespace(factory=factory, sessions=sessions)


@pytest.mark.asyncio
async def test_failed_listing_rolls_back_alone_and_has_backoff_while_next_listing_commits(worker, monkeypatch, caplog):
    first, second = UUID(int=1), UUID(int=2)
    monkeypatch.setattr(ppm, "adopted_listing_ids", AsyncMock(return_value=[first, second]))
    refresh = AsyncMock(side_effect=[RuntimeError("private source text"), {"status": "refreshed"}, {"status": "unchanged"}])
    monkeypatch.setattr(ppm, "refresh_managed_listing", refresh)
    assert await ppm_refresh.drain_pending(session_factory=worker.factory) == {"checked": 2, "refreshed": 1, "failed": 1}
    worker.sessions[1].rollback.assert_awaited_once()
    worker.sessions[1].commit.assert_not_awaited()
    worker.sessions[2].commit.assert_awaited_once()
    assert "private source text" not in caplog.text
    assert ppm_refresh._cursor == second
    assert await ppm_refresh.drain_pending(session_factory=worker.factory) == {"checked": 1, "refreshed": 0, "failed": 0}
    assert refresh.await_count == 3
    assert refresh.await_args.args[1] == second


@pytest.mark.asyncio
async def test_cursor_wraps_after_end_and_simultaneous_ticks_do_not_duplicate_generation(worker, monkeypatch):
    item = UUID(int=3)
    monkeypatch.setattr(ppm_refresh, "_cursor", UUID(int=9))
    page = AsyncMock(side_effect=[[], [item]])
    monkeypatch.setattr(ppm, "adopted_listing_ids", page)
    started, release = asyncio.Event(), asyncio.Event()

    async def refresh(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {"status": "refreshed"}

    monkeypatch.setattr(ppm, "refresh_managed_listing", refresh)
    task = asyncio.create_task(ppm_refresh.drain_pending(session_factory=worker.factory))
    await started.wait()
    assert await ppm_refresh.drain_pending(session_factory=worker.factory) == {"checked": 0, "refreshed": 0, "failed": 0}
    release.set()
    assert await task == {"checked": 1, "refreshed": 1, "failed": 0}
    assert [call.kwargs["after_id"] for call in page.await_args_list] == [UUID(int=9), None]
