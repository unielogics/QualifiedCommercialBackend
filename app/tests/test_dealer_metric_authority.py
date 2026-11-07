"""Saved DealerOS metrics follow current operands without rewriting history."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.dealer_os.models import DealerMetricLineage, DealerMetricSnapshot
from app.dealer_os.services import engines


def inputs(**extra):
    period = SimpleNamespace(id=uuid4(), period=date(2026, 10, 1))
    return engines.MetricInputs(period_rows=[period], periods=[{"period": date(2026, 10, 1),
        "ebitda_reported": 10000, "debt_service": 1000, "avg_daily_balance": 15000,
        "ending_balance": 20000, "low_balance": 5000, "nsf_count": 0}],
        addback_rows=[], target_rows=[], targets={"dscr_target": 1.25, "dscr_floor": 1.1},
        fallbacks={}, source_dependencies={"published_debt_version_id": "v2"}, **extra)


def snapshot(metrics):
    return SimpleNamespace(id=uuid4(), metrics=metrics)


def result(row=None):
    value=Mock()
    value.scalar_one_or_none.return_value=row
    return value


def test_policy_and_effective_source_changes_invalidate_saved_metrics_without_changing_math():
    source=inputs()
    metrics=engines.current_input_metrics(source)
    saved=snapshot(metrics)
    assert engines.metric_snapshot_is_current(saved, source)
    assert metrics["ebitda"]["bankable"] == 115200  # Existing 4% haircut is unchanged.
    assert metrics["dscr"]["current"] == 9.6
    assert metrics["dscr"]["target"] == 1.25
    changed=deepcopy(source)
    changed.source_dependencies["published_debt_version_id"]="v3"
    assert not engines.metric_snapshot_is_current(saved, changed)
    assert engines.current_input_metrics(changed)["dscr"]["current"] == 9.6
    with patch("app.services.financial_calculation_policy.CALCULATION_POLICY_VERSION", "next-math-policy"):
        assert not engines.metric_snapshot_is_current(saved, source)
    assert engines.metric_snapshot_is_current(saved, source)


@pytest.mark.asyncio
async def test_unsigned_old_metrics_are_replaced_on_read_without_writing_or_mutating_history():
    source=inputs()
    old=snapshot({"dscr": {"current": 99}, "ebitda": {"bankable": 999999}})
    before=deepcopy(old.metrics)
    db=SimpleNamespace(add=Mock(), flush=AsyncMock())
    with patch.object(engines, "load_metric_inputs", AsyncMock(return_value=source)):
        current=await engines.current_snapshot_metrics(db, uuid4(), old)
    assert current["dscr"]["current"] == 9.6
    assert old.metrics == before
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    assert current["_financial_calculation"]["source_signature"]


@pytest.mark.asyncio
async def test_matching_saved_metrics_are_reused_without_recompute_or_append():
    source=inputs()
    old=snapshot(engines.current_input_metrics(source))
    db=SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock())
    with patch.object(engines, "load_metric_inputs", AsyncMock(return_value=source)), \
         patch.object(engines, "recompute_snapshot", AsyncMock()) as recompute:
        assert await engines.current_snapshot_metrics(db, uuid4(), old) is old.metrics
        assert await engines.current_snapshot_row(db, uuid4(), old) is old
    db.execute.assert_not_awaited()
    recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_lineage_refresh_appends_a_quiet_current_snapshot_and_preserves_old_row():
    source=inputs()
    old=snapshot({"dscr": {"current": 99}})
    fresh=snapshot(engines.current_input_metrics(source))
    db=SimpleNamespace(execute=AsyncMock(side_effect=[result(), result(old)]))
    before=deepcopy(old.metrics)
    dealer_id=uuid4()
    with patch.object(engines, "load_metric_inputs", AsyncMock(return_value=source)), \
         patch.object(engines, "recompute_snapshot", AsyncMock(return_value=fresh)) as recompute:
        assert await engines.current_snapshot_row(db, dealer_id, old) is fresh
    recompute.assert_awaited_once_with(db, dealer_id, inputs=source, emit_alerts=False)
    assert old.metrics == before


@pytest.mark.asyncio
async def test_concurrent_cache_reader_rechecks_after_lock_and_uses_first_refresh():
    source=inputs()
    old=snapshot({"dscr": {"current": 99}})
    refreshed=snapshot(engines.current_input_metrics(source))
    db=SimpleNamespace(execute=AsyncMock(side_effect=[result(), result(refreshed)]))
    with patch.object(engines, "load_metric_inputs", AsyncMock(return_value=source)), \
         patch.object(engines, "recompute_snapshot", AsyncMock()) as recompute:
        assert await engines.current_snapshot_row(db, uuid4(), old) is refreshed
    recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_quiet_recompute_persists_policy_stamp_and_lineage_but_never_raises_alerts():
    source=inputs()
    db=SimpleNamespace(add=Mock(), flush=AsyncMock())
    with patch.object(engines, "_ensure_alert", AsyncMock()) as alert:
        row=await engines.recompute_snapshot(db, uuid4(), inputs=source, emit_alerts=False)
    assert isinstance(row, DealerMetricSnapshot)
    assert row.metrics["_financial_calculation"] == engines.metric_input_authority(source)
    assert any(isinstance(call.args[0], DealerMetricLineage) for call in db.add.call_args_list)
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_current_input_load_never_falls_back_to_old_numbers():
    old=snapshot({"dscr": {"current": 99}})
    with patch.object(engines, "load_metric_inputs", AsyncMock(side_effect=RuntimeError("Source unavailable"))):
        with pytest.raises(RuntimeError, match="Source unavailable"):
            await engines.current_snapshot_metrics(SimpleNamespace(), uuid4(), old)


@pytest.mark.asyncio
async def test_router_read_helpers_delegate_currentness_without_changing_scope_or_empty_behavior():
    from fastapi import HTTPException

    from app.dealer_os import router

    dealer_id=uuid4()
    old=snapshot({"dscr": {"current": 99}})
    db=SimpleNamespace(execute=AsyncMock(return_value=result(old)))
    with patch.object(engines, "current_snapshot_metrics", AsyncMock(return_value={"dscr": {"current": 1}})) as current:
        assert await router._latest_snapshot_metrics(db, dealer_id) == {"dscr": {"current": 1}}
        current.assert_awaited_once_with(db, dealer_id, old)
    with patch.object(engines, "current_snapshot_row", AsyncMock(return_value=old)) as current:
        assert await router._latest_snapshot_row(db, dealer_id) is old
        current.assert_awaited_once_with(db, dealer_id, old)
    db.execute.return_value=result(None)
    with pytest.raises(HTTPException) as caught:
        await router._latest_snapshot_metrics(db, dealer_id)
    assert caught.value.status_code == 400
    assert await engines.current_snapshot_row(db, dealer_id, None) is None
