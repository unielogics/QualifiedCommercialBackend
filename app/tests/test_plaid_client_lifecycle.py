from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.dealer_os.services import plaid_client


@pytest.fixture(autouse=True)
def _plaid_environment(monkeypatch):
    monkeypatch.setenv("DEALER_OS_PLAID_CLIENT_ID", "client-id")
    monkeypatch.setenv("DEALER_OS_PLAID_SECRET", "secret")
    monkeypatch.setenv("DEALER_OS_PLAID_ENV", "production")
    monkeypatch.setenv("DEALER_OS_PLAID_PRODUCTS", "statements,assets")
    monkeypatch.setenv("DEALER_OS_PLAID_CLIENT_NAME", "Qualified Commercial")
    monkeypatch.setenv("DEALER_OS_PLAID_WEBHOOK_URL", "https://api.example.test/plaid")


@pytest.mark.asyncio
async def test_initial_link_uses_fixed_platform_name_and_selected_products(monkeypatch):
    captured = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return SimpleNamespace(json=lambda: {"link_token": "link-production"})

    monkeypatch.setattr(plaid_client, "_post", fake_post)
    token = await plaid_client.create_link_token(
        dealer_id="file-1", dealer_name="Northstar Holdings LLC"
    )

    assert token == "link-production"
    assert captured["path"] == "/link/token/create"
    assert captured["payload"]["client_name"] == "Qualified Commercial"
    assert captured["payload"]["products"] == ["assets", "statements"]
    assert captured["payload"]["webhook"] == "https://api.example.test/plaid"


@pytest.mark.asyncio
async def test_assets_only_link_does_not_request_unentitled_statements(monkeypatch):
    captured = {}
    monkeypatch.setenv("DEALER_OS_PLAID_PRODUCTS", "assets")

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return SimpleNamespace(json=lambda: {"link_token": "link-assets"})

    monkeypatch.setattr(plaid_client, "_post", fake_post)
    token = await plaid_client.create_link_token(
        dealer_id="file-1", dealer_name="Northstar Holdings LLC"
    )

    assert token == "link-assets"
    assert captured["payload"]["products"] == ["assets"]
    assert "statements" not in captured["payload"]
    assert plaid_client.assets_enabled() is True
    assert plaid_client.statements_enabled() is False


@pytest.mark.asyncio
async def test_initial_link_never_exposes_a_long_customer_name(monkeypatch):
    captured = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return SimpleNamespace(json=lambda: {"link_token": "link-production"})

    monkeypatch.setattr(plaid_client, "_post", fake_post)
    await plaid_client.create_link_token(
        dealer_id="file-1",
        dealer_name="Very Long Operating Company LLC",
    )

    assert captured["payload"]["client_name"] == "Qualified Commercial"


@pytest.mark.asyncio
async def test_update_link_can_request_new_account_selection(monkeypatch):
    captured = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return SimpleNamespace(json=lambda: {"link_token": "update-production"})

    monkeypatch.setattr(plaid_client, "_post", fake_post)
    token = await plaid_client.create_update_link_token(
        access_token="access-production",
        client_user_id="file-1",
        display_name="Northstar Holdings LLC",
        account_selection_enabled=True,
    )

    assert token == "update-production"
    assert captured["path"] == "/link/token/create"
    assert captured["payload"]["access_token"] == "access-production"
    assert captured["payload"]["client_name"] == "Qualified Commercial"
    assert captured["payload"]["update"] == {"account_selection_enabled": True}


@pytest.mark.asyncio
async def test_assets_and_item_removal_use_production_endpoints(monkeypatch):
    calls = []

    async def fake_post(path, payload, **_kwargs):
        calls.append((path, payload))
        if path == "/asset_report/create":
            return SimpleNamespace(
                json=lambda: {
                    "asset_report_id": "report-1",
                    "asset_report_token": "report-token-1",
                }
            )
        return SimpleNamespace(json=lambda: {})

    monkeypatch.setattr(plaid_client, "_post", fake_post)
    report_id, report_token = await plaid_client.asset_report_create(
        ["access-1", "access-2"], client_report_id="file-1", days_requested=90
    )
    await plaid_client.asset_report_remove(report_token)
    await plaid_client.item_remove("access-1")

    assert (report_id, report_token) == ("report-1", "report-token-1")
    assert calls[0][0] == "/asset_report/create"
    assert calls[0][1]["access_tokens"] == ["access-1", "access-2"]
    assert calls[0][1]["options"]["webhook"] == "https://api.example.test/plaid"
    assert calls[1] == ("/asset_report/remove", {"asset_report_token": "report-token-1"})
    assert calls[2] == ("/item/remove", {"access_token": "access-1"})


def test_production_environment_never_uses_the_sandbox_host():
    assert plaid_client.environment() == "production"
    assert plaid_client._base() == "https://production.plaid.com"
