"""Dealer terms preserve and expose the same dates as their saved loan plan."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import production_packages
from app.services import production_term_sheets as sheets
from app.tests.test_production_combined_financed_save import env as env
from app.tests.test_production_combined_financed_save import payload


@pytest.mark.asyncio
async def test_dealer_saved_dates_are_read_and_adopted_and_legacy_manual_edit_is_preserved(env):
    body = payload(env, expected_funding_date="2026-01-31", maturity_date_mode="automatic")
    first, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=body, request=None)
    await env.db.commit()
    view = production_packages._term_sheet_read(first, {})
    assert view.expected_funding_date == date(2026, 1, 31)
    assert view.maturity_date == date(2028, 1, 31) and view.maturity_date_mode == "automatic"
    assert "Estimated" in view.date_note
    assert env.profile.financial_plan["new_loan_maturity_date"] == "2028-01-31"

    revised = payload(env, expected_version=1, expected_deal_budget_revision=1,
                      idempotency_key="dealer-date-save-2", maturity_date="2029-06-15")
    revised["deal_budget"] = {**deepcopy(body["deal_budget"]), "expected_revision": 1}
    second, _ = await sheets.record_sheet(env.db, profile=env.profile, user=env.actor, body=revised, request=None)
    assert second.expected_funding_date == first.expected_funding_date
    assert second.maturity_date == date(2029, 6, 15)
    assert second.extra["maturity_date_mode"] == "manual"
    assert env.profile.financial_plan["new_loan_maturity_date"] == "2029-06-15"
    assert first.maturity_date == date(2028, 1, 31)


@pytest.mark.asyncio
async def test_dealer_prefill_retains_stage_one_manual_maturity_without_plan_dates(env):
    env.profile.financial_plan.update(new_loan_amount=100000, new_loan_term_months=12)
    parent = SimpleNamespace(frozen_revision_id=None, status="draft", arrangement={
        "funding_date": "2026-01-31", "maturity": "2028-06-15", "term": 12})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    result = await sheets.defaults(db, env.profile, parent)
    assert result["values"]["expected_funding_date"] == "2026-01-31"
    assert result["values"]["maturity_date"] == "2028-06-15"
    assert result["values"]["maturity_date_mode"] == "manual"
