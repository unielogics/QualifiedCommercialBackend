"""The Plaid webhook endpoint is unauthenticated by necessity, so its
verification is the only thing standing between a stranger and our records.

The body identifies a connection by `item_id` and nothing else. Without a valid
signature, anyone who learned an item_id could mark a live connection revoked
(denial of service against a borrower's file) or queue syncs at will. These
tests exercise the rejection paths specifically, because a verifier that fails
open is indistinguishable from a working one until it is attacked.

Signatures are generated here with a locally-created EC key and the key fetch is
stubbed, so nothing touches the network.
"""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.dealer_os.services import plaid_client, plaid_webhook

# ── signing helpers ─────────────────────────────────────────────────────────

_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def _sign(body: bytes, *, key=_KEY, alg="ES256", iat=None, body_hash=None) -> str:
    claims = {
        "iat": int(time.time()) if iat is None else iat,
        "request_body_sha256": hashlib.sha256(body).hexdigest() if body_hash is None else body_hash,
    }
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "test-key"})


@pytest.fixture(autouse=True)
def _stub_key(monkeypatch):
    """Serve our test public key where the verifier fetches Plaid's.

    Patching this one seam — rather than the jwt module — keeps the test from
    depending on import order elsewhere in the suite.
    """

    async def _fake_key(key_id: str):
        return _KEY.public_key()

    monkeypatch.setattr(plaid_client, "_verification_key", _fake_key)
    plaid_client._key_cache.clear()
    yield
    plaid_client._key_cache.clear()


BODY = json.dumps({"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "itm_1"}).encode()


# ── verification ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_signature_is_accepted():
    assert await plaid_client.verify_webhook(BODY, _sign(BODY)) is True


@pytest.mark.asyncio
async def test_missing_header_is_rejected():
    assert await plaid_client.verify_webhook(BODY, "") is False


@pytest.mark.asyncio
async def test_garbage_header_is_rejected():
    assert await plaid_client.verify_webhook(BODY, "not-a-jwt") is False


@pytest.mark.asyncio
async def test_a_body_swapped_after_signing_is_rejected():
    """The classic attack: capture a genuine webhook, keep its signature, and
    replace the body with one that revokes a different connection."""
    signature_for_original = _sign(BODY)
    tampered = json.dumps(
        {"webhook_type": "ITEM", "webhook_code": "USER_PERMISSION_REVOKED", "item_id": "itm_victim"}
    ).encode()
    assert await plaid_client.verify_webhook(tampered, signature_for_original) is False


@pytest.mark.asyncio
async def test_a_stale_webhook_is_rejected():
    """Replay of a signature captured earlier."""
    old = _sign(BODY, iat=int(time.time()) - (plaid_client.WEBHOOK_MAX_AGE_SECONDS + 60))
    assert await plaid_client.verify_webhook(BODY, old) is False


@pytest.mark.asyncio
async def test_a_signature_from_the_wrong_key_is_rejected():
    assert await plaid_client.verify_webhook(BODY, _sign(BODY, key=_OTHER_KEY)) is False


@pytest.mark.asyncio
async def test_the_algorithm_is_pinned_to_es256():
    """`alg` is read from the token, so an unpinned verifier can be talked into
    a weaker algorithm by the attacker who supplies the token."""
    hs = jwt.encode(
        {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(BODY).hexdigest()},
        "secret",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    assert await plaid_client.verify_webhook(BODY, hs) is False


# ── handler behaviour ───────────────────────────────────────────────────────


class _FakeDB:
    """Just enough of a session to resolve Dealer and application items."""

    def __init__(self, item, application_item=None):
        self._items = [item, application_item]
        self._calls = 0

    async def execute(self, _stmt):
        item = self._items[min(self._calls, len(self._items) - 1)]
        self._calls += 1
        return SimpleNamespace(scalar_one_or_none=lambda: item)


class _AssetDB:
    def __init__(self, report):
        self.report = report

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.report)


def _item(**over):
    fields = {
        "item_id": "itm_1",
        "status": "active",
        "error": None,
        "auto_refresh": True,
        "next_refresh_at": None,
        "encrypted_access_token": "cipher",
    }
    fields.update(over)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_revocation_stops_everything_and_drops_the_token():
    """Our privacy policy points users at my.plaid.com. Honouring what they do
    there is a published commitment, not an optimisation."""
    item = _item(next_refresh_at="soon")
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {"webhook_type": "ITEM", "webhook_code": "USER_PERMISSION_REVOKED", "item_id": "itm_1"},
    )
    assert out == "revoked"
    assert item.status == "revoked"
    assert item.auto_refresh is False
    assert item.next_refresh_at is None
    assert item.encrypted_access_token is None


@pytest.mark.asyncio
async def test_application_profile_items_receive_the_same_webhook_handling():
    item = _item(error="stale warning")
    out = await plaid_webhook.handle(
        _FakeDB(None, application_item=item),
        {
            "webhook_type": "STATEMENTS",
            "webhook_code": "STATEMENTS_REFRESH_COMPLETE",
            "item_id": "itm_1",
            "result": "SUCCESS",
        },
    )
    assert out == "sync queued"
    assert item.next_refresh_at is not None
    assert item.error is None


@pytest.mark.asyncio
async def test_account_revocation_preserves_the_remaining_item_connection():
    item = _item()
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "ITEM",
            "webhook_code": "USER_ACCOUNT_REVOKED",
            "item_id": "itm_1",
            "account_id": "acct_revoked",
        },
    )
    assert out == "account revocation flagged"
    assert item.status == "active"
    assert item.encrypted_access_token == "cipher"
    assert item.next_refresh_at is not None
    assert "one linked bank account" in item.error


