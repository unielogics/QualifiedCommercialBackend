from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import plaid_policy


def _item(**overrides):
    values = {
        "status": "active",
        "plaid_products": [],
        "plaid_unavailable_products": [],
        "plaid_products_checked_at": object(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_policy_supports_all_three_valid_modes(monkeypatch):
    monkeypatch.setattr(
        plaid_policy.plaid_client, "products", lambda: ["assets", "statements"]
    )

    for policy, expected in (
        (plaid_policy.PlaidProductPolicy(True, False), ["assets"]),
        (plaid_policy.PlaidProductPolicy(False, True), ["statements"]),
        (
            plaid_policy.PlaidProductPolicy(True, True),
            ["assets", "statements"],
        ),
    ):
        policy.validate()
        assert policy.selected_products == expected


def test_policy_refuses_disabling_both_products():
    with pytest.raises(plaid_policy.InvalidPlaidPolicy):
        plaid_policy.PlaidProductPolicy(False, False).validate()


def test_policy_refuses_a_product_outside_the_deployment_allowlist(monkeypatch):
    monkeypatch.setattr(plaid_policy.plaid_client, "products", lambda: ["assets"])

    with pytest.raises(plaid_policy.PlaidProductUnavailable) as caught:
        plaid_policy.PlaidProductPolicy(True, True).validate()

    assert caught.value.unavailable == ["statements"]
    assert caught.value.available == ["assets"]


def test_item_authorization_tracks_pending_and_fallback_products():
    policy = plaid_policy.PlaidProductPolicy(True, True)
    item = _item(plaid_products=["assets"])

    assert plaid_policy.pending_products(item, policy) == ["statements"]
    assert plaid_policy.authorization_state(item, policy) == "client_authorization_required"

    plaid_policy.mark_optional_statements_unavailable(item, policy)

    assert plaid_policy.pending_products(item, policy) == []
    assert plaid_policy.authorization_state(item, policy) == "fallback_required"


def test_statements_only_can_use_the_pdf_fallback():
    policy = plaid_policy.PlaidProductPolicy(False, True)
    item = _item()

    plaid_policy.mark_optional_statements_unavailable(item, policy)

    assert item.plaid_unavailable_products == ["statements"]
    assert plaid_policy.pending_products(item, policy) == []
    assert plaid_policy.authorization_state(item, policy) == "fallback_required"


@pytest.mark.asyncio
async def test_linked_profile_uses_dealer_policy_as_authority():
    dealer = SimpleNamespace(
        plaid_assets_enabled=False,
        plaid_statements_enabled=True,
    )
    profile = SimpleNamespace(
        dealer_id="dealer-1",
        plaid_assets_enabled=True,
        plaid_statements_enabled=False,
    )

    class Db:
        async def get(self, _model, key):
            assert key == "dealer-1"
            return dealer

    policy, owner = await plaid_policy.for_profile(Db(), profile)

    assert owner is dealer
    assert policy.selected_products == ["statements"]


@pytest.mark.asyncio
async def test_handoff_preserves_the_latest_explicit_policy():
    now = datetime.now(UTC)
    dealer = SimpleNamespace(
        plaid_assets_enabled=True,
        plaid_statements_enabled=False,
        plaid_policy_updated_at=now - timedelta(hours=1),
        plaid_policy_updated_by_user_id="dealer-actor",
    )
    profile = SimpleNamespace(
        plaid_assets_enabled=False,
        plaid_statements_enabled=True,
        plaid_policy_updated_at=now,
        plaid_policy_updated_by_user_id="profile-actor",
    )

    await plaid_policy.copy_latest_policy_on_handoff(dealer, profile)

    assert dealer.plaid_assets_enabled is False
    assert dealer.plaid_statements_enabled is True
    assert dealer.plaid_policy_updated_at == now
    assert dealer.plaid_policy_updated_by_user_id == "profile-actor"
