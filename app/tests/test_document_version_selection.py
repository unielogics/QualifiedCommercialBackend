"""Execute real source selectors; replacement never falls back by filename."""
from __future__ import annotations

import copy
import sqlite3
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.models.bucket import BucketFile
from app.services import bucket_ai, financial_plan
from app.services.document_version_selection import (
    PendingDocumentAnalysis,
    current_document_file_clause,
    latest_document_file_clause,
    lock_latest_sources,
)
from app.services.financial_debt_actions import payoff_review_projection


def sql(predicate):
    return str(select(BucketFile.id).where(predicate(BucketFile.id)).compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True},
    ))


@pytest.fixture
def database():
    with sqlite3.connect(":memory:") as db:
        db.executescript("""
            CREATE TABLE bucket_files (
                id TEXT PRIMARY KEY, bucket_id TEXT NOT NULL, document_family_id TEXT NOT NULL,
                document_version_number INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'uploaded',
                document_version_status TEXT NOT NULL DEFAULT 'current',
                is_current_document_version BOOLEAN NOT NULL DEFAULT 1,
                deleted_at TEXT, parent_zip_file_id TEXT
            );
        """)
        yield db


def add(db, bucket, family, number=1, *, status="uploaded", state="current", current=True, deleted=None, parent=None):
    key = uuid4().hex
    db.execute("INSERT INTO bucket_files VALUES (?,?,?,?,?,?,?,?,?)", (key, bucket, family, number, status, state, current, deleted, parent))
    return key


def selected(db, predicate):
    return {row[0] for row in db.execute(sql(predicate)).fetchall()}


@pytest.mark.parametrize("state,status,deleted", [
    ("pending", "uploaded", None), ("failed", "uploaded", None),
    ("rejected", "rejected", None), ("current", "uploaded", "2026-11-06"),
])
def test_received_update_blocks_old_current_without_silent_fallback(database, state, status, deleted):
    bucket, family = uuid4().hex, uuid4().hex
    old = add(database, bucket, family)
    latest = add(database, bucket, family, 2, status=status, state=state, current=False, deleted=deleted)
    assert old not in selected(database, latest_document_file_clause)
    assert selected(database, current_document_file_clause) == set()
    assert selected(database, latest_document_file_clause) == ({latest} if deleted is None else set())


def test_duplicate_upload_does_not_add_fulfillment_or_replace_current(database):
    bucket, family = uuid4().hex, uuid4().hex
    old = add(database, bucket, family)
    duplicate = add(database, bucket, family, 2, state="duplicate", current=False)
    assert selected(database, latest_document_file_clause) == {old}
    assert selected(database, current_document_file_clause) == {old}
    assert duplicate not in selected(database, latest_document_file_clause)


def test_distinct_accounts_periods_and_loans_stay_separate_even_in_same_bucket(database):
    bucket = uuid4().hex
    originals = {add(database, bucket, uuid4().hex) for _ in range(5)}
    assert selected(database, latest_document_file_clause) == originals
    assert selected(database, current_document_file_clause) == originals


def test_upload_reservation_is_not_received_until_complete(database):
    bucket, family = uuid4().hex, uuid4().hex
    old = add(database, bucket, family)
    pending = add(database, bucket, family, 2, status="uploading", state="pending", current=False)
    assert selected(database, current_document_file_clause) == {old}
    database.execute("UPDATE bucket_files SET status='uploaded' WHERE id=?", (pending,))
    assert selected(database, latest_document_file_clause) == {pending}
    assert selected(database, current_document_file_clause) == set()


def test_bucket_scope_and_zip_replacement_select_only_latest_archive_children(database):
    bucket, family = uuid4().hex, uuid4().hex
    old_zip = add(database, bucket, family, state="failed", current=False)
    old_child = add(database, bucket, uuid4().hex, parent=old_zip)
    other_bucket = add(database, uuid4().hex, family, 5)
    assert old_child in selected(database, current_document_file_clause)
    new_zip = add(database, bucket, family, 2, state="failed", current=False)
    new_child = add(database, bucket, uuid4().hex, parent=new_zip)
    assert old_child not in selected(database, latest_document_file_clause)
    assert selected(database, current_document_file_clause) == {new_child, other_bucket}


def test_collection_can_show_latest_update_without_promoting_any_proposed_figures():
    analysis = SimpleNamespace(id=uuid4(), status="completed", classification="payoff_or_mortgage_statement", analysis={"key_facts": {"payoff_amount": 12345}}, evidence_claims=[{"value": 12345}])
    original = copy.deepcopy(analysis.analysis)
    projected = PendingDocumentAnalysis(analysis, SimpleNamespace(document_version_status="pending"))
    assert projected.usability_status == "needs_review" and projected.classification is None
    assert projected.analysis["key_facts"] == {} and projected.evidence_claims == []
    assert analysis.analysis == original


