"""One document that cannot be analyzed must not stop the documents behind it.

From about 17:50 UTC on 2026-11-15 the analysis drain failed on every
one-minute pass for a day. A document's extracted entity type was a sentence,
longer than the 32 characters the file's field holds, so saving it failed.
That alone would have cost one document. What cost the queue was the handler:
after rolling back it named the file from a row the rollback had just expired,
which raised in turn, aborted the pass, and left the same document at the head
of the queue for the next one.

These tests pin the three things that were wrong: the write that did not fit,
the same write when the desk accepts the suggestion by hand, and a pass that
could not survive a failure. The drain runs against a real session
(app/tests/_sqlite_schema.py), because a session double never expires a row
and so could not show the fault.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis
from app.routers.application_profiles import _apply_reviewed_profile_fact
from app.services import application_profiles as profiles
from app.services import bucket_ai
from app.tests._sqlite_schema import sqlite_session, sqlite_template

NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)

#: What the document that stopped the queue said, in substance.
LONG_ENTITY_TYPE = "Limited liability company taxed as an S corporation"


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "analysis_drain")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        yield session


# ── the write that did not fit ──────────────────────────────────────────────


def test_the_field_limit_is_read_off_the_column() -> None:
    assert profiles.profile_field_limit("entity_type") == ApplicationProfile.__table__.c.entity_type.type.length == 32
    assert profiles.profile_field_limit("funding_category") == 64
    assert profiles.profile_field_limit("no_such_field") is None
    assert profiles.fits_profile_field("entity_type", "S corporation")
    assert profiles.fits_profile_field("entity_type", "x" * 32)
    assert not profiles.fits_profile_field("entity_type", "x" * 33)
    assert not profiles.fits_profile_field("entity_type", LONG_ENTITY_TYPE)
    # A field with no stated length holds whatever it is given.
    assert profiles.fits_profile_field("no_such_field", "x" * 500)


async def _capture(entity_type_value):
    """Run the capture for one document that states an entity type."""
    bucket_id = uuid.uuid4()
    profile = SimpleNamespace(
        id=uuid.uuid4(), primary_bucket_id=bucket_id, entity_type=None, extraction_reviewed_at=NOW
    )

    def rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                rows([uuid.uuid4()]),
                rows([profile]),
                rows([]),  # nothing accepted yet
                SimpleNamespace(scalar_one_or_none=lambda: None),  # this fact is new
            ]
        ),
        add=added.append,
        flush=AsyncMock(),
    )
    file = SimpleNamespace(id=uuid.uuid4(), bucket_id=bucket_id, statement_period=None)
    analysis = SimpleNamespace(
        id=uuid.uuid4(),
        classification="tax_return",
        analysis={"profile_facts": {"entity_type": {"value": entity_type_value, "confidence": 0.9}}, "key_facts": {}},
    )
    assert await profiles.capture_extracted_profile_facts(db, file=file, analysis=analysis) == [profile.id]
    return profile, added


async def test_a_legal_form_that_fits_goes_onto_the_file() -> None:
    profile, added = await _capture("  S   corporation ")
    assert profile.entity_type == "S corporation"
    assert [fact.field_key for fact in added] == ["entity_type"]


async def test_a_sentence_is_kept_as_a_suggestion_and_not_cut_to_fit() -> None:
    profile, added = await _capture(LONG_ENTITY_TYPE)
    # Nothing is written that the column would refuse, and nothing truncated:
    # half a legal form on the file would be a wrong one.
    assert profile.entity_type is None
    # The document's own words are still there for the desk, in full.
    assert [fact.value for fact in added] == [{"value": LONG_ENTITY_TYPE}]
    assert profile.extraction_reviewed_at is None


async def test_an_entity_type_already_on_the_file_is_left_alone() -> None:
    bucket_id = uuid.uuid4()
    profile = SimpleNamespace(
        id=uuid.uuid4(), primary_bucket_id=bucket_id, entity_type="LLC", extraction_reviewed_at=None
    )

    def rows(values):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            rows([uuid.uuid4()]), rows([profile]), rows([]), SimpleNamespace(scalar_one_or_none=lambda: None),
        ]),
        add=lambda _fact: None,
        flush=AsyncMock(),
    )
    analysis = SimpleNamespace(
        id=uuid.uuid4(), classification="tax_return",
        analysis={"profile_facts": {"entity_type": {"value": "Corporation"}}, "key_facts": {}},
    )
    await profiles.capture_extracted_profile_facts(
        db, file=SimpleNamespace(id=uuid.uuid4(), bucket_id=bucket_id, statement_period=None), analysis=analysis
    )
    assert profile.entity_type == "LLC"


async def test_accepting_a_suggestion_that_does_not_fit_is_refused_in_words() -> None:
    profile = SimpleNamespace(entity_type=None, funding_category=None)
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(HTTPException) as refused:
        await _apply_reviewed_profile_fact(None, profile, user, "entity_type", LONG_ENTITY_TYPE)

    assert refused.value.status_code == 422
    assert f"{len(LONG_ENTITY_TYPE)} characters" in refused.value.detail
    assert "holds 32" in refused.value.detail
    assert "Edit file classification" in refused.value.detail
    assert profile.entity_type is None

    await _apply_reviewed_profile_fact(None, profile, user, "entity_type", " S corporation ")
    assert profile.entity_type == "S corporation"
    with pytest.raises(HTTPException):
        await _apply_reviewed_profile_fact(None, profile, user, "funding_category", "x" * 65)
    await _apply_reviewed_profile_fact(None, profile, user, "funding_category", "x" * 64)
    assert profile.funding_category == "x" * 64


# ── a pass that survives a failure ──────────────────────────────────────────


async def _queued(db, name, minutes_ago):
    """One uploaded document waiting in the analysis queue."""
    bucket = Bucket(name=f"{name} room", ai_context={"review_type": "dealer"})
    db.add(bucket)
    await db.flush()
    file = BucketFile(bucket_id=bucket.id, file_name=f"{name}.pdf", s3_key=f"k/{name}.pdf", status="uploaded")
    db.add(file)
    await db.flush()
    placeholder = BucketFileAnalysis(
        bucket_file_id=file.id, bucket_id=bucket.id, content_hash=f"pending:{name}", status="pending"
    )
    placeholder.created_at = NOW - timedelta(minutes=minutes_ago)
    db.add(placeholder)
    await db.flush()
    return file.id, placeholder.id


async def _hash_of(db, placeholder_id) -> str:
    return (
        await db.execute(select(BucketFileAnalysis.content_hash).where(BucketFileAnalysis.id == placeholder_id))
    ).scalar_one()


def _analyzer(db, *, failing: set, calls: list, disturbs_session: bool = False):
    """Stands in for the model call: fails for the named documents, and files
    a finished analysis for the rest as the real one does.

    `disturbs_session` is what a real analysis does on the way out: it
    reconciles the room, and the rows the session held before it are no longer
    loaded afterwards.
    """

    async def analyze(session, file, **_kwargs):
        calls.append(file.id)
        if file.id in failing:
            # The real failure was a write the database refused at flush time.
            raise RuntimeError("value too long for type character varying(32)")
        session.add(
            BucketFileAnalysis(
                bucket_file_id=file.id, bucket_id=file.bucket_id, content_hash=f"sha:{file.id.hex[:16]}",
                status="completed", analysis_version=bucket_ai.CURRENT_FILE_ANALYSIS_VERSION,
            )
        )
        await session.flush()
        if disturbs_session:
            session.expire_all()
        return object()

    return analyze


async def test_a_document_that_cannot_be_analyzed_does_not_stop_the_ones_behind_it(db, monkeypatch, caplog) -> None:
    stuck_file, stuck_placeholder = await _queued(db, "stuck", minutes_ago=30)  # oldest: head of the queue
    next_file, next_placeholder = await _queued(db, "next", minutes_ago=20)
    last_file, last_placeholder = await _queued(db, "last", minutes_ago=10)
    await db.commit()
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={stuck_file}, calls=calls))

    with caplog.at_level(logging.ERROR, logger=bucket_ai.log.name):
        processed = await bucket_ai.drain_file_analyses(db, limit=3)

    # The pass finished, and the two documents behind the stuck one were done.
    assert processed == 2
    assert calls == [stuck_file, next_file, last_file]
    assert (await _hash_of(db, next_placeholder)).startswith("consumed:")
    assert (await _hash_of(db, last_placeholder)).startswith("consumed:")
    # The stuck one is still queued for the next pass, and the log names it.
    assert await _hash_of(db, stuck_placeholder) == "pending:stuck"
    failures = [record for record in caplog.records if "drain_file_analyses: failed" in record.getMessage()]
    assert len(failures) == 1
    assert str(stuck_file) in failures[0].getMessage()


async def test_a_backlog_is_worked_through_in_one_pass_not_one_document_a_minute(db, monkeypatch) -> None:
    """After the first fix went out the queue moved again, one document a
    minute: each pass analyzed the first document and then failed reaching for
    the second, whose file the first analysis had left unloaded. Nothing had
    shown it before because a pass rarely holds two documents; the day-long
    backlog made every pass hold five."""
    queued = [await _queued(db, f"doc{n}", minutes_ago=50 - n) for n in range(4)]
    await db.commit()
    calls: list = []
    monkeypatch.setattr(
        bucket_ai, "analyze_bucket_file", _analyzer(db, failing=set(), calls=calls, disturbs_session=True)
    )

    assert await bucket_ai.drain_file_analyses(db, limit=4) == 4

    assert calls == [file_id for file_id, _placeholder in queued]
    for _file_id, placeholder_id in queued:
        assert (await _hash_of(db, placeholder_id)).startswith("consumed:")


async def test_a_backfill_is_worked_through_in_one_pass_too(db, monkeypatch) -> None:
    bucket = Bucket(name="Legacy room", ai_context={})
    db.add(bucket)
    await db.flush()
    files = []
    for n in range(3):
        file = BucketFile(bucket_id=bucket.id, file_name=f"old{n}.pdf", s3_key=f"k/old{n}.pdf", status="uploaded")
        file.created_at = NOW - timedelta(days=3 - n)
        files.append(file)
    db.add_all(files)
    await db.commit()
    newest_first = [file.id for file in reversed(files)]
    calls: list = []
    monkeypatch.setattr(
        bucket_ai, "analyze_bucket_file", _analyzer(db, failing=set(), calls=calls, disturbs_session=True)
    )

    assert await bucket_ai.drain_file_analyses(db, limit=3) == 3

    assert calls == newest_first


async def test_a_failed_document_is_not_tried_a_second_time_in_the_same_pass(db, monkeypatch) -> None:
    stuck_file, _stuck_placeholder = await _queued(db, "stuck", minutes_ago=30)
    next_file, _next_placeholder = await _queued(db, "next", minutes_ago=20)
    await db.commit()
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={stuck_file}, calls=calls))

    # Room left in the pass after the queue: the backfill of documents with no
    # analysis runs, and the document that just failed is one of those.
    processed = await bucket_ai.drain_file_analyses(db, limit=5)

    assert processed == 1
    assert calls.count(stuck_file) == 1
    assert calls.count(next_file) == 1


async def test_every_document_failing_still_ends_the_pass_cleanly(db, monkeypatch) -> None:
    first, first_placeholder = await _queued(db, "first", minutes_ago=30)
    second, second_placeholder = await _queued(db, "second", minutes_ago=20)
    await db.commit()
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={first, second}, calls=calls))

    assert await bucket_ai.drain_file_analyses(db, limit=2) == 0

    assert calls == [first, second]
    assert await _hash_of(db, first_placeholder) == "pending:first"
    assert await _hash_of(db, second_placeholder) == "pending:second"


async def test_a_backfilled_document_that_fails_does_not_stop_the_next(db, monkeypatch) -> None:
    # No queue placeholders at all: two uploaded documents that were never analyzed.
    bucket = Bucket(name="Legacy room", ai_context={})
    db.add(bucket)
    await db.flush()
    older = BucketFile(bucket_id=bucket.id, file_name="older.pdf", s3_key="k/older.pdf", status="uploaded")
    newer = BucketFile(bucket_id=bucket.id, file_name="newer.pdf", s3_key="k/newer.pdf", status="uploaded")
    older.created_at, newer.created_at = NOW - timedelta(days=2), NOW - timedelta(days=1)
    db.add_all([older, newer])
    await db.commit()
    older_id, newer_id = older.id, newer.id
    calls: list = []
    # The backfill takes the newest first, so the failing one is reached first.
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={newer_id}, calls=calls))

    assert await bucket_ai.drain_file_analyses(db, limit=2) == 1

    assert calls == [newer_id, older_id]


# ── a document that keeps failing is given up on ────────────────────────────
#
# With the queue moving again, one thing was left: a document that fails for a
# reason that will not go away was started on again every minute, one model
# call each time, for as long as it stayed uploaded. Nothing recorded that an
# attempt had been made, so nothing could stop.


async def _uploaded(db, name, *, queue=True, content_hash=None, days_ago=0):
    """One uploaded document, queued for analysis the way an upload queues it."""
    bucket = Bucket(name=f"{name} room", ai_context={"review_type": "dealer"})
    db.add(bucket)
    await db.flush()
    file = BucketFile(
        bucket_id=bucket.id, file_name=f"{name}.pdf", s3_key=f"k/{name}.pdf", status="uploaded",
        content_hash=content_hash,
    )
    if days_ago:
        file.created_at = NOW - timedelta(days=days_ago)
    db.add(file)
    await db.flush()
    if queue:
        await bucket_ai.enqueue_file_analysis(db, file)
    await db.commit()
    return file.id


async def _analyses(db, file_id):
    """Every analysis row the file has, read fresh."""
    return list(
        (
            await db.execute(
                select(BucketFileAnalysis)
                .where(BucketFileAnalysis.bucket_file_id == file_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _queue_rows(db, file_id):
    return [row for row in await _analyses(db, file_id) if row.status == "pending"]


async def _verdicts(db, file_id):
    return [row for row in await _analyses(db, file_id) if row.status != "pending"]


async def _passes(db, count, *, limit=5):
    return [await bucket_ai.drain_file_analyses(db, limit=limit) for _ in range(count)]


async def test_a_document_that_keeps_failing_is_stopped_after_its_attempts(db, monkeypatch, caplog) -> None:
    file_id = await _uploaded(db, "stuck")
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={file_id}, calls=calls))

    with caplog.at_level(logging.ERROR, logger=bucket_ai.log.name):
        processed = await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 4)

    # Started on five times, a pass apart, and never again after that: not from
    # the queue, and not from the backfill of documents with no analysis.
    assert processed == [0] * (bucket_ai.MAX_ANALYSIS_ATTEMPTS + 4)
    assert calls == [file_id] * bucket_ai.MAX_ANALYSIS_ATTEMPTS
    # It is off the queue ...
    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash.startswith("consumed:")
    assert bucket_ai.queue_attempts(queue_row) == bucket_ai.MAX_ANALYSIS_ATTEMPTS
    # ... and the failure is filed where an analysis is read, in words the desk
    # is shown, so the document stops saying it is still being checked.
    (verdict,) = await _verdicts(db, file_id)
    assert verdict.status == "failed"
    assert verdict.usability_status == "needs_review"
    assert verdict.skip_reason == bucket_ai.ATTEMPTS_EXHAUSTED
    assert verdict.analysis_version == bucket_ai.CURRENT_FILE_ANALYSIS_VERSION
    assert verdict.error == bucket_ai.ATTEMPTS_EXHAUSTED_MESSAGE
    assert "5 times" in verdict.error and "Traceback" not in verdict.error and "varchar" not in verdict.error.lower()
    # Said once in the log, with the reason the last attempt gave.
    gave_up = [record for record in caplog.records if "gave up" in record.getMessage()]
    assert len(gave_up) == 1
    assert str(file_id) in gave_up[0].getMessage()
    assert "value too long" in gave_up[0].getMessage()


async def test_each_failed_attempt_is_counted_and_its_error_kept_on_the_queue_row(db, monkeypatch) -> None:
    file_id = await _uploaded(db, "stuck")
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={file_id}, calls=[]))

    await _passes(db, 2)

    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash == "pending"  # still queued
    assert bucket_ai.queue_attempts(queue_row) == 2
    assert queue_row.analysis == {
        "queue_attempts": 2,
        "queue_last_error": "RuntimeError: value too long for type character varying(32)",
    }
    # Nothing is written where the desk reads a document's reason: while the
    # attempts go on, the document is in progress, not explained by an error.
    assert queue_row.error is None and queue_row.skip_detail is None
    assert await _verdicts(db, file_id) == []


async def test_only_the_first_line_of_an_error_is_kept(db, monkeypatch) -> None:
    """A database error goes on to quote the statement and the values it was
    given, which are the client's. Those stay in the log."""
    file_id = await _uploaded(db, "stuck")

    async def refuses(session, file, **_kwargs):
        raise RuntimeError(
            "value too long for type character varying(32)\n"
            "[SQL: UPDATE application_profiles SET entity_type=$1 WHERE id = $2]\n"
            "[parameters: ('Limited liability company taxed as an S corporation', 'c0ffee')]"
        )

    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", refuses)

    await _passes(db, 1)

    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.analysis["queue_last_error"] == "RuntimeError: value too long for type character varying(32)"
    assert "parameters" not in str(queue_row.analysis) and "S corporation" not in str(queue_row.analysis)


