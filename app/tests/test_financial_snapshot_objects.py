import importlib.util
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import JSON, Column, DateTime, MetaData, Table, Uuid, column, null, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.bucket import BucketAIReview, BucketFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import document_versions, funding_requests
from app.services.financial_snapshot_objects import (
    known_null_wrapper,
    snapshot_object,
    sql_mark_snapshot,
)


@pytest.mark.parametrize("value", [None, {}, {"key_metrics": {"dscr": -.25}, "decision": "historical"},
    [None, {"key_metrics": {"dscr": -.25}}, {"_funding_request_stale": True}],
    [None, {"funding_request_stale": True, "funding_request_revision": 1}, {"document_versions_stale": True}]])
def test_null_and_known_wrappers_become_objects_without_inventing_or_overwriting_facts(value):
    original = deepcopy(value)
    result = {**snapshot_object(value), "_funding_request_stale": True}
    assert isinstance(result, dict) and result["_funding_request_stale"] is True
    assert value == original
    if isinstance(value, dict) and value.get("key_metrics") or isinstance(value, list) and any(isinstance(part, dict) and "key_metrics" in part for part in value):
        assert result["key_metrics"] == {"dscr": -.25}
    else:
        assert "key_metrics" not in result


@pytest.mark.parametrize("value", [[None], [None, {"not_stale": True}], [{"_funding_request_stale": True}],
    [None, {"_funding_request_stale": True}, 123], [None, {"_funding_request_stale": False}],
    [None, [1], {"_document_versions_stale": True}], "legacy", 0, False])
def test_arbitrary_arrays_and_scalars_are_preserved_and_not_selected_by_the_repair(value):
    assert not known_null_wrapper(value)
    assert snapshot_object(value) == {"_legacy_snapshot_value": value}


def test_known_wrapper_merges_objects_in_order_and_keeps_each_distinct_marker():
    value = [None, {"_funding_request_stale": True, "request_revision": 1, "source": {"value": 0}},
             {"_document_versions_stale": True, "request_revision": 2}]
    assert snapshot_object(value) == {"_funding_request_stale": True, "_document_versions_stale": True,
        "request_revision": 2, "source": {"value": 0}}


def test_sql_marker_guards_both_sql_null_and_json_null_before_jsonb_concatenation():
    statement = select(sql_mark_snapshot(column("snapshot", JSONB), {"_funding_request_stale": True}))
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "CASE WHEN (jsonb_typeof(snapshot)" in sql
    assert "snapshot IS NULL OR jsonb_typeof(snapshot)" in sql
    assert "jsonb_build_object" in sql
    assert "END ||" in sql
    parameters = statement.compile(dialect=postgresql.dialect()).params
    assert "object" in parameters.values() and "null" in parameters.values()
    assert {} in parameters.values() and {"_funding_request_stale": True} in parameters.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, {"key_metrics": {"dscr": -.25}}, [None, {"_funding_request_stale": True}]])
@pytest.mark.parametrize("writer", ["funding", "document"])
async def test_invalidation_updates_loaded_instances_and_uses_the_same_guard_for_both_columns(writer, value):
    bucket_id = uuid4()
    intake = PublicUnderwritingIntake(id=uuid4(), bucket_id=bucket_id, result_snapshot=deepcopy(value), intake_state={})
    review = BucketAIReview(id=uuid4(), bucket_id=bucket_id, context_snapshot=deepcopy(value))
    db = SimpleNamespace(execute=AsyncMock(), identity_map={1: intake, 2: review})
    if writer == "funding":
        await funding_requests.invalidate_request_reviews(db, SimpleNamespace(primary_bucket_id=bucket_id,
            current_funding_request={"revision": 2}, use_of_funds_revision=3))
        intake_marker, review_marker = "_funding_request_stale", "funding_request_stale"
    else:
        file = BucketFile(id=uuid4(), bucket_id=bucket_id, document_family_id=uuid4(), document_version_status="pending")
        await document_versions.mark_received(db, file)
        intake_marker, review_marker = "_document_versions_stale", "document_versions_stale"
    assert intake.result_snapshot[intake_marker] is True
    assert review.context_snapshot[review_marker] is True
    assert isinstance(intake.result_snapshot, dict) and isinstance(review.context_snapshot, dict)
    if isinstance(value, dict):
        assert intake.result_snapshot["key_metrics"] == value["key_metrics"]
        assert review.context_snapshot["key_metrics"] == value["key_metrics"]
    assert db.execute.await_count == 2
    for call in db.execute.await_args_list:
        sql = str(call.args[0].compile(dialect=postgresql.dialect()))
        assert "CASE WHEN (jsonb_typeof(" in sql and "END ||" in sql


