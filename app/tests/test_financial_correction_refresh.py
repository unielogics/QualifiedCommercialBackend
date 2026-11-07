from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.services import financial_metric_review
from app.tests.test_financial_metric_review import ACTOR, build


@asynccontextmanager
async def savepoint():
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
async def test_reviewed_corrections_refresh_consumers_once_and_replays_do_not(changed):
    profile = SimpleNamespace(id=uuid4(), financial_metric_review={}, primary_bucket_id=uuid4())
    query = Mock()
    query.scalar_one.return_value = profile
    db = SimpleNamespace(execute=AsyncMock(return_value=query), flush=AsyncMock(),
                         refresh=AsyncMock(), begin_nested=savepoint)
    state = {"revision": 1, "events": [{"id": str(uuid4())}]}
    with (patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()),
          patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=build())),
          patch.object(financial_metric_review, "change", return_value=(state, changed)),
          patch("app.services.application_profiles.log_profile_action", AsyncMock()),
          patch("app.services.financial_publication.invalidate_financial_review_caches", AsyncMock()) as invalidate,
          patch("app.services.financial_publication.refresh_advisory_projections", AsyncMock()) as refresh):
        await financial_metric_review.correct(db, profile, ACTOR, action="correct")
    if changed:
        invalidate.assert_awaited_once_with(db, profile, correction_revision=1)
        refresh.assert_awaited_once_with(db, profile, event_key=f"financial-correction:{state['events'][-1]['id']}")
        assert profile.financial_metric_review == state
    else:
        invalidate.assert_not_awaited()
        refresh.assert_not_awaited()


def test_program_earnings_select_matching_business_before_latest_period():
    from app.services.program_earnings_facts import annual_ebitda_fact
    from app.tests.test_program_ebitda import ENTITY, pnl

    valid = pnl()
    unrelated = pnl(business_name="Unrelated Business LLC", period_end="2026-09-30", net_income=999999)
    unrelated["file_id"] = "unrelated"
    result, source = annual_ebitda_fact([valid, unrelated], entity=ENTITY)
    assert result == 52000
    assert source["source_refs"][0]["file_id"] == "source"
