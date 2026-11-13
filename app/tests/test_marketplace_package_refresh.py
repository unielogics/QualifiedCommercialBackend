"""Automatic package refresh shares render/version primitives without approving an offer."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import dealer_ai_intake as core
from app.services import marketplace_package_refresh as service


class Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class DB:
    def __init__(self):
        self.commit, self.rollback, self.delete = AsyncMock(), AsyncMock(), AsyncMock()
        self.execute = AsyncMock(return_value=Result())
        self.flush = AsyncMock()
        self.rows, self.savepoint_rolled_back = [], False

    def add(self, value):
        value.id = value.id or uuid4()
        self.rows.append(value)

    @asynccontextmanager
    async def begin_nested(self):
        try:
            yield
        except Exception:
            self.savepoint_rolled_back = True
            raise


def setup():
    profile = SimpleNamespace(id=uuid4(), intake_id=uuid4())
    intake = SimpleNamespace(id=profile.intake_id, business_name="Example Studio", full_name="Example", bucket_id=uuid4())
    financials = {"package_options": dict(service.SAVED_OPTIONS), "package_readiness": {"requirements": []}}
    values = ([], {"package_input_sha256": "saved-source", "package_options": dict(service.SAVED_OPTIONS)}, {}, financials, "saved-source")
    return DB(), profile, intake, values


def artifacts():
    return [(SimpleNamespace(id=uuid4(), artifact_type=kind, s3_key=f"immutable/{kind}.pdf"), public_kind, label)
            for kind, public_kind, label in service.KINDS]


@pytest.mark.asyncio
async def test_reuse_does_not_render_publish_or_commit():
    db, profile, intake, values = setup()
    current = artifacts()
    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, values))), \
         patch.object(service, "_ready", AsyncMock(return_value=current)), \
         patch.object(core, "_create_executive_summary_artifact", AsyncMock()) as render:
        result = await service.prepare(db, profile)
        assert result["reused"] and result["published"] and result["artifacts"] == current
        assert await service.publish_prepared(db, result) is result
    render.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_input_collection_forces_saved_options_and_fresh_intake_read():
    db, profile, intake, values = setup()
    with patch.object(service, "existing_family", AsyncMock(return_value=None)), \
         patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)) as load, \
         patch.object(core, "_package_generation_inputs", AsyncMock(return_value=values)) as collect:
        assert await service.current_input_fingerprint(db, profile) == "saved-source"
    load.assert_awaited_once_with(db, profile.intake_id, refresh=True)
    collect.assert_awaited_once_with(db, intake, service.SAVED_OPTIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["working_draft", "missing_saved_plan"])
async def test_working_draft_or_dropped_saved_plan_is_never_a_marketplace_package(invalid):
    db, profile, intake, values = setup()
    family = SimpleNamespace(current_version_id=uuid4()) if invalid == "missing_saved_plan" else None
    if invalid == "working_draft":
        values[3]["package_options"]["include_working_operating_plan"] = True
    with patch.object(service, "existing_family", AsyncMock(return_value=family)), \
         patch.object(core, "_load_admin_dealer_lead", AsyncMock(return_value=intake)), \
         patch.object(core, "_package_generation_inputs", AsyncMock(return_value=values)):
        with pytest.raises(HTTPException) as raised:
            await service.current_input_fingerprint(db, profile)
    assert raised.value.status_code == 409
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_uses_one_saved_snapshot_for_all_pdfs_without_publishing_or_ai():
    db, profile, intake, values = setup()
    rows = artifacts()

    async def create(index, *args, **kwargs):
        assert kwargs["financials"] is values[3]
        assert kwargs["source_snapshot_metadata"] is values[1]
        kwargs["cleanup_s3_keys"].append(rows[index][0].s3_key)
        if index == 0:
            assert "conditional" in kwargs["prepared_summary"]["executive_summary"]
        return rows[index][0]

    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, values))), \
         patch.object(service, "_ready", AsyncMock(return_value=None)), \
         patch.object(core, "_create_executive_summary_artifact", AsyncMock(side_effect=lambda *a, **kw: None)) as summary, \
         patch.object(core, "_create_lender_packet_artifact", AsyncMock()) as packet, \
         patch.object(core, "_create_investor_presentation_artifact", AsyncMock()) as presentation, \
         patch.object(core, "_publish_package_artifact_to_bucket", AsyncMock()) as publish:
        async def summary_call(*a, **kw): return await create(0, *a, **kw)
        async def packet_call(*a, **kw): return await create(1, *a, **kw)
        async def presentation_call(*a, **kw): return await create(2, *a, **kw)
        summary.side_effect, packet.side_effect, presentation.side_effect = summary_call, packet_call, presentation_call
        result = await service.prepare(db, profile)
    assert result["artifacts"] == rows and not result["published"] and not result["reused"]
    assert result["created_s3_keys"] == [row[0].s3_key for row in rows]
    assert all(mock.await_args.args[2] is None for mock in (summary, packet, presentation))
    publish.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_failed_render_rolls_back_savepoint_before_deleting_only_its_new_bytes():
    db, profile, intake, values = setup()
    async def first(*a, **kw):
        kw["cleanup_s3_keys"].append("new-summary")
        return artifacts()[0][0]
    deleted = []
    def remove(key):
        assert db.savepoint_rolled_back
        deleted.append(key)
    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, values))), \
         patch.object(service, "_ready", AsyncMock(return_value=None)), \
         patch.object(core, "_create_executive_summary_artifact", AsyncMock(side_effect=first)), \
         patch.object(core, "_create_lender_packet_artifact", AsyncMock(side_effect=RuntimeError("render failed"))), \
         patch.object(core, "_delete_s3_object", side_effect=remove):
        with pytest.raises(RuntimeError, match="render failed"):
            await service.prepare(db, profile)
    assert deleted == ["new-summary"]
    db.rollback.assert_not_called()
    db.commit.assert_not_called()


def prepared(profile):
    return {"profile": profile, "intake_id": profile.intake_id, "input_fingerprint": "saved-source",
            "artifacts": artifacts(), "generation_id": uuid4(), "generated_at": "synthetic-time",
            "created_s3_keys": ["new-a", "new-b", "new-c"], "published": False, "reused": False}


@pytest.mark.asyncio
async def test_source_change_before_acceptance_keeps_prior_current_package_intact():
    db, profile, intake, values = setup()
    value = prepared(profile)
    changed = (*values[:4], "newer-source")
    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, changed))), \
         patch.object(core, "_publish_package_artifact_to_bucket", AsyncMock()) as publish:
        with pytest.raises(HTTPException) as raised:
            await service.publish_prepared(db, value)
    assert raised.value.status_code == 409 and not value["published"]
    publish.assert_not_called()
    db.delete.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_acceptance_publishes_same_generation_with_system_actor_without_committing():
    db, profile, intake, values = setup()
    value = prepared(profile)
    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, values))), \
         patch.object(service, "_ready", AsyncMock(return_value=None)), \
         patch.object(core, "_publish_package_artifact_to_bucket", AsyncMock(return_value=(SimpleNamespace(id=uuid4()), []))) as publish:
        assert await service.publish_prepared(db, value) is value
    assert value["published"] and len(value["bucket_files"]) == 3
    assert publish.await_count == 3
    assert all(call.kwargs["user"] is None and call.kwargs["generation_id"] == value["generation_id"] for call in publish.await_args_list)
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_equivalent_package_wins_without_duplicate_bucket_versions():
    db, profile, intake, values = setup()
    value, current = prepared(profile), artifacts()
    own = [row[0] for row in value["artifacts"]]
    with patch.object(service, "_inputs", AsyncMock(return_value=(intake, values))), \
         patch.object(service, "_ready", AsyncMock(return_value=current)), \
         patch.object(core, "_delete_s3_object", MagicMock()) as remove, \
         patch.object(core, "_publish_package_artifact_to_bucket", AsyncMock()) as publish:
        await service.publish_prepared(db, value)
    assert value["artifacts"] == current and value["reused"] and value["created_s3_keys"] == []
    assert [call.args[0] for call in db.delete.await_args_list] == own
    assert remove.call_count == 3
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_actual_summary_primitive_accepts_system_actor_and_never_calls_ai_for_prepared_text():
    db, _profile, intake, _values = setup()
    with patch.object(core, "_generate_management_json", AsyncMock(side_effect=AssertionError("AI forbidden"))) as ai, \
         patch.object(core, "_latest_result_for_intake", return_value={}), \
         patch.object(core, "_prepend_credit_key_metric"), patch.object(core, "_prepend_published_program_metric"), \
         patch.object(core, "render_executive_summary_pdf", return_value=b"%PDF-synthetic"), \
         patch.object(core, "_store_underwriting_pdf", AsyncMock(return_value="new-summary")), \
         patch.object(core, "_log", AsyncMock()) as audit:
        result = await core._create_executive_summary_artifact(db, intake, None, financials={},
            context_snapshot={}, prepared_summary={"title": "Saved summary", "executive_summary": "Conditional operating plan."})
    assert result.created_by_user_id is None and result.s3_key == "new-summary"
    assert audit.await_args.kwargs["actor_role"] == "system" and audit.await_args.kwargs["user"] is None
    ai.assert_not_called()