async def test_an_attempt_is_on_record_before_the_work_starts(db, monkeypatch) -> None:
    """A document whose analysis takes the process down never reaches an error
    handler. If the attempt were only counted there, that document would be
    started on again after every restart, for good."""
    import asyncio

    file_id = await _uploaded(db, "fatal")
    calls: list = []

    async def cut_off(session, file, **_kwargs):
        calls.append(file.id)
        raise asyncio.CancelledError  # not an Exception: no handler in the drain runs

    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", cut_off)
    for _restart in range(bucket_ai.MAX_ANALYSIS_ATTEMPTS):
        with pytest.raises(asyncio.CancelledError):
            await bucket_ai.drain_file_analyses(db, limit=5)
        await db.rollback()  # what the lost connection does to the open transaction

    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash == "pending"
    assert bucket_ai.queue_attempts(queue_row) == bucket_ai.MAX_ANALYSIS_ATTEMPTS

    # The next pass does not start on it a sixth time; it stops.
    assert await bucket_ai.drain_file_analyses(db, limit=5) == 0
    assert len(calls) == bucket_ai.MAX_ANALYSIS_ATTEMPTS
    (verdict,) = await _verdicts(db, file_id)
    assert verdict.status == "failed" and verdict.skip_reason == bucket_ai.ATTEMPTS_EXHAUSTED


