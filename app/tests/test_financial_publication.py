from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.bucket import BucketAIReview
from app.services import bucket_ai
from app.services import financial_publication as publication


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.scalar


def test_stale_marker_is_explicit_and_does_not_change_unrelated_reviews():
    assert publication.review_is_financially_stale(SimpleNamespace(context_snapshot={"financial_versions_stale": True}))
    assert not publication.review_is_financially_stale(SimpleNamespace(context_snapshot={}))
    assert not publication.review_is_financially_stale(None)


def test_old_advisory_snapshot_is_not_used_when_its_refresh_failed():
    from datetime import UTC, datetime, timedelta

    published = datetime.now(UTC)
    assert not publication.snapshot_matches_publication(SimpleNamespace(as_of=published - timedelta(seconds=1)), published)
    assert publication.snapshot_matches_publication(SimpleNamespace(as_of=published + timedelta(seconds=1)), published)
    assert not publication.snapshot_matches_publication(None, published)


@pytest.mark.asyncio
async def test_completed_and_fallback_review_queries_both_reject_stale_results():
    stale = SimpleNamespace(context_snapshot={"financial_versions_stale": True}, result={"key_metrics": {"dscr": 8}})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(scalar=stale), Result(scalar=stale)]))
    assert await bucket_ai.latest_review(db, uuid4()) is None
    for call in db.execute.await_args_list:
        assert "financial_versions_stale" in call.args[0].compile().params.values()


@pytest.mark.asyncio
async def test_publication_invalidates_the_intake_fallback_and_loaded_review():
    bucket_id, version_id = uuid4(), uuid4()
    profile = SimpleNamespace(primary_bucket_id=bucket_id)
    review = BucketAIReview(id=uuid4(), bucket_id=bucket_id, context_snapshot={"review_type": "main_street_v1"})
    intake = SimpleNamespace(latest_review_id=review.id, latest_review=review, result_snapshot={"key_metrics": {"dscr": 8}}, intake_state={"existing": "kept"})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(), Result([intake])]), flush=AsyncMock(), identity_map={review.id: review})
    await publication.invalidate_financial_review_caches(db, profile, SimpleNamespace(id=version_id))
    assert intake.latest_review_id is intake.latest_review is intake.result_snapshot is None
    assert intake.intake_state["existing"] == "kept"
    assert intake.intake_state["financial_versions_stale"] is True
    assert review.context_snapshot["review_type"] == "main_street_v1"
    assert review.context_snapshot["financial_versions_stale"] is True


@pytest.mark.asyncio
async def test_scoped_chat_context_uses_the_exact_current_version_and_file_scope():
    profile_id, file_id = uuid4(), uuid4()
    family = SimpleNamespace(id=uuid4(), kind="debt_schedule", label="Business debt schedule")
    from datetime import UTC, datetime

    version = SimpleNamespace(id=uuid4(), version_number=3, created_at=datetime.now(UTC), pdf_file_id=file_id, source_file_id=None, key_facts={"total_outstanding_balance": 100})
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Result([(family, version)])))
    result = await publication.current_version_context(db, profile_id=profile_id, visible_file_ids={file_id}, client_safe=True)
    assert result[0]["version_id"] == str(version.id)
    assert result[0]["key_facts"]["total_outstanding_balance"] == 100
    query = db.execute.await_args.args[0]
    assert "current_version_id" in str(query)
    params = query.compile().params
    assert profile_id in params.values() and "pfs" in params.values()
    assert [file_id] in params.values()


@pytest.mark.asyncio
async def test_scoped_ai_uses_accepted_schedule_instead_of_partial_new_family_and_excluded_payments():
    from datetime import UTC, datetime

    profile_id, file_id, partial_file_id = uuid4(), uuid4(), uuid4()
    family = SimpleNamespace(id=uuid4(), profile_id=profile_id, kind="debt_schedule", label="Accepted debt")
    other = SimpleNamespace(id=uuid4(), profile_id=profile_id, kind="debt_schedule", label="New period")
    accepted = SimpleNamespace(id=uuid4(), version_number=2, source="form", status="published", created_at=datetime.now(UTC),
        pdf_file_id=file_id, source_file_id=None, key_facts={"total_monthly_debt_service": 1500},
        body={"business_name": "Little Star Therapy", "debts": [
            {"lender": "Kapfi", "balance": "40000", "monthly_payment": "1000"},
            {"lender": "Other", "balance": "9000", "monthly_payment": "500", "count_in_dscr": False},
        ]})
    partial = SimpleNamespace(id=uuid4(), version_number=1, source="form", status="published", created_at=datetime.now(UTC),
        pdf_file_id=partial_file_id, source_file_id=None, key_facts={"total_monthly_debt_service": None},
        body={"debts": [{"lender": "Unreviewed new period", "balance": "100", "monthly_payment": None}]})
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Result([(other, partial), (family, accepted)])))
    result = await publication.current_version_context(db, visible_file_ids={file_id, partial_file_id})
    assert len(result) == 1 and result[0]["version_id"] == str(accepted.id)
    assert result[0]["authoritative"] and result[0]["debt_service_for_dscr"] == 1000
    assert result[0]["source"]["file_id"] == str(file_id)
    assert accepted.key_facts["total_monthly_debt_service"] == 1500  # Immutable form total retained.
    # A limited share of the incomplete source grants no access to the accepted
    # schedule in another file, even though the database stub returns both.
    limited = await publication.current_version_context(db, visible_file_ids={partial_file_id})
    assert len(limited) == 1 and not limited[0]["authoritative"]
    assert str(file_id) not in str(limited) and str(accepted.id) not in str(limited)


@pytest.mark.asyncio
async def test_review_context_scope_requires_bucket_and_visible_file_intersection():
    bucket_id, file_id = uuid4(), uuid4()
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Result([])))
    assert await publication.current_version_context(db, bucket_id=bucket_id, visible_file_ids={file_id}) == []
    query = db.execute.await_args.args[0]
    params = query.compile().params
    assert bucket_id in params.values() and [file_id] in params.values()


@pytest.mark.asyncio
async def test_advisory_failure_does_not_fail_publication_or_start_a_model_run():
    @asynccontextmanager
    async def nested():
        yield

    db = SimpleNamespace(begin_nested=nested, refresh=AsyncMock())
    profile = SimpleNamespace(id=uuid4(), loan_id=uuid4())
    family = SimpleNamespace(id=uuid4(), kind="debt_schedule", label="Business debt schedule")
    version = SimpleNamespace(id=uuid4(), version_number=2)
    file = SimpleNamespace(id=uuid4())
    with patch.object(publication, "invalidate_financial_review_caches", AsyncMock()) as invalidated, patch.object(publication, "refresh_advisory_projections", AsyncMock(side_effect=RuntimeError("policy offline"))), patch("app.services.activity_log.mark_loan_dirty", AsyncMock()) as dirty, patch("app.services.file_events.emit", AsyncMock()) as event, patch("app.services.bucket_ai.tracked_messages_create", AsyncMock()) as model:
        await publication.after_publication(db, profile, family=family, version=version, file=file)
    invalidated.assert_awaited_once()
    dirty.assert_awaited_once_with(db, profile.loan_id)
    event.assert_awaited_once()
    model.assert_not_called()
