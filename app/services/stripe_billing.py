from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


STRIPE_API_BASE = "https://api.stripe.com/v1"


class StripeBillingError(RuntimeError):
    pass


class StripeConfigError(StripeBillingError):
    pass


def _secret_key() -> str:
    key = get_settings().stripe_secret_key
    if not key:
        raise StripeConfigError("Stripe is not configured")
    return key


async def _stripe_request(
    method: str,
    path: str,
    *,
    data: dict[str, Any] | list[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    key = _secret_key()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            method,
            f"{STRIPE_API_BASE}{path}",
            auth=(key, ""),
            data=data,
        )
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            msg = payload.get("error", {}).get("message") or resp.text
        except Exception:  # noqa: BLE001
            msg = resp.text
        raise StripeBillingError(f"Stripe request failed ({resp.status_code}): {msg}")
    return resp.json()


async def create_customer(*, email: str | None, name: str | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if email:
        data["email"] = email
    if name:
        data["name"] = name
    return await _stripe_request("POST", "/customers", data=data)


async def create_setup_intent(*, customer_id: str, metadata: dict[str, str]) -> dict[str, Any]:
    data: list[tuple[str, Any]] = [
        ("customer", customer_id),
        ("usage", "off_session"),
        ("payment_method_types[]", "card"),
    ]
    for key, value in metadata.items():
        data.append((f"metadata[{key}]", value))
    return await _stripe_request("POST", "/setup_intents", data=data)


async def retrieve_setup_intent(setup_intent_id: str) -> dict[str, Any]:
    return await _stripe_request("GET", f"/setup_intents/{setup_intent_id}")


async def retrieve_payment_method(payment_method_id: str) -> dict[str, Any]:
    return await _stripe_request("GET", f"/payment_methods/{payment_method_id}")


async def create_payment_intent(
    *,
    amount_cents: int,
    currency: str,
    customer_id: str,
    payment_method_id: str,
    description: str,
    metadata: dict[str, str],
) -> dict[str, Any]:
    data: list[tuple[str, Any]] = [
        ("amount", amount_cents),
        ("currency", currency.lower()),
        ("customer", customer_id),
        ("payment_method", payment_method_id),
        ("confirm", "true"),
        ("off_session", "true"),
        ("description", description[:1000]),
    ]
    for key, value in metadata.items():
        data.append((f"metadata[{key}]", value))
    return await _stripe_request("POST", "/payment_intents", data=data)