async def test_a_document_that_recovers_before_the_limit_is_analyzed_as_usual(db, monkeypatch) -> None:
    file_id = await _uploaded(db, "flaky")
    failing = {file_id}
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing=failing, calls=calls))

    assert await _passes(db, 2) == [0, 0]
    failing.clear()  # whatever was wrong has passed
    assert await _passes(db, 3) == [1, 0, 0]

    assert calls == [file_id] * 3
    (verdict,) = await _verdicts(db, file_id)
    assert verdict.status == "completed"
    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash.startswith("consumed:")


async def test_a_legacy_upload_that_was_never_queued_is_stopped_too(db, monkeypatch) -> None:
    """The backfill picks up uploads that predate queuing. They had no queue
    row to count on, so one that failed was picked up again on every pass."""
    stuck = await _uploaded(db, "legacy-stuck", queue=False, days_ago=1)
    fine = await _uploaded(db, "legacy-fine", queue=False, days_ago=2)
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={stuck}, calls=calls))

    processed = await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 3)

    assert processed == [1] + [0] * (bucket_ai.MAX_ANALYSIS_ATTEMPTS + 2)
    assert calls.count(fine) == 1
    assert calls.count(stuck) == bucket_ai.MAX_ANALYSIS_ATTEMPTS
    (verdict,) = await _verdicts(db, stuck)
    assert verdict.status == "failed" and verdict.skip_reason == bucket_ai.ATTEMPTS_EXHAUSTED


