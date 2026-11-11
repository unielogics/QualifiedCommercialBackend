from contextlib import asynccontextmanager

import pytest

from app.tests.test_capital_partners import Scenario, harness


@asynccontextmanager
async def isolated_capital():
    value = await harness()
    try:
        async with value:
            yield value
    finally:
        engine = value.db.bind
        await value.db.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_listing_auto_dates_follow_saved_funding_and_term_and_manual_override_is_preserved():
    async with isolated_capital() as h:
        scenario = Scenario(h)
        row = await scenario.listing(await scenario.admin(), publish=False)
        path = f"/api/v1/capital/admin/listings/{row.id}"
        result = await h.client.patch(path, json={"expected_funding_date": "2024-01-31", "term_months": 1, "maturity_date_mode": "automatic"})
        assert result.status_code == 200, result.text
        assert result.json()["maturity_date"] == "2024-02-29"
        assert result.json()["expected_funding_date"] == "2024-01-31"
        result = await h.client.patch(path, json={"term_months": 12})
        assert result.status_code == 200, result.text
        assert result.json()["maturity_date"] == "2025-01-31"
        result = await h.client.patch(path, json={"maturity_date": "2026-06-15", "maturity_date_mode": "manual"})
        assert result.status_code == 200, result.text
        assert result.json()["maturity_date"] == "2026-06-15"
        result = await h.client.patch(path, json={"expected_funding_date": "2024-02-28", "term_months": 24})
        assert result.status_code == 200, result.text
        assert result.json()["maturity_date"] == "2026-06-15"
        assert result.json()["maturity_date_mode"] == "manual"
        result = await h.client.patch(path, json={"maturity_date_mode": "automatic"})
        assert result.status_code == 200, result.text
        assert result.json()["maturity_date"] == "2026-02-28"
        assert "Estimated" in result.json()["date_note"]


@pytest.mark.asyncio
async def test_invalid_dates_identify_a_field_and_funded_dates_cannot_be_repriced():
    async with isolated_capital() as h:
        scenario = Scenario(h)
        row = await scenario.listing(await scenario.admin(), publish=False)
        path = f"/api/v1/capital/admin/listings/{row.id}"
        invalid = await h.client.patch(path, json={"expected_funding_date": "2026-06-01", "maturity_date": "2026-05-01", "maturity_date_mode": "manual"})
        assert invalid.status_code == 422
        assert invalid.json()["detail"][0]["loc"] == ["body", "maturity_date"]
        await h.db.rollback()
        row = await scenario.listing(await scenario.admin(), publish=False)
        path = f"/api/v1/capital/admin/listings/{row.id}"
        row.status = "funded"
        await h.db.commit()
        blocked = await h.client.patch(path, json={"expected_funding_date": "2026-06-01", "maturity_date_mode": "automatic"})
        assert blocked.status_code == 409
        assert "committed or funded" in blocked.json()["detail"]
