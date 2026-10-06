"""Plaid Transfer provider boundary for QC Payments.

This module is intentionally separate from the Dealer OS Plaid evidence
client.  Evidence Items grant read access to statements/assets; payment Items
grant authority to move money and must never be interchangeable in storage or
consent.  The credentials may point at the same Plaid application, but every
token, Link session, and API operation in this file is Transfer-only.

The service exposes raw provider operations only.  Database locking,
idempotent claims, authorization policy, and audit records live in
``app.services.payments``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from decimal import Decimal
from typing import Any, Literal

import httpx

from app.services.provider_secrets import _decrypt_fernet, _encrypt_fernet

log = logging.getLogger(__name__)

_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

AchClass = Literal["ccd", "web"]
WEBHOOK_MAX_AGE_SECONDS = 300
_verification_key_cache: dict[str, Any] = {}


class PlaidTransferError(RuntimeError):
    """Safe provider error suitable for an operator-facing action state."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.retryable = retryable


def _env(name: str, fallback: str | None = None) -> str:
    value = (os.environ.get(name) or "").strip()
    if value or fallback is None:
        return value
    return (os.environ.get(fallback) or "").strip()


def environment() -> str:
    value = _env("PAYMENTS_PLAID_ENV").lower() or "sandbox"
    if value not in _HOSTS:
        raise PlaidTransferError(
            "PAYMENTS_PLAID_ENV must be sandbox or production",
            code="PAYMENTS_PLAID_ENV_INVALID",
        )
    return value


def enabled() -> bool:
    return bool(
        _env("PAYMENTS_PLAID_CLIENT_ID")
        and _env("PAYMENTS_PLAID_SECRET")
    )


def redirect_uri() -> str:
    return _env("PAYMENTS_PLAID_REDIRECT_URI", "DEALER_OS_PLAID_ROOM_REDIRECT_URI")


def webhook_url() -> str:
    return _env("PAYMENTS_PLAID_WEBHOOK_URL", "DEALER_OS_PLAID_WEBHOOK_URL")


def client_name() -> str:
    value = _env("PAYMENTS_PLAID_CLIENT_NAME", "DEALER_OS_PLAID_CLIENT_NAME")
    return (value or "Qualified Commercial")[:30]


def encrypt_access_token(value: str) -> str:
    return _encrypt_fernet(value)


def decrypt_access_token(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _decrypt_fernet(value)
    except Exception:  # noqa: BLE001 - corrupted/rotated ciphertext is unusable
        log.exception("Unable to decrypt a Plaid Transfer access token")
        return None


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    if not enabled():
        raise PlaidTransferError(
            "Plaid Transfer is not configured",
            code="PAYMENTS_PLAID_NOT_CONFIGURED",
        )
    body = {
        "client_id": _env("PAYMENTS_PLAID_CLIENT_ID"),
        "secret": _env("PAYMENTS_PLAID_SECRET"),
        **payload,
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=8.0)
        ) as client:
            response = await client.post(f"{_HOSTS[environment()]}{path}", json=body)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise PlaidTransferError(
            "Plaid Transfer did not return a definite result. The payment state will be reconciled before another attempt.",
            code="PLAID_NETWORK_UNCERTAIN",
            retryable=True,
        ) from exc

    data: dict[str, Any]
    try:
        parsed = response.json()
        data = parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        data = {}

    if response.status_code >= 400:
        code = str(data.get("error_code") or "PLAID_TRANSFER_ERROR")
        request_id = str(data.get("request_id") or "") or None
        log.warning(
            "Plaid Transfer %s returned %s code=%s request_id=%s",
            path,
            response.status_code,
            code,
            request_id,
        )
        message = str(data.get("display_message") or data.get("error_message") or "")
        raise PlaidTransferError(
            message or "Plaid could not complete this payment operation",
            code=code,
            request_id=request_id,
            retryable=response.status_code == 429 or response.status_code >= 500,
        )
    return data


async def _verification_key(key_id: str) -> Any:
    from jwt import PyJWK

    if key_id in _verification_key_cache:
        return _verification_key_cache[key_id]
    data = await _post("/webhook_verification_key/get", {"key_id": key_id})
    jwk = data.get("key")
    if not isinstance(jwk, dict):
        raise PlaidTransferError(
            "Plaid returned no webhook verification key",
            code="PLAID_WEBHOOK_KEY_MISSING",
        )
    key = PyJWK.from_dict(jwk).key
    _verification_key_cache[key_id] = key
    return key


