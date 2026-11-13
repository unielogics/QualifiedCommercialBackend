"""PostgreSQL lock semantics and real ORM retry/rollback at the busy boundary."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital_partners.services import refresh
from app.services import marketplace_package_refresh as packages
from app.tests.test_marketplace_ppm_lifecycle import ppm_harness
from app.tests.test_marketplace_saved_refresh import reload, setup
from app.tests.test_ppm_saved_source_stability import real_sources  # noqa: F401


class Locked(Exception):
    sqlstate = "55P03"


def busy():
    return OperationalError("synthetic NOWAIT", {}, Locked("synthetic competing offering transaction"))


@pytest.mark.asyncio
async def test_source_to_listing_lock_uses_postgresql_nowait_and_source_free_lock_can_wait():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    await refresh._load(db, uuid4(), lock=True, nonblocking=True)
    assert str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect())).endswith("FOR UPDATE NOWAIT")
    await refresh._load(db, uuid4(), lock=True)
    assert str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect())).endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_saved_source_enqueue_skips_locked_offerings_instead_of_waiting():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    assert await refresh.queue_profile(db, uuid4()) == 0
    statement = db.execute.await_args.args[0]
    assert str(statement.compile(dialect=postgresql.dialect())).endswith("FOR UPDATE SKIP LOCKED")


@pytest.mark.parametrize("code,expected", [("55P03", True), ("40P01", False), ("23505", False), (None, False)])
def test_only_a_postgres_nowait_conflict_is_classified_as_retryable_busy(code, expected):
    original = Locked("synthetic")
    original.sqlstate = code
    assert refresh._lock_busy(OperationalError("statement", {}, original)) is expected


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_manual_busy_request_returns_clear409_without_overwriting_existing_intent(monkeypatch):
    async with ppm_harness() as h:
        listing, _, _, prepare, _ = await setup(h, monkeypatch)
        original = refresh._load

        async def unavailable(db, ident, **kw):
            if kw.get("nonblocking"):
                raise busy()
            return await original(db, ident, **kw)

        monkeypatch.setattr(refresh, "_load", unavailable)
        result = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={"idempotency_key": "busy"})
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "refresh_busy"
        assert "being reviewed" in result.json()["detail"]["message"]
        assert refresh.state(listing)["status"] == "idle"
        prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_worker_busy_after_render_rolls_back_discards_own_bytes_and_requeues(monkeypatch):
    async with ppm_harness() as h:
        listing, _, factory, prepare, discard = await setup(h, monkeypatch)
        queued = await refresh.request(h.db, listing, actor_id=h.current.id)
        await h.db.commit()
        original = AsyncSession.execute
        finish = refresh._finish_failure
        events = []

        async def discarded(_prepared):
            assert prepare.await_args.args[0].in_transaction() is False
            events.append("source_transaction_released_then_discard")

        async def after_rollback(*args):
            assert prepare.await_args.args[0].in_transaction() is False
            assert events == ["source_transaction_released_then_discard"]
            events.append("source_free_requeue")
            return await finish(*args)

        discard.side_effect = discarded
        monkeypatch.setattr(refresh, "_finish_failure", after_rollback)

        async def competing(db, statement, *args, **kw):
            lock = getattr(statement, "_for_update_arg", None)
            if lock is not None and lock.nowait:
                raise busy()
            return await original(db, statement, *args, **kw)

        monkeypatch.setattr(AsyncSession, "execute", competing)
        assert await refresh.run_one(factory, listing.id) == "skipped"
        current = await reload(h, listing)
        assert current["status"] == "queued" and current["job_id"] == queued["job_id"]
        assert current["error_code"] is None
        assert refresh._job(listing)["claim_token"] is None
        assert (refresh._date(refresh._job(listing)["due_at"]) - datetime.now(UTC)).total_seconds() > 35
        assert not any(row.kind == "investor_presentation" for row in listing.documents)
        prepare.assert_awaited_once()
        discard.assert_awaited_once()
        assert events == ["source_transaction_released_then_discard", "source_free_requeue"]
        packages.publish_prepared.assert_not_awaited()
        # A subsequent tick can complete the same durable job after the review.
        monkeypatch.setattr(AsyncSession, "execute", original)
        job = refresh._job(listing)
        job["due_at"] = datetime.now(UTC).isoformat()
        refresh._put(listing, job)
        await h.db.commit()
        assert await refresh.run_one(factory, listing.id) == "succeeded"
        assert (await reload(h, listing))["job_id"] == queued["job_id"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_recovery_skips_busy_listing_then_recovers_source_on_later_tick(monkeypatch):
    async with ppm_harness() as h:
        listing, _, factory, _, _ = await setup(h, monkeypatch)
        original = refresh._load

        async def unavailable(db, ident, **kw):
            if kw.get("nonblocking"):
                raise busy()
            return await original(db, ident, **kw)

        monkeypatch.setattr(refresh, "_load", unavailable)
        refresh._cursor = None
        await refresh.recover_sources(factory, limit=3)
        assert (await reload(h, listing))["status"] == "idle"
        monkeypatch.setattr(refresh, "_load", original)
        await refresh.recover_sources(factory, limit=3)
        assert (await reload(h, listing))["status"] == "queued"
