from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services import credit_links, scheduler
from app.services import credit_report_import as reports


def pull(score=None):
    return SimpleNamespace(id=uuid4(), client_id=uuid4(), application_owner_id=None,
        status="completed", fico=score, notes=None, bureau_response={},
        pulled_at=datetime(2026, 11, 1, tzinfo=UTC), expires_at=None)


@pytest.mark.parametrize("how", ["manual", "automatic", "signature", "sweep"])
@pytest.mark.parametrize("score", [661, 735])
async def test_every_linking_method_imports_the_correct_clients_report(monkeypatch, how, score):
    row = pull()
    owner = SimpleNamespace(id=uuid4(), profile_id=uuid4(), credit_pull_id=None,
        full_name="Example owner", credit_summary=None)
    profile = SimpleNamespace(id=owner.profile_id, intake_id=None)
    db = SimpleNamespace(get=AsyncMock(), flush=AsyncMock())

    async def import_existing(db_arg, pull_arg):
        assert db_arg is db and pull_arg is row
        row.fico = score
        row.notes = "fico_source=scraped:fico_4"
        row.bureau_response = {"parsed_report": {"scores": [{"score": score}]}, "report_import_status": "completed"}

    imported = AsyncMock(side_effect=import_existing)
    monkeypatch.setattr(reports, "ensure_report_imported", imported)
    monkeypatch.setattr(credit_links, "_record", AsyncMock())
    monkeypatch.setattr(credit_links, "_progress", AsyncMock())
    await credit_links.link_pull(db, profile, owner, row, how=how)
    imported.assert_awaited_once_with(db, row)
    assert owner.credit_pull_id == row.id
    assert owner.credit_score == score
    assert owner.credit_summary["score_verified"] is True
    assert owner.credit_summary["report_available"] is True
    assert owner.credit_summary["linked_how"] == how
    assert owner.credit_summary["report_import_status"] == "completed"


async def test_provider_failure_records_a_retry_without_erasing_a_real_score(monkeypatch):
    row = pull(680)
    original_date = row.pulled_at
    monkeypatch.setattr(reports, "import_existing_report", AsyncMock(side_effect=reports.ReportImportUnavailable("Provider temporarily unavailable")))
    sync = AsyncMock()
    monkeypatch.setattr(reports, "sync_linked_owners", sync)
    db = SimpleNamespace(flush=AsyncMock())
    assert await reports.ensure_report_imported(db, row) is None
    assert row.fico == 680 and row.pulled_at == original_date
    assert row.bureau_response["report_import_status"] == "pending"
    attempted = datetime.fromisoformat(row.bureau_response["report_import_attempted_at"])
    retry = datetime.fromisoformat(row.bureau_response["report_import_next_retry_at"])
    assert (retry - attempted).total_seconds() == 300
    assert row.bureau_response["report_import_attempts"] == 1
    sync.assert_awaited_once_with(db, row)
    await reports.ensure_report_imported(db, row)
    assert row.bureau_response["report_import_attempts"] == 2
    attempted = datetime.fromisoformat(row.bureau_response["report_import_attempted_at"])
    retry = datetime.fromisoformat(row.bureau_response["report_import_next_retry_at"])
    assert (retry - attempted).total_seconds() == 600


def session(**attrs):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    for name, value in attrs.items():
        setattr(db, name, value)
    return db


async def test_retry_backfills_linked_clients_even_when_matching_is_manual(monkeypatch):
    first, second = pull(), pull()
    selected = session(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [first.id, second.id])))
    first_db = session(scalar=AsyncMock(return_value=first))
    second_db = session(scalar=AsyncMock(return_value=second))
    sessions = [selected, first_db, second_db]
    monkeypatch.setattr("app.db.SessionLocal", lambda: sessions.pop(0))
    mode = AsyncMock(side_effect=AssertionError("Report imports must not depend on automatic matching mode"))
    monkeypatch.setattr(credit_links, "mode", mode)
    imported = AsyncMock(return_value={"scores": []})
    monkeypatch.setattr(reports, "ensure_report_imported", imported)
    result = await reports.run_pending_report_imports()
    assert result == {"checked": 2, "imported": 2, "pending": 0, "failed": 0}
    assert imported.await_count == 2
    first_db.commit.assert_awaited_once()
    second_db.commit.assert_awaited_once()
    mode.assert_not_awaited()
    query = str(selected.scalars.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "application_owners.credit_pull_id = credit_pulls.id" in query
    assert "dos_owners.credit_pull_id = credit_pulls.id" in query
    assert "public_underwriting_intakes.intake_state" in query
    assert "CAST(credit_pulls.id AS VARCHAR)" in query
    assert "credit_pulls.status = 'completed'" in query
    assert "report_import_next_retry_at" in query and "LIMIT 20" in query
    locked = str(first_db.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "SKIP LOCKED" in locked


async def test_retry_skips_an_already_completed_concurrent_import(monkeypatch):
    row = pull()
    row.bureau_response = {"report_import_version": reports.IMPORT_VERSION, "report_import_status": "completed"}
    sessions = [session(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [row.id]))), session(scalar=AsyncMock(return_value=row))]
    monkeypatch.setattr("app.db.SessionLocal", lambda: sessions.pop(0))
    imported = AsyncMock()
    monkeypatch.setattr(reports, "ensure_report_imported", imported)
    result = await reports.run_pending_report_imports()
    assert result["checked"] == 0
    imported.assert_not_awaited()


async def test_retry_respects_a_concurrently_rescheduled_attempt(monkeypatch):
    row = pull()
    row.bureau_response = {"report_import_status": "pending", "report_import_next_retry_at": "2999-01-01T00:00:00+00:00"}
    sessions = [session(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [row.id]))), session(scalar=AsyncMock(return_value=row))]
    monkeypatch.setattr("app.db.SessionLocal", lambda: sessions.pop(0))
    imported = AsyncMock()
    monkeypatch.setattr(reports, "ensure_report_imported", imported)
    result = await reports.run_pending_report_imports()
    assert result["checked"] == 0
    imported.assert_not_awaited()


async def test_one_clients_failure_does_not_abort_the_next_import(monkeypatch):
    first, second = pull(), pull()
    sessions = [session(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [first.id, second.id]))),
        session(scalar=AsyncMock(return_value=first)), session(scalar=AsyncMock(return_value=second))]
    monkeypatch.setattr("app.db.SessionLocal", lambda: sessions.pop(0))
    imported = AsyncMock(side_effect=[RuntimeError("Example transaction error"), {"scores": []}])
    monkeypatch.setattr(reports, "ensure_report_imported", imported)
    result = await reports.run_pending_report_imports()
    assert result["failed"] == 1 and result["imported"] == 1


def test_report_retry_is_scheduled_separately_from_automatic_matching():
    import inspect

    source = inspect.getsource(scheduler.start_scheduler)
    assert 'id="credit_report_imports"' in source
    assert "run_pending_report_imports" in inspect.getsource(scheduler.job_credit_report_imports)