async def test_stopping_leaves_a_finished_analysis_alone(db, monkeypatch) -> None:
    """When a re-run the desk asked for keeps failing, the analysis the
    document already has is still the truth about it."""
    file_id = await _uploaded(db, "reread", queue=False, content_hash="sha-current")
    db.add(
        BucketFileAnalysis(
            bucket_file_id=file_id, bucket_id=(await db.get(BucketFile, file_id)).bucket_id,
            content_hash="sha-current", analysis_version=bucket_ai.CURRENT_FILE_ANALYSIS_VERSION,
            status="completed", usability_status="usable", summary="Three months of statements.",
        )
    )
    await bucket_ai.enqueue_file_analysis(db, await db.get(BucketFile, file_id), force=True)
    await db.commit()
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={file_id}, calls=calls))

    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 2)

    assert len(calls) == bucket_ai.MAX_ANALYSIS_ATTEMPTS
    (verdict,) = await _verdicts(db, file_id)
    assert verdict.status == "completed" and verdict.usability_status == "usable"
    assert verdict.summary == "Three months of statements." and verdict.error is None
    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash.startswith("consumed:")


async def test_the_failure_is_filed_against_the_bytes_the_file_holds(db, monkeypatch) -> None:
    """Readers match an analysis to the file by its content hash; a failure
    filed under any other name would leave them saying "still processing"."""
    file_id = await _uploaded(db, "known-bytes", content_hash="sha-of-the-upload")
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={file_id}, calls=[]))

    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 1)

    # The strict read the file page's document count uses.
    statuses = (
        await db.execute(
            select(BucketFileAnalysis.status)
            .join(BucketFile, BucketFile.id == BucketFileAnalysis.bucket_file_id)
            .where(
                BucketFileAnalysis.bucket_file_id == file_id,
                BucketFileAnalysis.analysis_version == bucket_ai.CURRENT_FILE_ANALYSIS_VERSION,
                BucketFileAnalysis.content_hash == BucketFile.content_hash,
            )
        )
    ).scalars().all()
    assert statuses == ["failed"]