def test_repair_migration_only_unwraps_null_prefixed_object_lists_with_true_known_markers():
    path = Path("alembic/versions/0272_financial_snapshot_objects.py")
    spec = importlib.util.spec_from_file_location("snapshot_repair_0272", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module.op, "execute") as execute:
        module.upgrade()
    assert execute.call_count == 2
    sql = "\n".join(str(call.args[0]) for call in execute.call_args_list)
    assert "public_underwriting_intakes" in sql and "bucket_ai_reviews" in sql
    assert "-> 0 = 'null'::jsonb" in sql
    assert "part.position > 1 AND jsonb_typeof(part.value) IS DISTINCT FROM 'object'" in sql
    assert "'_funding_request_stale' = 'true'::jsonb" in sql
    assert "'document_versions_stale' = 'true'::jsonb" in sql
    assert "jsonb_object_agg(entry.key, entry.value ORDER BY part.position)" in sql
    assert "ELSE '[]'::jsonb END" in sql  # Array functions remain safe despite planner predicate reordering.
    assert module.down_revision == "0271_cp_investor_presentation"


@pytest.mark.asyncio
async def test_sqlite_real_invalidation_preserves_both_null_kinds_objects_and_unrelated_values():
    """Execute both production writers, including repeated cached SQL, on SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    reviews = Table("bucket_ai_reviews", metadata, Column("id", Uuid, primary_key=True),
        Column("bucket_id", Uuid), Column("context_snapshot", JSON), Column("updated_at", DateTime))
    intakes = Table("public_underwriting_intakes", metadata, Column("id", Uuid, primary_key=True),
        Column("bucket_id", Uuid), Column("result_snapshot", JSON),
        Column("latest_review_id", Uuid), Column("updated_at", DateTime))
    bucket_id = uuid4()
    values = [null(), JSON.NULL, {"key_metrics": {"dscr": -.25, "zero": 0}}, [1, {"source": "legacy"}], "legacy"]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            for value in values:
                await connection.execute(reviews.insert().values(id=uuid4(), bucket_id=bucket_id, context_snapshot=value))
                await connection.execute(intakes.insert().values(id=uuid4(), bucket_id=bucket_id,
                    result_snapshot=value, latest_review_id=uuid4()))
        async with AsyncSession(engine) as db:
            profile = SimpleNamespace(primary_bucket_id=bucket_id,
                current_funding_request={"revision": 2}, use_of_funds_revision=3)
            await funding_requests.invalidate_request_reviews(db, profile)
            profile.current_funding_request["revision"] = 4
            await funding_requests.invalidate_request_reviews(db, profile)
            file = BucketFile(id=uuid4(), bucket_id=bucket_id, document_family_id=uuid4(), document_version_status="pending")
            await document_versions.mark_received(db, file)
            review_values = list((await db.execute(select(reviews.c.context_snapshot))).scalars())
            intake_rows = (await db.execute(select(intakes.c.result_snapshot, intakes.c.latest_review_id))).all()
            for snapshots, prefix in ((review_values, ""), ([row[0] for row in intake_rows], "_")):
                assert all(isinstance(value, dict) for value in snapshots)
                assert all(value[f"{prefix}funding_request_stale"] is True for value in snapshots)
                assert all(value[f"{prefix}document_versions_stale"] is True for value in snapshots)
                assert "_legacy_snapshot_value" not in snapshots[0] and "_legacy_snapshot_value" not in snapshots[1]
                assert snapshots[2]["key_metrics"] == {"dscr": -.25, "zero": 0}
                assert snapshots[3]["_legacy_snapshot_value"] == [1, {"source": "legacy"}]
                assert snapshots[4]["_legacy_snapshot_value"] == "legacy"
            assert all(value["funding_request_revision"] == 4 and value["use_of_funds_revision"] == 3 for value in review_values)
            assert all(row[1] is None for row in intake_rows)
    finally:
        await engine.dispose()