@pytest.mark.asyncio
async def test_a_revoked_item_is_never_resurrected_by_a_later_webhook():
    """Plaid does not guarantee ordering, so a statements event queued before a
    revocation can arrive after it."""
    item = _item(status="revoked", auto_refresh=False)
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "STATEMENTS",
            "webhook_code": "STATEMENTS_REFRESH_COMPLETE",
            "item_id": "itm_1",
            "result": "SUCCESS",
        },
    )
    assert out == "ignored: item revoked"
    assert item.next_refresh_at is None
    assert item.status == "revoked"


@pytest.mark.asyncio
async def test_item_error_stops_the_scheduler_retrying_what_only_a_user_can_fix():
    item = _item(next_refresh_at="soon")
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "ITEM",
            "webhook_code": "ERROR",
            "item_id": "itm_1",
            "error": {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login required"},
        },
    )
    assert out == "error recorded"
    assert item.status == "error"
    assert item.next_refresh_at is None
    assert "login required" in item.error
    assert item.update_mode_reason == "item_login_required"


@pytest.mark.asyncio
async def test_new_accounts_available_requests_account_selection_update_mode():
    item = _item()
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "ITEM",
            "webhook_code": "NEW_ACCOUNTS_AVAILABLE",
            "item_id": "itm_1",
        },
    )
    assert out == "new accounts available"
    assert item.update_mode_reason == "new_accounts_available"
    assert item.update_mode_account_selection is True


@pytest.mark.asyncio
async def test_login_repaired_dismisses_update_mode_prompt():
    item = _item(
        status="error",
        error="Reconnect",
        update_mode_reason="item_login_required",
        update_mode_account_selection=True,
    )
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {"webhook_type": "ITEM", "webhook_code": "LOGIN_REPAIRED", "item_id": "itm_1"},
    )
    assert out == "login repaired"
    assert item.status == "active"
    assert item.update_mode_reason is None
    assert item.update_mode_account_selection is False


@pytest.mark.asyncio
async def test_asset_report_ready_webhook_marks_report_downloadable():
    report = SimpleNamespace(status="pending", error="waiting", ready_at=None)
    out = await plaid_webhook.handle(
        _AssetDB(report),
        {
            "webhook_type": "ASSETS",
            "webhook_code": "PRODUCT_READY",
            "asset_report_id": "asset-report-1",
            "environment": "production",
        },
    )
    assert out == "asset report ready"
    assert report.status == "ready"
    assert report.error is None
    assert report.ready_at is not None


@pytest.mark.asyncio
async def test_a_successful_statements_refresh_queues_a_sync():
    item = _item(error="stale warning")
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "STATEMENTS",
            "webhook_code": "STATEMENTS_REFRESH_COMPLETE",
            "item_id": "itm_1",
            "result": "SUCCESS",
        },
    )
    assert out == "sync queued"
    assert item.next_refresh_at is not None
    assert item.error is None


@pytest.mark.asyncio
async def test_pending_disconnect_warns_without_severing():
    """Seven days' notice is only useful if the connection still works today."""
    item = _item()
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {
            "webhook_type": "ITEM",
            "webhook_code": "PENDING_DISCONNECT",
            "item_id": "itm_1",
            "disconnect_time": "2026-09-01T00:00:00Z",
        },
    )
    assert out == "pending disconnect flagged"
    assert item.status == "active"
    assert item.auto_refresh is True
    assert "renewed" in item.error


@pytest.mark.asyncio
async def test_an_unknown_item_is_ignored_rather_than_erroring():
    """Plaid cannot know which items we still track; a 500 here earns retries."""
    out = await plaid_webhook.handle(
        _FakeDB(None),
        {"webhook_type": "ITEM", "webhook_code": "ERROR", "item_id": "itm_gone"},
    )
    assert out == "ignored: unknown item"


@pytest.mark.asyncio
async def test_an_unhandled_code_is_accepted_not_rejected():
    item = _item()
    out = await plaid_webhook.handle(
        _FakeDB(item),
        {"webhook_type": "ITEM", "webhook_code": "SOMETHING_NEW", "item_id": "itm_1"},
    )
    assert out.startswith("unhandled")