async def verify_webhook(raw_body: bytes, verification_header: str) -> bool:
    """Verify Plaid's ES256 signature, freshness, and raw-body hash."""

    import jwt

    if not verification_header:
        return False
    try:
        header = jwt.get_unverified_header(verification_header)
    except Exception:  # noqa: BLE001
        return False
    if header.get("alg") != "ES256" or not header.get("kid"):
        return False
    try:
        key = await _verification_key(str(header["kid"]))
        claims = jwt.decode(
            verification_header,
            key=key,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
    except PlaidTransferError:
        raise
    except Exception:  # noqa: BLE001
        return False
    issued = claims.get("iat")
    if not isinstance(issued, (int, float)):
        return False
    age = time.time() - float(issued)
    if age < -30 or age > WEBHOOK_MAX_AGE_SECONDS:
        return False
    expected = claims.get("request_body_sha256")
    actual = hashlib.sha256(raw_body).hexdigest()
    return isinstance(expected, str) and hmac.compare_digest(expected, actual)


async def create_link_token(
    *,
    client_user_id: str,
    redirect_override: str | None = None,
    access_token: str | None = None,
    authorization_id: str | None = None,
) -> str:
    """Create a Transfer-only Link session.

    ``authorization_id`` invokes Plaid's repair flow after an
    ``user_action_required`` authorization decision.  Plaid requires update
    mode to omit products in that case.
    """

    if authorization_id and access_token:
        raise ValueError("authorization_id and access_token are mutually exclusive")
    payload: dict[str, Any] = {
        "client_name": client_name(),
        "user": {"client_user_id": client_user_id},
        "country_codes": ["US"],
        "language": "en",
    }
    if authorization_id:
        payload["transfer"] = {"authorization_id": authorization_id}
    elif access_token:
        # Plaid update mode uses the existing access token and deliberately
        # omits products; re-sending ``transfer`` attempts to initialize the
        # product again instead of repairing/reselecting the existing Item.
        payload["access_token"] = access_token
    else:
        payload["products"] = ["transfer"]
    target_redirect = redirect_override or redirect_uri()
    if target_redirect:
        payload["redirect_uri"] = target_redirect
    if webhook_url():
        payload["webhook"] = webhook_url()
    data = await _post("/link/token/create", payload)
    token = str(data.get("link_token") or "")
    if not token:
        raise PlaidTransferError(
            "Plaid returned no payment Link token",
            code="PLAID_LINK_TOKEN_MISSING",
        )
    return token


async def exchange_public_token(public_token: str) -> tuple[str, str]:
    data = await _post(
        "/item/public_token/exchange", {"public_token": public_token}
    )
    access_token = str(data.get("access_token") or "")
    item_id = str(data.get("item_id") or "")
    if not access_token or not item_id:
        raise PlaidTransferError(
            "Plaid returned an incomplete payment connection",
            code="PLAID_TOKEN_EXCHANGE_INCOMPLETE",
        )
    return access_token, item_id


async def accounts(access_token: str) -> list[dict[str, Any]]:
    data = await _post("/accounts/get", {"access_token": access_token})
    result: list[dict[str, Any]] = []
    for row in data.get("accounts") or []:
        if not isinstance(row, dict) or not row.get("account_id"):
            continue
        result.append(
            {
                "account_id": str(row["account_id"]),
                "name": str(row.get("name") or "Bank account"),
                "official_name": row.get("official_name"),
                "mask": str(row.get("mask") or ""),
                "type": str(row.get("type") or ""),
                "subtype": str(row.get("subtype") or ""),
            }
        )
    return result


def _amount(value: Decimal | str | int | float) -> str:
    parsed = Decimal(str(value)).quantize(Decimal("0.01"))
    if parsed <= 0:
        raise ValueError("Transfer amount must be positive")
    return format(parsed, ".2f")


async def create_authorization(
    *,
    access_token: str,
    account_id: str,
    amount: Decimal | str | int | float,
    ach_class: AchClass,
    legal_name: str,
    idempotency_key: str,
    email: str | None = None,
    phone: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    user_present: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    user: dict[str, Any] = {"legal_name": legal_name.strip()}
    if email:
        user["email_address"] = email
    if phone:
        user["phone_number"] = phone
    device = {
        key: value
        for key, value in {
            "ip_address": ip_address,
            "user_agent": user_agent,
        }.items()
        if value
    }
    payload: dict[str, Any] = {
        "access_token": access_token,
        "account_id": account_id,
        "type": "debit",
        "network": "ach",
        "amount": _amount(amount),
        "ach_class": ach_class,
        "user": user,
        "idempotency_key": idempotency_key[:50],
        "user_present": user_present,
    }
    if device:
        payload["device"] = device
    if ledger_id:
        payload["ledger_id"] = ledger_id
    data = await _post("/transfer/authorization/create", payload)
    authorization = data.get("authorization")
    if not isinstance(authorization, dict) or not authorization.get("id"):
        raise PlaidTransferError(
            "Plaid returned no transfer authorization",
            code="PLAID_AUTHORIZATION_MISSING",
        )
    return authorization


async def create_transfer(
    *,
    access_token: str,
    account_id: str,
    authorization_id: str,
    amount: Decimal | str | int | float,
    description: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    safe_description = "".join(ch for ch in description if ch.isascii())[:10]
    payload: dict[str, Any] = {
        "access_token": access_token,
        "account_id": account_id,
        "authorization_id": authorization_id,
        "amount": _amount(amount),
        "description": safe_description or "Fee",
    }
    if metadata:
        payload["metadata"] = {
            str(key)[:40]: str(value)[:500]
            for key, value in metadata.items()
            if str(key).isascii() and str(value).isascii()
        }
    data = await _post("/transfer/create", payload)
    transfer = data.get("transfer")
    if not isinstance(transfer, dict) or not transfer.get("id"):
        raise PlaidTransferError(
            "Plaid returned no transfer record",
            code="PLAID_TRANSFER_MISSING",
        )
    return transfer


async def get_transfer(transfer_id: str) -> dict[str, Any]:
    data = await _post("/transfer/get", {"transfer_id": transfer_id})
    transfer = data.get("transfer")
    if not isinstance(transfer, dict):
        raise PlaidTransferError(
            "Plaid returned no transfer record",
            code="PLAID_TRANSFER_MISSING",
        )
    return transfer


async def get_ledger_available_balance(
    *,
    ledger_id: str,
    originator_client_id: str | None = None,
) -> Decimal:
    """Return the spendable balance for the exact Plaid Ledger.

    Refunds are funded from the Ledger associated with the original transfer,
    not necessarily the account's default Ledger.  Keep this lookup strict so
    a missing, malformed, or mismatched provider response fails closed before
    any money-moving endpoint is called.
    """

    expected_ledger_id = str(ledger_id or "").strip()
    if not expected_ledger_id:
        raise PlaidTransferError(
            "The original transfer has no Plaid Ledger reference",
            code="PLAID_REFUND_LEDGER_MISSING",
        )
    payload: dict[str, Any] = {"ledger_id": expected_ledger_id}
    normalized_originator = str(originator_client_id or "").strip()
    if normalized_originator:
        payload["originator_client_id"] = normalized_originator
    data = await _post("/transfer/ledger/get", payload)
    returned_ledger_id = str(data.get("ledger_id") or "").strip()
    if returned_ledger_id != expected_ledger_id:
        raise PlaidTransferError(
            "Plaid returned a different Ledger than the original transfer",
            code="PLAID_REFUND_LEDGER_MISMATCH",
        )
    balance = data.get("balance")
    if not isinstance(balance, dict):
        raise PlaidTransferError(
            "Plaid returned no Ledger balance",
            code="PLAID_REFUND_LEDGER_BALANCE_MISSING",
        )
    try:
        available = Decimal(str(balance.get("available")))
    except Exception as exc:  # noqa: BLE001 - provider payload is untrusted
        raise PlaidTransferError(
            "Plaid returned an invalid available Ledger balance",
            code="PLAID_REFUND_LEDGER_BALANCE_INVALID",
        ) from exc
    if not available.is_finite():
        raise PlaidTransferError(
            "Plaid returned an invalid available Ledger balance",
            code="PLAID_REFUND_LEDGER_BALANCE_INVALID",
        )
    return available


async def get_transfer_by_authorization(authorization_id: str) -> dict[str, Any] | None:
    """Resolve an ambiguous create using Plaid's deterministic authorization.

    Plaid accepts either ``transfer_id`` or ``authorization_id`` on
    ``/transfer/get``. A transfer authorization can create at most one
    transfer, so this lookup is safe after a lost ``/transfer/create``
    response and does not issue another debit.
    """

    try:
        data = await _post(
            "/transfer/get", {"authorization_id": authorization_id}
        )
    except PlaidTransferError as exc:
        if (exc.code or "").upper() in {
            "TRANSFER_NOT_FOUND",
            "NO_TRANSFER_FOUND",
            "NOT_FOUND",
        }:
            return None
        raise
    transfer = data.get("transfer")
    if transfer is None:
        return None
    if not isinstance(transfer, dict) or not transfer.get("id"):
        raise PlaidTransferError(
            "Plaid returned an incomplete transfer record",
            code="PLAID_TRANSFER_INCOMPLETE",
        )
    return transfer


async def cancel_transfer(transfer_id: str) -> None:
    await _post("/transfer/cancel", {"transfer_id": transfer_id})


async def create_refund(
    *, transfer_id: str, amount: Decimal | str | int | float, idempotency_key: str
) -> dict[str, Any]:
    data = await _post(
        "/transfer/refund/create",
        {
            "transfer_id": transfer_id,
            "amount": _amount(amount),
            "idempotency_key": idempotency_key[:50],
        },
    )
    refund = data.get("refund")
    if not isinstance(refund, dict) or not refund.get("id"):
        raise PlaidTransferError(
            "Plaid returned no refund record",
            code="PLAID_REFUND_MISSING",
        )
    return refund


async def sync_events(
    *, after_id: int, count: int = 500
) -> tuple[list[dict[str, Any]], bool]:
    data = await _post(
        "/transfer/event/sync",
        {"after_id": max(0, after_id), "count": min(500, max(1, count))},
    )
    rows = [row for row in (data.get("transfer_events") or []) if isinstance(row, dict)]
    return rows, bool(data.get("has_more"))
