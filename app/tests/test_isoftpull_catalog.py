from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException

from app.services import isoftpull_catalog as catalog


def test_applicant_catalog_ignores_new_pull_links_and_reads_rows():
    soup = BeautifulSoup('<table><tbody><tr><th><a href="/client/applicants/123">Example Person</a><a href="/client/applicants/123">View</a></th><td>NA</td><td>Source</td><td>Active</td><td>Staff</td><td>11/05/2026</td></tr></tbody></table><a href="/client/reports/new">New</a>', "html.parser")
    assert catalog.parse_applicants(soup) == [{"id": "123", "name": "Example Person", "status": "Active", "updated": "11/05/2026"}]


async def test_provider_search_uses_get_and_explicit_archive_filter(monkeypatch):
    fetch = AsyncMock(return_value=SimpleNamespace(status_code=200, text='<a rel="next" href="?page=2">Next</a>'))
    monkeypatch.setattr(catalog, "get_session", lambda: SimpleNamespace(fetch=fetch))
    result = await catalog.applicants("Example & Test", 2, True)
    url = fetch.await_args.args[0]
    assert "query=Example+%26+Test" in url and "page=2" in url and "archived" in url
    assert result["has_next"]


async def test_report_must_belong_to_selected_provider_applicant(monkeypatch):
    monkeypatch.setattr(catalog, "reports", AsyncMock(return_value=([], "")))
    fetch = AsyncMock()
    monkeypatch.setattr(catalog, "page", fetch)
    with pytest.raises(ValueError, match="not available"):
        await catalog.retrieve("123", str(uuid4()), "soft_pull")
    fetch.assert_not_awaited()


async def test_catalog_never_selects_the_new_report_route(monkeypatch):
    identifier = str(uuid4())
    soup = BeautifulSoup(f'<a href="/client/reports/new?id=123">New pull</a><a href="/client/reports/{identifier}">Existing</a><a href="/client/reports/{identifier}">Duplicate</a>', "html.parser")
    monkeypatch.setattr(catalog, "page", AsyncMock(return_value=(soup, "")))
    rows, _ = await catalog.reports("123")
    assert len(rows) == 1 and rows[0]["id"] == identifier
    with pytest.raises(ValueError):
        await catalog.reports("../new")


@pytest.mark.parametrize("role", ["client", "broker", "dealer_partner", "field_rep"])
async def test_provider_wide_catalog_is_not_available_outside_internal_staff(role):
    from app.routers.application_profiles import provider_credit_applicants

    db = SimpleNamespace(get=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await provider_credit_applicants(uuid4(), uuid4(), SimpleNamespace(role=role), db=db)
    assert error.value.status_code == 403
    db.get.assert_not_awaited()


async def test_provider_import_requires_explicit_identity_confirmation(monkeypatch):
    from app.routers import application_profiles as routes
    from app.schemas.application_profile import ProviderCreditLinkRequest

    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=SimpleNamespace(client_id=uuid4())))
    monkeypatch.setattr(routes, "_owner_for_profile", AsyncMock(return_value=SimpleNamespace()))
    fetch = AsyncMock()
    monkeypatch.setattr(catalog, "retrieve", fetch)
    with pytest.raises(HTTPException) as error:
        await routes.link_provider_credit(uuid4(), uuid4(), ProviderCreditLinkRequest(applicant_id="123", report_id=uuid4()), SimpleNamespace(role="super_admin"), SimpleNamespace())
    assert error.value.status_code == 422
    fetch.assert_not_awaited()


async def test_provider_import_uses_shared_report_link_and_never_runs_a_pull(monkeypatch):
    from datetime import UTC, datetime

    from app.routers import application_profiles as routes
    from app.schemas.application_profile import ProviderCreditLinkRequest
    from app.services import credit_links, credit_report_archive, isoftpull_client

    profile = SimpleNamespace(id=uuid4(), client_id=uuid4())
    owner = SimpleNamespace(id=uuid4())
    original_date = datetime(2026, 10, 15, tzinfo=UTC)
    parsed = {"personal_info": {"name": "Example Person"}, "scores": [{"score": 680}], "best_score": 680}
    monkeypatch.setattr(routes.profiles, "load_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(routes, "_owner_for_profile", AsyncMock(return_value=owner))
    monkeypatch.setattr(routes.profiles, "owner_read", lambda value: value)
    monkeypatch.setattr(catalog, "retrieve", AsyncMock(return_value=(parsed, "<html>existing report</html>", original_date)))
    monkeypatch.setattr(credit_report_archive, "archive_report_html", lambda *args: "encrypted-archive-key")
    linked = AsyncMock()
    monkeypatch.setattr(credit_links, "link_pull", linked)
    new_pull = AsyncMock(side_effect=AssertionError("Never create a bureau inquiry"))
    monkeypatch.setattr(isoftpull_client, "pull", new_pull)
    db = SimpleNamespace(execute=AsyncMock(), scalar=AsyncMock(return_value=None), add=Mock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    result = await routes.link_provider_credit(profile.id, owner.id, ProviderCreditLinkRequest(applicant_id="123", report_id=uuid4(), confirm_identity=True), SimpleNamespace(role="super_admin"), db)
    assert result is owner
    pull = db.add.call_args.args[0]
    assert pull.pulled_at == original_date and pull.client_id == profile.client_id
    assert pull.bureau_response["report_s3_key"] == "encrypted-archive-key"
    linked.assert_awaited_once()
    db.commit.assert_awaited_once()
    new_pull.assert_not_awaited()