async def test_the_room_reads_a_stopped_document_as_failed_not_as_still_checking(db, monkeypatch) -> None:
    from app.services import locked_file_requests

    file_id = await _uploaded(db, "stuck")
    (queue_row,) = await _queue_rows(db, file_id)
    queue_row.created_at = NOW - timedelta(hours=1)  # queued an hour before it was given up on
    await db.commit()
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={file_id}, calls=[]))
    file = await db.get(BucketFile, file_id)

    before = await _room_state(db, locked_file_requests, file)
    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 1)
    after = await _room_state(db, locked_file_requests, await db.get(BucketFile, file_id))

    assert (before.analysis_status, before.analysis_reason_code) == ("pending", "analysis_pending")
    assert (after.analysis_status, after.analysis_reason_code) == ("failed", "analysis_failed")


async def _room_state(db, locked_file_requests, file):
    """The analysis state the room shows for a file, by the room's own rule
    for choosing which of the file's analysis rows is current."""
    rows = sorted(
        await _analyses(db, file.id),
        key=lambda row: (row.analysis_version, row.created_at, str(row.id)),
        reverse=True,
    )
    current = next(
        (row for row in rows if not file.content_hash or row.content_hash == file.content_hash), None
    )
    return locked_file_requests.public_analysis_state(file, current)


