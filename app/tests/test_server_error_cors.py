from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import server_errors


@pytest.mark.parametrize("origin", ["https://app.qualifiedcommercial.com", "https://unapproved.example", None])
async def test_unhandled_errors_keep_cors_only_for_approved_origins(monkeypatch, origin):
    allowed = "https://app.qualifiedcommercial.com"
    monkeypatch.setattr(server_errors, "get_settings", lambda: SimpleNamespace(cors_origins_list=[allowed]))
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=[allowed], allow_credentials=True)
    app.add_exception_handler(Exception, server_errors.server_error_response)

    @app.get("/failure")
    async def failure():
        raise RuntimeError("Internal information must stay out of the response")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.example") as client:
        response = await client.get("/failure", headers={"Origin": origin} if origin else {})

    assert response.status_code == 500
    assert response.json() == {"detail": "An unexpected server error occurred."}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Origin"
    if origin == allowed:
        assert response.headers["access-control-allow-origin"] == allowed
        assert response.headers["access-control-allow-credentials"] == "true"
    else:
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers
