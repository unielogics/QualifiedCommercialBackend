"""Current DealerOS views refresh source math while historical rows stay intact."""
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.dealer_os import router
from app.dealer_os.services import engines
from app.tests.test_dealer_metric_authority import inputs


def dealer():
    now=datetime.now(UTC)
    return SimpleNamespace(id=uuid4(), name="Example business", status="collecting",
                           created_at=now, updated_at=now)


def saved(metrics, *, age=0):
    return SimpleNamespace(id=uuid4(), metrics=metrics, score=75, tier="healthy",
        as_of=date.today()-timedelta(days=age), created_at=datetime.now(UTC)-timedelta(days=age))


def result(*, rows=None, scalar=None):
    query=Mock()
    query.scalars.return_value.all.return_value=rows or []
    query.all.return_value=rows or []
    query.scalar_one_or_none.return_value=scalar
    query.scalar_one.return_value=0
    return query


@pytest.mark.asyncio
async def test_health_displays_the_current_snapshot_selected_by_shared_authority():
    owner=dealer()
    fresh=saved(engines.current_input_metrics(inputs()))
    db=SimpleNamespace(execute=AsyncMock(return_value=result()))
    with patch.object(router, "require_team_or_dealer_or_rep"), \
         patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=owner)), \
         patch.object(router, "_latest_snapshot_row", AsyncMock(return_value=fresh)) as current:
        response=await router.dealer_health(owner.id, SimpleNamespace(), db)
    current.assert_awaited_once_with(db, owner.id)
    assert response.snapshot.id == fresh.id
    assert response.snapshot.metrics["dscr"]["current"] == 9.6


@pytest.mark.asyncio
async def test_package_and_analyst_share_current_snapshot_instead_of_old_stored_math():
    owner=dealer()
    fresh=saved(engines.current_input_metrics(inputs()))
    db=SimpleNamespace(execute=AsyncMock(return_value=result()))
    with patch.object(router, "_latest_snapshot_row", AsyncMock(return_value=fresh)) as current, \
         patch.object(router, "_global_program_settings", AsyncMock(return_value={})), \
         patch.object(router, "compute_paths", return_value=[]):
        response=await router._build_lender_package(db, owner)
    current.assert_awaited_once_with(db, owner.id)
    assert response.snapshot.id == fresh.id
    assert response.snapshot.metrics["dscr"]["current"] == 9.6


@pytest.mark.asyncio
async def test_progress_refreshes_current_row_then_compares_preserved_history():
    owner=dealer()
    current=saved(engines.current_input_metrics(inputs()))
    historical=saved({"dscr": {"current": 2.0}}, age=30)
    original=deepcopy(historical.metrics)
    calls=[]

    async def execute(statement):
        calls.append("history" if "dos_metric_snapshots" in str(statement) else "actions")
        return result(rows=[current, historical] if "dos_metric_snapshots" in str(statement) else [])

    async def refresh(db, key):
        calls.append("refresh-current")
        return current

    db=SimpleNamespace(execute=AsyncMock(side_effect=execute))
    with patch.object(router, "require_team_or_dealer_or_rep"), \
         patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=owner)), \
         patch.object(router, "_latest_snapshot_row", AsyncMock(side_effect=refresh)):
        response=await router.dealer_progress(owner.id, SimpleNamespace(), db)
    assert calls[:2] == ["refresh-current", "history"]
    assert response.deltas["dscr"].model_dump(by_alias=True) == {"from": 2.0, "to": 9.6, "delta": 7.6}
    assert historical.metrics == original


@pytest.mark.asyncio
async def test_batched_list_hides_old_policy_scores_without_per_dealer_recalculation():
    first,second=dealer(),dealer()
    old=saved({"dscr": {"current": 99}})
    old.dealer_id=first.id
    current=saved(engines.current_input_metrics(inputs()))
    current.dealer_id=second.id
    replies=[result(rows=[(first, None), (second, None)]), result(rows=[old, current]),
             result(), result(), result(), result(), result(), result()]
    db=SimpleNamespace(execute=AsyncMock(side_effect=replies))
    with patch.object(router, "require_team_or_dealer_or_rep"), \
         patch.object(router, "is_audit_client", return_value=False), \
         patch.object(router, "is_rep", return_value=False), \
         patch.object(engines, "load_metric_inputs", AsyncMock(side_effect=AssertionError("N+1 recalculation"))) as load:
        response=await router.list_dealers(SimpleNamespace(), db)
    assert response[0].score is None and response[0].tier is None
    assert response[1].score == 75 and response[1].tier == "healthy"
    assert old.metrics == {"dscr": {"current": 99}}
    load.assert_not_awaited()
    assert db.execute.await_count <= 10
