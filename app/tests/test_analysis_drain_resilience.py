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