@pytest.mark.asyncio
async def test_late_old_analysis_keeps_history_without_routing_importing_or_capturing_facts():
    db = SimpleNamespace(info={})
    file = SimpleNamespace(id=uuid4())
    analysis = SimpleNamespace(status="completed", content_hash="a" * 64)
    with (
        patch("app.services.document_versions.complete_analysis", AsyncMock(return_value=False)),
        patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value=set())),
        patch.object(bucket_ai, "analysis_usability", return_value="usable"),
        patch.object(bucket_ai, "reconcile_uploaded_file", AsyncMock()) as route,
        patch("app.services.financial_workspace.register_uploaded_financial_document", AsyncMock()) as stage,
        patch("app.services.application_profiles.capture_extracted_profile_facts", AsyncMock()) as capture,
    ):
        await bucket_ai._reconcile_analysis_consumers(db, file=file, analysis=analysis)
    route.assert_not_called()
    stage.assert_not_called()
    capture.assert_not_called()
    assert analysis.status == "completed"


@pytest.mark.asyncio
async def test_review_publication_locks_family_before_current_source_check_without_autoflush():
    file = SimpleNamespace(id=uuid4(), document_family_id=uuid4())
    db = SimpleNamespace(info={}, no_autoflush=nullcontext(), execute=AsyncMock())
    with patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value=set())):
        assert not await lock_latest_sources(db, [file])
    query = db.execute.await_args.args[0]
    assert query._for_update_arg is not None


def test_replaced_payoff_cannot_fall_back_to_old_quote_or_balance_in_refinance():
    key = str(uuid4())
    saved = [{"id": key, "lender": "One loan", "balance": 80000, "payoff_amount": 78000, "monthly_payment": 1000}]
    projected = payoff_review_projection(saved, {key})
    obligations = financial_plan.obligations({"rows": projected, "service": {"schedule_authoritative": True}}, {})
    assert obligations[0]["payoff"] is None and obligations[0]["payoff_requires_review"]
    assert projected[0]["published_payoff_amount"] == 78000
    assert saved[0]["payoff_amount"] == 78000 and "payoff_requires_review" not in saved[0]


@pytest.mark.asyncio
async def test_stale_source_is_rejected_before_another_financial_import_model_call():
    from fastapi import HTTPException

    from app.services import financial_workspace as workspace

    profile = SimpleNamespace(primary_bucket_id=uuid4())
    source = SimpleNamespace(id=uuid4(), bucket_id=profile.primary_bucket_id, deleted_at=None, status="uploaded")
    db = SimpleNamespace(get=AsyncMock(return_value=source))
    with patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value=set())), patch.object(workspace, "_extract_full_form", AsyncMock()) as model:
        with pytest.raises(HTTPException) as error:
            await workspace.propose_import(db, profile, kind="pfs", file_id=source.id)
    assert error.value.status_code == 409 and "newer document version" in error.value.detail.lower()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_acquires_family_root_before_material_file_lock():
    order = []
    file = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(no_autoflush=nullcontext(), execute=AsyncMock(side_effect=lambda _query: order.append("file")))
    with patch("app.services.document_version_selection.lock_document_roots", AsyncMock(side_effect=lambda *_args: order.append("root"))):
        await bucket_ai._lock_analysis_scope(db, file)
    assert order == ["root", "file"]


@pytest.mark.asyncio
async def test_manual_signoff_acquires_root_before_readiness_bucket_and_profile():
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services import application_programs

    order = []
    file = SimpleNamespace(id=uuid4())
    async with AsyncSession() as db:
        db.get = AsyncMock(return_value=file)
        with (
            patch.object(application_programs.profiles, "_profile_evidence_file_ids", AsyncMock(return_value={file.id})),
            patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value={file.id})),
            patch("app.services.document_version_selection.lock_document_roots", AsyncMock(side_effect=lambda *_args: order.append("root"))),
            patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=RuntimeError("readiness boundary"))),
        ):
            with pytest.raises(RuntimeError, match="readiness boundary"):
                await application_programs.override_evidence_decision(db, profile=SimpleNamespace(), requirement_key="bank", file_id=file.id, payload=SimpleNamespace(), user=SimpleNamespace())
    assert order == ["root"]


@pytest.mark.parametrize("changed,classification,reason", [
    ({"account_last4": "9999"}, "bank_statement", "bank account"),
    ({"statement_period": "2026-07"}, "bank_statement", "reporting period"),
    ({"business_name": "Different LLC"}, "bank_statement", "owner or borrower"),
    ({"loan_number": "different-loan"}, "payoff_or_mortgage_statement", "loan reference"),
    ({"tax_year": 2024}, "tax_return", "tax year"),
])
def test_explicit_replacement_cannot_merge_known_distinct_document_identities(changed, classification, reason):
    from app.services.document_identity import (
        document_identity_conflict,
        structured_document_identity,
    )

    original = {"account_last4": "1234", "statement_period": "2026-06", "business_name": "Same LLC", "loan_number": "loan-1", "tax_year": 2025}
    assert reason in document_identity_conflict(structured_document_identity({**original, **changed}, classification), structured_document_identity(original, classification))


