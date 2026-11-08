"""Publication boundaries for reviewed calculations, using synthetic evidence."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.services import financial_metric_composition as composition
from app.services import financial_metric_review as reviews
from app.services.financial_metric_reporting import metric_brief
from app.tests.test_financial_metric_composition import payload
from app.tests.test_financial_metric_review import ACTOR, build, correction, target


def batch_command(cockpit, *, revision=0):
    return {
        "corrections": [{"input_key": key, "action": "correct", "value": amount,
                         "source_stamp": target(cockpit, key)["source_stamp"]}
                        for key, amount in [("profit_and_loss.net_income", 30000), ("profit_and_loss.interest", 1500)]],
        "reason": "Compared with the source PDF", "expected_revision": revision,
        "idempotency_key": "review-batch-save",
    }


def session(profile):
    result = Mock()
    result.scalar_one.return_value = profile
    stage = {"nested": False, "entries": 0}

    @asynccontextmanager
    async def savepoint():
        stage.update(nested=True, entries=stage["entries"] + 1)
        try:
            yield
        finally:
            stage["nested"] = False

    return SimpleNamespace(execute=AsyncMock(return_value=result), flush=AsyncMock(), refresh=AsyncMock(),
                           begin_nested=savepoint), stage


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["batch", "composition"])
async def test_explicit_review_saves_once_and_idempotent_replay_does_not_rewrite_or_refresh(kind):
    profile = SimpleNamespace(id=uuid4(), financial_metric_review={}, primary_bucket_id=uuid4())
    db, stage = session(profile)
    command = batch_command(build()) if kind == "batch" else payload(build())

    async def current(*_args):
        return build(state=profile.financial_metric_review)

    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock,
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(side_effect=current)),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()) as log,
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock()) as invalidate,
          patch("app.services.financial_publication.refresh_advisory_projections", AsyncMock()) as refresh):
        result = await reviews.save_review(db, profile, ACTOR, composition=kind == "composition", **command)
        replay = await reviews.save_review(db, profile, ACTOR, composition=kind == "composition", **command)
    assert result["metric_review"]["revision"] == replay["metric_review"]["revision"] == (2 if kind == "batch" else 1)
    assert lock.await_count == 2 and db.execute.await_count == 2
    assert all(call.args[0].get_execution_options()["populate_existing"] for call in db.execute.await_args_list)
    assert log.await_count == invalidate.await_count == refresh.await_count == db.flush.await_count == 1
    assert stage["entries"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["single", "batch", "composition"])
async def test_advisory_failure_keeps_authoritative_invalidation_outside_its_savepoint(kind):
    profile = SimpleNamespace(id=uuid4(), financial_metric_review={}, primary_bucket_id=uuid4())
    db, stage = session(profile)
    current = build()
    if kind == "single":
        command = {**batch_command(current)["corrections"][0], "reason": "Verified", "expected_revision": 0,
                   "idempotency_key": "review-single-save"}
    else:
        command = batch_command(current) if kind == "batch" else payload(current)

    async def invalidate(*_args, **_kwargs):
        assert not stage["nested"]
        stage["invalidated"] = True

    async def fail_refresh(*_args, **_kwargs):
        assert stage["nested"] and stage["invalidated"]
        raise RuntimeError("advisory is unavailable")

    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()),
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(side_effect=lambda *_args: build(state=profile.financial_metric_review))),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()),
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock(side_effect=invalidate)),
          patch("app.services.financial_publication.refresh_advisory_projections", AsyncMock(side_effect=fail_refresh))):
        if kind == "single":
            result = await reviews.correct(db, profile, ACTOR, **command)
        else:
            result = await reviews.save_review(db, profile, ACTOR, composition=kind == "composition", **command)
    assert result["metric_review"]["revision"] > 0 and stage["invalidated"]
    db.refresh.assert_awaited_once_with(profile)


@pytest.mark.asyncio
async def test_failed_authoritative_invalidation_fails_the_save_instead_of_reporting_success():
    profile = SimpleNamespace(id=uuid4(), financial_metric_review={}, primary_bucket_id=uuid4())
    db, stage = session(profile)
    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()),
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=build())),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()),
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock(side_effect=RuntimeError("invalidation failed"))),
          patch("app.services.financial_publication.refresh_advisory_projections", AsyncMock()) as refresh):
        with pytest.raises(RuntimeError, match="invalidation failed"):
            await reviews.save_review(db, profile, ACTOR, **batch_command(build()))
    assert stage["entries"] == 0
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_writer_reloads_the_locked_profile_and_rejects_its_stale_revision():
    existing_state = correction(build())
    current = SimpleNamespace(id=uuid4(), financial_metric_review=existing_state, primary_bucket_id=uuid4())
    stale = SimpleNamespace(id=current.id, financial_metric_review={})
    db, _ = session(current)
    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock,
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=build(state=existing_state))),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()) as log,
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock()) as invalidate):
        with pytest.raises(HTTPException) as caught:
            await reviews.save_review(db, stale, ACTOR, **batch_command(build()))
    assert caught.value.status_code == 409 and current.financial_metric_review == existing_state
    assert stale.financial_metric_review == {}
    lock.assert_awaited_once_with(db, stale)
    log.assert_not_awaited()
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_second_input_does_not_write_the_valid_first_input():
    profile = SimpleNamespace(id=uuid4(), financial_metric_review={}, primary_bucket_id=uuid4())
    db, _ = session(profile)
    command = batch_command(build())
    command["corrections"][1]["source_stamp"] = "x" * 64
    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()),
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=build())),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()) as log):
        with pytest.raises(HTTPException) as caught:
            await reviews.save_review(db, profile, ACTOR, **command)
    assert caught.value.status_code == 409 and profile.financial_metric_review == {}
    log.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_service_blocks_unprivileged_actor_before_profile_reads():
    with patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()) as lock:
        with pytest.raises(HTTPException) as caught:
            await reviews.save_review(AsyncMock(), SimpleNamespace(), SimpleNamespace(role=Role.BROKER))
    assert caught.value.status_code == 403
    lock.assert_not_awaited()


def test_internal_ai_gets_labeled_assumptions_without_audit_text_and_external_gets_no_recipe():
    current = build()
    command = payload(current, adjustments=[{"id": "PRIVATE-ID", "label": "Reviewed nonrecurring expense", "amount": 1000}])
    state, _ = composition.change({}, current["metric_review"], actor=ACTOR, **command)
    state["events"][0].update(reason="PRIVATE-REASON", actor_name="PRIVATE-ACTOR", idempotency_key="PRIVATE-RECEIPT")
    updated = build(state=state)
    brief = metric_brief(updated)
    assumption = brief["calculation_reviews"][0]
    assert assumption["status"] == "staff_adjusted" and assumption["reviewed_value"] == 4.5
    assert assumption["adjustments"] == [{"label": "Reviewed nonrecurring expense", "amount": 1000}]
    assert all(text not in json.dumps(brief) for text in ("PRIVATE-ID", "PRIVATE-REASON", "PRIVATE-ACTOR", "PRIVATE-RECEIPT"))
    safe = reviews.redact_for_external(updated, SimpleNamespace(role=Role.CLIENT))
    assert safe["dscr"]["headline"]["value"] == 4.5
    assert all("recipe" not in row for row in safe["metric_review"]["calculations"])
    assert safe["metric_review"]["composition_history"] == []
    assert "Reviewed nonrecurring expense" not in json.dumps(safe)
    assert "PRIVATE-" not in json.dumps(safe)
