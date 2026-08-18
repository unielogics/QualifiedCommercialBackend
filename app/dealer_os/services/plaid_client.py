"""Thin Plaid API client — Statements product ONLY.

Deliberately not the plaid-python SDK: four JSON POSTs don't justify a
dependency, and the raw API keeps the failure surface visible. Config comes
straight from the environment (DEALER_OS_PLAID_* — zero touches to the shared
Settings class, per the isolation contract):

    DEALER_OS_PLAID_CLIENT_ID
    DEALER_OS_PLAID_SECRET
    DEALER_OS_PLAID_ENV        sandbox | production   (default sandbox)

When the keys are absent every entrypoint raises PlaidUnavailable — the API
layer turns that into {enabled: false}, never a 500.

Token encryption mirrors app/services/provider_secrets' Fernet recipe
(PROVIDER_SECRETS_ENCRYPTION_KEY, falling back to CLERK_SECRET_KEY, hashed
into a valid Fernet key) so a later consolidation is trivial.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from datetime import date, timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# How far back the FIRST pull reaches ("pull all the statements").
STATEMENT_LOOKBACK_DAYS = 730

# The auto-refresh cadence the user asked for.
REFRESH_EVERY_DAYS = 30


class PlaidUnavailable(Exception):
    """Keys absent or the Plaid API rejected/failed the call."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def enabled() -> bool:
    return bool(_env("DEALER_OS_PLAID_CLIENT_ID") and _env("DEALER_OS_PLAID_SECRET"))


def environment() -> str:
    env = _env("DEALER_OS_PLAID_ENV").lower() or "sandbox"
    return env if env in _HOSTS else "sandbox"


def _base() -> str:
    return _HOSTS[environment()]


async def _post(path: str, payload: dict[str, Any], *, timeout: float = 30.0) -> httpx.Response:
    if not enabled():
        raise PlaidUnavailable("Plaid keys are not configured (DEALER_OS_PLAID_CLIENT_ID/SECRET)")
    body = {
        "client_id": _env("DEALER_OS_PLAID_CLIENT_ID"),
        "secret": _env("DEALER_OS_PLAID_SECRET"),
        **payload,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{_base()}{path}", json=body)
    except httpx.HTTPError as exc:
        raise PlaidUnavailable(f"Plaid request failed: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        # Plaid errors carry error_code/error_message — log the code, surface
        # a clean message (never the raw payload, it can echo identifiers).
        code = msg = ""
        try:
            err = resp.json()
            code, msg = err.get("error_code", ""), err.get("error_message", "")
        except Exception:
            pass
        logger.warning("plaid %s -> %s %s", path, resp.status_code, code)
        raise PlaidUnavailable(msg or f"Plaid rejected the request ({resp.status_code})")
    return resp


async def create_link_token(*, dealer_id: str, dealer_name: str) -> str:
    today = date.today()
    resp = await _post(
        "/link/token/create",
        {
            "client_name": "Qualified Commercial — Dealer Capital OS",
            "user": {"client_user_id": dealer_id},
            "products": ["statements"],
            "statements": {
                "start_date": (today - timedelta(days=STATEMENT_LOOKBACK_DAYS)).isoformat(),
                "end_date": today.isoformat(),
            },
            "country_codes": ["US"],
            "language": "en",
        },
    )
    token = resp.json().get("link_token")
    if not token:
        raise PlaidUnavailable("Plaid returned no link token")
    return token


async def exchange_public_token(public_token: str) -> tuple[str, str]:
    """-> (access_token, item_id)"""
    resp = await _post("/item/public_token/exchange", {"public_token": public_token})
    data = resp.json()
    access, item = data.get("access_token"), data.get("item_id")
    if not access or not item:
        raise PlaidUnavailable("Plaid token exchange returned an incomplete response")
    return access, item


async def statements_list(access_token: str) -> dict[str, Any]:
    """-> {institution_name, statements: [{statement_id, month, year, account_name?}]}

    Defensive against both response shapes Plaid has used (statements nested
    under accounts[], and top-level)."""
    resp = await _post("/statements/list", {"access_token": access_token})
    data = resp.json()
    out: list[dict[str, Any]] = []
    for acct in data.get("accounts") or []:
        for st in acct.get("statements") or []:
            if st.get("statement_id"):
                out.append(
                    {
                        "statement_id": st["statement_id"],
                        "month": st.get("month"),
                        "year": st.get("year"),
                        "account_name": acct.get("account_name") or acct.get("name"),
                    }
                )
    for st in data.get("statements") or []:
        if st.get("statement_id"):
            out.append(
                {
                    "statement_id": st["statement_id"],
                    "month": st.get("month"),
                    "year": st.get("year"),
                    "account_name": None,
                }
            )
    return {"institution_name": data.get("institution_name"), "statements": out}


async def statements_download(access_token: str, statement_id: str) -> bytes:
    resp = await _post(
        "/statements/download",
        {"access_token": access_token, "statement_id": statement_id},
        timeout=90.0,
    )
    return resp.content


async def item_remove(access_token: str) -> None:
    await _post("/item/remove", {"access_token": access_token})


# --- token encryption at rest -------------------------------------------------


def _fernet() -> Fernet:
    raw = (
        _env("PROVIDER_SECRETS_ENCRYPTION_KEY")
        or _env("CLERK_SECRET_KEY")
        or "dealer-os-dev-fallback"
    )
    try:
        if len(raw) == 44:
            return Fernet(raw.encode())
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):  # key rotation / corruption — treat as absent
        return None