def test_payoff_quote_dates_can_advance_and_unknown_identity_is_not_invented():
    from app.services.document_identity import (
        document_identity_conflict,
        structured_document_identity,
    )

    original = {"loan_number": "loan-1", "business_name": "Same LLC", "payoff_date": "2026-10-01"}
    newer = {**original, "payoff_date": "2026-11-06"}
    assert document_identity_conflict(structured_document_identity(newer, "payoff_or_mortgage_statement"), structured_document_identity(original, "payoff_or_mortgage_statement")) is None
    assert document_identity_conflict(structured_document_identity({}, "payoff_or_mortgage_statement"), structured_document_identity(original, "payoff_or_mortgage_statement")) is None


@pytest.mark.asyncio
async def test_published_payoff_review_accepts_latest_pending_but_not_old_failed_rejected_or_mismatched(database):
    from uuid import UUID

    from app.models.bucket import BucketFileAnalysis
    from app.services.financial_debt_actions import reviewed_payoff_source_ids

    bucket, family = uuid4().hex, uuid4().hex
    old = add(database, bucket, family)
    pending = add(database, bucket, family, 2, state="pending", current=False)
    failed = add(database, bucket, uuid4().hex, state="failed", current=False)
    rejected = add(database, bucket, uuid4().hex, state="rejected", current=False)
    mismatched = add(database, bucket, uuid4().hex, state="pending", current=False)
    current = add(database, bucket, uuid4().hex)
    sources = {UUID(value): SimpleNamespace(id=UUID(value), content_hash="same-hash") for value in (old, pending, failed, rejected, mismatched, current)}
    class Rows:
        def __init__(self, values): self.values = values
        def scalars(self): return self
        def all(self): return self.values
    class DB:
        info = {}
        async def execute(self, statement):
            if statement.column_descriptions[0]["entity"] is BucketFileAnalysis:
                return Rows([SimpleNamespace(bucket_file_id=UUID(mismatched), content_hash="same-hash", analysis={"document_version_review": {"status": "identity_mismatch"}})])
            sql = str(statement.with_only_columns(BucketFile.id).compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
            return Rows([sources[UUID(row[0])] for row in database.execute(sql).fetchall()])
    assert await reviewed_payoff_source_ids(DB(), set(sources)) == {UUID(pending), UUID(current)}


@pytest.mark.asyncio
@pytest.mark.parametrize("latest,usable,expected_calls", [(False, False, 0), (True, False, 0), (True, True, 1)])
async def test_cached_merchant_offer_cannot_update_terms_from_an_old_or_pending_version(latest, usable, expected_calls):
    from app.services import merchant_processing
    file = SimpleNamespace(id=uuid4())
    analysis = SimpleNamespace(status="completed", classification=merchant_processing.OFFER_CLASSIFICATION)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: analysis)))
    selector = AsyncMock(side_effect=[{file.id} if latest else set(), {file.id} if usable else set()])
    with patch("app.services.document_version_selection.document_file_ids", selector), patch.object(merchant_processing, "absorb_analysis", AsyncMock()) as absorb, patch.object(bucket_ai, "analyze_bucket_file", AsyncMock()) as analyze:
        await merchant_processing.ensure_extracted(db, SimpleNamespace(terms={"accepted": "preserved"}), file, review_type=None)
    assert absorb.await_count == expected_calls
    analyze.assert_not_called()
    if not latest:
        db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_evidence_active_count_counts_latest_received_family_once_and_keeps_history_visible():
    from datetime import UTC, datetime

    from app.services import application_profiles
    bucket = uuid4()
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=bucket, intake_id=None)
    def source():
        return SimpleNamespace(id=uuid4(), bucket_id=bucket, file_name="payoff.pdf", content_type="application/pdf", size_bytes=100, created_at=datetime.now(UTC), source_kind="client_upload", source_detail=None)
    old, latest = source(), source()
    class Rows:
        def __init__(self, values): self.values = values
        def scalars(self): return self
        def all(self): return self.values
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(name="Evidence")), execute=AsyncMock(side_effect=[Rows([old, latest]), Rows([])]))
    with patch("app.services.document_version_selection.filter_document_files", AsyncMock(return_value=[latest])), patch.object(application_profiles.locked_file_requests, "request_states_for_files", AsyncMock(return_value={})), patch.object(application_profiles.locked_file_requests, "is_password_protected_file", return_value=False), patch.object(application_profiles.locked_file_requests, "current_request_state", return_value=None):
        result = await application_profiles.evidence_state(db, profile)
    assert result.sources[0].active_file_count == 1
    assert {file.id for file in result.files} == {old.id, latest.id}
