from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import plaid_transfer


@pytest.fixture(autouse=True)
def _payment_plaid_environment(monkeypatch):
    monkeypatch.setenv("PAYMENTS_PLAID_CLIENT_ID", "payments-client")
    monkeypatch.setenv("PAYMENTS_PLAID_SECRET", "payments-secret")
    monkeypatch.setenv("PAYMENTS_PLAID_ENV", "production")
    monkeypatch.setenv("PAYMENTS_PLAID_CLIENT_NAME", "Qualified Commercial")
    monkeypatch.setenv(
        "PAYMENTS_PLAID_WEBHOOK_URL", "https://api.example.test/api/v1/webhooks/plaid"
    )
    monkeypatch.setenv(
        "PAYMENTS_PLAID_REDIRECT_URI", "https://app.example.test/payments/plaid/oauth"
    )


@pytest.mark.asyncio
async def test_initial_payment_link_is_transfer_only(monkeypatch):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"link_token": "payment-link"}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    token = await plaid_transfer.create_link_token(client_user_id="obligation-1")

    assert token == "payment-link"
    assert captured["path"] == "/link/token/create"
    assert captured["payload"]["products"] == ["transfer"]
    assert captured["payload"]["webhook"].endswith("/api/v1/webhooks/plaid")
    assert captured["payload"]["redirect_uri"].endswith("/payments/plaid/oauth")


@pytest.mark.asyncio
async def test_payment_update_link_never_reinitializes_transfer(monkeypatch):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"link_token": "payment-update-link"}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    await plaid_transfer.create_link_token(
        client_user_id="obligation-1", access_token="payment-access"
    )

    assert captured["payload"]["access_token"] == "payment-access"
    assert "products" not in captured["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ach_class", ["ccd", "web"])
async def test_transfer_authorization_uses_server_selected_ach_class(
    monkeypatch, ach_class: plaid_transfer.AchClass
):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"authorization": {"id": "auth-1", "decision": "approved"}}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    result = await plaid_transfer.create_authorization(
        access_token="payment-access",
        account_id="account-1",
        amount=Decimal("1250.50"),
        ach_class=ach_class,
        legal_name="Northstar Holdings LLC",
        email="owner@example.test",
        idempotency_key="authorization:" + "x" * 80,
    )

    assert result["id"] == "auth-1"
    assert captured["path"] == "/transfer/authorization/create"
    assert captured["payload"]["ach_class"] == ach_class
    assert captured["payload"]["amount"] == "1250.50"
    assert captured["payload"]["user"]["legal_name"] == "Northstar Holdings LLC"
    assert captured["payload"]["user_present"] is False
    assert len(captured["payload"]["idempotency_key"]) <= 50


@pytest.mark.asyncio
async def test_transfer_create_uses_short_statement_description_and_non_pii_metadata(
    monkeypatch,
):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"transfer": {"id": "transfer-1", "status": "pending"}}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    await plaid_transfer.create_transfer(
        access_token="payment-access",
        account_id="account-1",
        authorization_id="auth-1",
        amount="90.00",
        description="QC Origination Fees",
        metadata={"payment_transfer_id": "ledger-1", "non_ascii": "café"},
    )

    assert captured["path"] == "/transfer/create"
    assert len(captured["payload"]["description"]) <= 10
    assert captured["payload"]["metadata"] == {"payment_transfer_id": "ledger-1"}


@pytest.mark.asyncio
async def test_ambiguous_transfer_lookup_uses_authorization_id(monkeypatch):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"transfer": {"id": "transfer-1", "status": "pending"}}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    transfer = await plaid_transfer.get_transfer_by_authorization("auth-1")

    assert transfer == {"id": "transfer-1", "status": "pending"}
    assert captured == {
        "path": "/transfer/get",
        "payload": {"authorization_id": "auth-1"},
    }


@pytest.mark.asyncio
async def test_ledger_available_balance_reads_the_exact_original_ledger(monkeypatch):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {
            "ledger_id": "ledger-1",
            "balance": {"available": "1250.50", "pending": "99.00"},
        }

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)

    available = await plaid_transfer.get_ledger_available_balance(
        ledger_id="ledger-1",
        originator_client_id="originator-1",
    )

    assert available == Decimal("1250.50")
    assert captured == {
        "path": "/transfer/ledger/get",
        "payload": {
            "ledger_id": "ledger-1",
            "originator_client_id": "originator-1",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            {"ledger_id": "another-ledger", "balance": {"available": "50.00"}},
            "PLAID_REFUND_LEDGER_MISMATCH",
        ),
        (
            {"ledger_id": "ledger-1", "balance": {"available": "not-money"}},
            "PLAID_REFUND_LEDGER_BALANCE_INVALID",
        ),
        (
            {"ledger_id": "ledger-1", "balance": {"available": "NaN"}},
            "PLAID_REFUND_LEDGER_BALANCE_INVALID",
        ),
    ],
)
async def test_ledger_balance_response_fails_closed(
    monkeypatch, response, expected_code
):
    async def fake_post(_path, _payload, **_kwargs):
        return response

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)

    with pytest.raises(plaid_transfer.PlaidTransferError) as exc_info:
        await plaid_transfer.get_ledger_available_balance(ledger_id="ledger-1")

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_missing_authorization_transfer_is_a_definite_empty_lookup(monkeypatch):
    async def fake_post(_path, _payload, **_kwargs):
        raise plaid_transfer.PlaidTransferError(
            "not found", code="TRANSFER_NOT_FOUND", retryable=False
        )

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)

    assert await plaid_transfer.get_transfer_by_authorization("auth-1") is None


@pytest.mark.asyncio
async def test_generic_not_found_is_a_definite_empty_lookup(monkeypatch):
    async def fake_post(_path, _payload, **_kwargs):
        raise plaid_transfer.PlaidTransferError(
            "not found", code="NOT_FOUND", retryable=False
        )

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)

    assert await plaid_transfer.get_transfer_by_authorization("auth-1") is None


def test_payment_provider_does_not_fall_back_to_evidence_credentials(monkeypatch):
    monkeypatch.delenv("PAYMENTS_PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYMENTS_PLAID_SECRET", raising=False)
    monkeypatch.setenv("DEALER_OS_PLAID_CLIENT_ID", "evidence-client")
    monkeypatch.setenv("DEALER_OS_PLAID_SECRET", "evidence-secret")

    assert plaid_transfer.enabled() is False


@pytest.mark.asyncio
async def test_event_sync_is_cursor_based_and_bounded(monkeypatch):
    captured: dict = {}

    async def fake_post(path, payload, **_kwargs):
        captured.update(path=path, payload=payload)
        return {"transfer_events": [{"event_id": 42}], "has_more": True}

    monkeypatch.setattr(plaid_transfer, "_post", fake_post)
    events, has_more = await plaid_transfer.sync_events(after_id=41, count=900)

    assert events == [{"event_id": 42}]
    assert has_more is True
    assert captured == {
        "path": "/transfer/event/sync",
        "payload": {"after_id": 41, "count": 500},
    }
