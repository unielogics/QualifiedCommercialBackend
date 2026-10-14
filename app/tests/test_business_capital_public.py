import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.db import get_db
from app.routers import business_capital
from app.services import funding_programs


@pytest.mark.asyncio
async def test_public_catalog_is_source_free_and_needs_no_source_approval():
    app = FastAPI()
    app.include_router(business_capital.public_router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/public/business-capital/catalog")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["products"]) == 4
    assert len(payload["industries"]) == 3
    assert all(set(item) == {"slug", "title", "description", "features"} for item in payload["products"])
    text = json.dumps(payload).lower()
    for private_term in ("arf", "bankroll", "equifax", "beacon", "source_version", "program_key", "lender_id"):
        assert private_term not in text
    assert not any(character.isdigit() for character in text)


@pytest.mark.asyncio
async def test_contact_request_maps_slugs_only_on_server_and_returns_no_source():
    app = FastAPI()
    app.include_router(business_capital.public_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: object()
    with patch.object(business_capital, "save_contact_referral", new_callable=AsyncMock) as save:
        save.return_value = {"status": "received", "message": "Your financing review request is with our team."}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/public/business-capital/referrals", json={
                "idempotency_key": str(uuid4()), "full_name": "Test Business", "email": "test@example.com",
                "consent": True, "product_slug": "interest-only-business-loan", "industry_slug": "hvac-business",
            })
        assert response.status_code == 201
        assert "arf" not in response.text.lower()
        saved = save.await_args.args[0]
        assert saved.program_key == "arf_io_flex_pay"
        assert saved.industry_key == "hvac"


def test_contact_intake_cannot_accept_financial_fields_or_private_keys():
    values = {"idempotency_key": uuid4(), "full_name": "Test Business", "email": "test@example.com", "consent": True}
    for additional in ({"annual_revenue": 100000}, {"program_key": "arf_bankroll"}, {"product_slug": "arf_bankroll"}, {"consent": False}):
        with pytest.raises(ValidationError):
            business_capital.ContactRequest(**(values | additional))


@pytest.mark.asyncio
async def test_source_products_never_leak_through_general_public_catalog():
    db = AsyncMock()
    with patch.object(funding_programs, "catalog_rows", AsyncMock(return_value=[SimpleNamespace(program_key="arf_bankroll")])), \
         patch.object(funding_programs, "scopes_by_program", AsyncMock(return_value={})), \
         patch.object(funding_programs, "commercial_terms_by_program", AsyncMock(return_value={})):
        assert await funding_programs.public_catalog(db) == []
