"""Saved client PDFs are downloads of stored versions, never issuance actions."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.routers import application_terms as routes
from app.services.application_terms_pdf import render_terms_html
from app.tests.test_loan_repayment_exports import _terms


@pytest.fixture
def download(monkeypatch):
    row = _terms(id=uuid4(), status="draft", issued_at=None, expires_on=None,
        issued_pdf_bytes=None, issued_filename=None, issued_pdf_sha256=None,
        deal_budget_snapshot={"revision": 1, "currency": "USD", "costs_confirmed": True, "rows": []},
        deal_budget_revision=1, deal_budget_calculation={}, collateral_snapshot={}, collateral_revision=1)
    owner = SimpleNamespace(id=uuid4(), deal_budget={"revision": 99, "target_net_amount": 999999},
        financial_plan={"new_loan": {"amount": 999999}}, financial_metric_review={"revision": 99},
        underwriting_status="in_underwriting")
    actor = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), add=Mock())
    loader = AsyncMock(return_value=owner)
    current = AsyncMock(return_value=row)
    log = AsyncMock()
    captured = {}

    def renderer(stored, **context):
        captured["row"] = stored
        captured["html"] = render_terms_html(stored, **context)
        return b"%PDF-synthetic-saved-v4"

    render = Mock(side_effect=renderer)
    forbidden = {
        "issue": Mock(side_effect=AssertionError("Download cannot issue terms")),
        "save": AsyncMock(side_effect=AssertionError("Download cannot save live terms")),
        "recalculate": AsyncMock(side_effect=AssertionError("Download cannot recalculate current finances")),
        "send": AsyncMock(side_effect=AssertionError("Download cannot send terms")),
    }
    monkeypatch.setattr(routes.profiles, "load_profile", loader)
    monkeypatch.setattr(routes.profiles, "log_profile_action", log)
    monkeypatch.setattr(routes.application_terms, "current_term_sheet", current)
    monkeypatch.setattr(routes.application_terms, "issue", forbidden["issue"])
    monkeypatch.setattr(routes.application_terms, "save_terms", forbidden["save"])
    monkeypatch.setattr(routes.application_terms, "_dscr_context", forbidden["recalculate"])
    monkeypatch.setattr(routes, "send_as_user", forbidden["send"])
    monkeypatch.setattr(routes, "_pdf_context", AsyncMock(return_value=("Example LLC", "Example Owner")))
    monkeypatch.setattr(routes, "render_terms_pdf", render)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    return SimpleNamespace(app=app, row=row, owner=owner, actor=actor, db=db, current=current,
        loader=loader, render=render, log=log, captured=captured, forbidden=forbidden)


async def request(env, version=4):
    async with AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test") as client:
        return await client.get(f"/api/v1/application-profiles/{env.owner.id}/client-terms.pdf",
            params={"expected_version": version})


@pytest.mark.asyncio
async def test_saved_draft_download_uses_exact_stored_terms_without_issuing_saving_or_recalculating(download):
    env = download
    before_row, before_profile = deepcopy(vars(env.row)), deepcopy(vars(env.owner))
    response = await request(env)
    assert response.status_code == 200
    assert response.content == b"%PDF-synthetic-saved-v4"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="Example-LLC-Financing-Terms-v4.pdf"'
    assert env.captured["row"] is env.row
    assert "Draft terms" in env.captured["html"] and "Version 4" in env.captured["html"]
    assert "$120,000.00" in env.captured["html"] and "$999,999" not in env.captured["html"]
    assert vars(env.row) == before_row and vars(env.owner) == before_profile
    env.log.assert_awaited_once()
    assert env.log.call_args.args[3] == "term_sheet.downloaded"
    env.db.commit.assert_awaited_once()  # Download audit only.
    env.db.flush.assert_not_awaited()
    env.db.add.assert_not_called()
    for action in env.forbidden.values():
        action.assert_not_called()


@pytest.mark.asyncio
async def test_issued_download_returns_original_frozen_bytes_even_if_expired_or_renderer_changed(download):
    env = download
    env.row.status = "issued"
    env.row.expires_on = date(2020, 1, 1)
    env.row.issued_pdf_bytes = b"%PDF-original-issued-version"
    env.row.issued_filename = "Original-Client-Terms-v4.pdf"
    before = deepcopy(vars(env.row))
    response = await request(env)
    assert response.status_code == 200
    assert response.content == env.row.issued_pdf_bytes
    assert response.headers["content-disposition"] == 'attachment; filename="Original-Client-Terms-v4.pdf"'
    env.render.assert_not_called()
    env.forbidden["issue"].assert_not_called()
    assert vars(env.row) == before


@pytest.mark.asyncio
async def test_newer_saved_version_conflict_does_not_render_or_write_audit(download):
    response = await request(download, version=3)
    assert response.status_code == 409
    assert "newer terms version" in response.json()["detail"]
    download.render.assert_not_called()
    download.log.assert_not_awaited()
    download.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_saved_version_does_not_export_seeded_preview_or_save_a_version(download):
    download.current.return_value = None
    response = await request(download)
    assert response.status_code == 409
    assert "Save the client terms" in response.json()["detail"]
    download.render.assert_not_called()
    download.db.commit.assert_not_awaited()
    download.forbidden["save"].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.DEALER_PARTNER, Role.FIELD_REP])
async def test_saved_download_requires_the_underwriting_role_before_loading_the_file(download, role):
    download.actor.role = role
    response = await request(download)
    assert response.status_code == 403
    download.loader.assert_not_awaited()
    download.render.assert_not_called()


@pytest.mark.asyncio
async def test_saved_download_keeps_profile_visibility_scope(download):
    download.loader.side_effect = HTTPException(404, "Application file not found")
    response = await request(download)
    assert response.status_code == 404
    download.current.assert_not_awaited()
    download.render.assert_not_called()
    download.db.commit.assert_not_awaited()