async def test_a_retry_the_desk_asks_for_starts_the_count_again(db, monkeypatch) -> None:
    file_id = await _uploaded(db, "stuck")
    failing = {file_id}
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing=failing, calls=calls))
    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS + 1)
    assert len(calls) == bucket_ai.MAX_ANALYSIS_ATTEMPTS

    # The desk presses retry after the cause is fixed.
    failing.clear()
    await bucket_ai.enqueue_file_analysis(db, await db.get(BucketFile, file_id), force=True)
    await db.commit()

    assert await _passes(db, 2) == [1, 0]
    assert len(calls) == bucket_ai.MAX_ANALYSIS_ATTEMPTS + 1
    assert sorted(row.status for row in await _verdicts(db, file_id)) == ["completed", "failed"]


async def test_stopping_one_document_does_not_hold_up_the_one_behind_it(db, monkeypatch) -> None:
    stuck = await _uploaded(db, "stuck")
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={stuck}, calls=[]))
    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS)  # every attempt used; not yet stopped
    behind = await _uploaded(db, "behind")
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing={stuck}, calls=calls, disturbs_session=True))

    assert await bucket_ai.drain_file_analyses(db, limit=5) == 1

    assert calls == [behind]
    assert [row.status for row in await _verdicts(db, stuck)] == ["failed"]
    assert [row.status for row in await _verdicts(db, behind)] == ["completed"]


async def test_a_stop_that_cannot_be_recorded_costs_no_model_call_and_no_pass(db, monkeypatch, caplog) -> None:
    stuck = await _uploaded(db, "stuck")
    behind = await _uploaded(db, "behind")
    failing = {stuck, behind}
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing=failing, calls=calls))
    await _passes(db, bucket_ai.MAX_ANALYSIS_ATTEMPTS)
    calls.clear()
    failing.discard(behind)
    # Filing the failure fails for the stuck document, whatever the reason.
    real = bucket_ai._get_or_create_analysis_row

    async def refuse(session, file, content_hash):
        if file.id == stuck:
            raise RuntimeError("could not file it")
        return await real(session, file, content_hash)

    monkeypatch.setattr(bucket_ai, "_get_or_create_analysis_row", refuse)

    with caplog.at_level(logging.ERROR, logger=bucket_ai.log.name):
        assert await _passes(db, 2) == [0, 0]

    # Nothing was sent to the model for it, and the pass went on to the next.
    assert stuck not in calls
    (queue_row,) = await _queue_rows(db, stuck)
    assert queue_row.content_hash == "pending"
    assert any("could not stop retrying" in record.getMessage() for record in caplog.records)
    # Once it can be filed, it is.
    monkeypatch.setattr(bucket_ai, "_get_or_create_analysis_row", real)
    await bucket_ai.drain_file_analyses(db, limit=5)
    assert [row.status for row in await _verdicts(db, stuck)] == ["failed"]


async def test_an_archive_still_being_unpacked_uses_none_of_its_attempts(db, monkeypatch) -> None:
    file_id = await _uploaded(db, "bundle")
    file = await db.get(BucketFile, file_id)
    file.extraction_status = "retryable"
    await db.commit()
    calls: list = []
    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", _analyzer(db, failing=set(), calls=calls))
    monkeypatch.setattr("app.routers.buckets._reconcile_completed_bucket_file", AsyncMock())

    assert await _passes(db, 3) == [0, 0, 0]

    assert calls == []
    (queue_row,) = await _queue_rows(db, file_id)
    assert queue_row.content_hash == "pending" and bucket_ai.queue_attempts(queue_row) == 0


# ── a document queued a second time ─────────────────────────────────────────


async def test_a_document_queued_a_second_time_can_leave_the_queue(db, monkeypatch) -> None:
    """A document whose first analysis ended without a finished result is
    queued again by the next thing that asks (an archive being re-read, a file
    opened from its room). Both queue rows were renamed to the same name when
    taken, the database refused the second as a duplicate, and that refusal
    was outside anything that caught it: every pass ended there, with every
    document behind it waiting."""
    file_id = await _uploaded(db, "twice")
    behind = await _uploaded(db, "behind")
    calls: list = []

    async def ends_unfinished(session, file, **_kwargs):
        calls.append(file.id)
        if file.id == file_id and calls.count(file_id) == 1:
            session.add(  # the model call failed: a failed analysis, filed and committed
                BucketFileAnalysis(
                    bucket_file_id=file.id, bucket_id=file.bucket_id, content_hash="sha-first",
                    status="failed", analysis_version=bucket_ai.CURRENT_FILE_ANALYSIS_VERSION,
                )
            )
        else:
            session.add(
                BucketFileAnalysis(
                    bucket_file_id=file.id, bucket_id=file.bucket_id, content_hash=f"sha:{file.id.hex[:16]}",
                    status="completed", analysis_version=bucket_ai.CURRENT_FILE_ANALYSIS_VERSION,
                )
            )
        await session.flush()
        return object()

    monkeypatch.setattr(bucket_ai, "analyze_bucket_file", ends_unfinished)
    assert await bucket_ai.drain_file_analyses(db, limit=1) == 1  # the first, unfinished, analysis
    # Queued again, the ordinary way; it now sits ahead of the other document.
    await bucket_ai.enqueue_file_analysis(db, await db.get(BucketFile, file_id))
    await db.commit()
    for row in await _queue_rows(db, file_id):
        if row.content_hash == "pending":
            row.created_at = NOW - timedelta(days=1)
    await db.commit()

    assert await bucket_ai.drain_file_analyses(db, limit=5) == 2

    assert calls == [file_id, file_id, behind]
    taken = [row.content_hash for row in await _queue_rows(db, file_id)]
    assert len(taken) == 2 and len(set(taken)) == 2
    assert all(name.startswith("consumed:") for name in taken)
