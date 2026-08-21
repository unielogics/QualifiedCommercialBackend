"""Thin Plaid API client — Statements product ONLY.

Deliberately not the plaid-python SDK: four JSON POSTs don't justify a
dependency, and the raw API keeps the failure surface visible. Config comes
straight from the environment (DEALER_OS_PLAID_* — zero touches to the shared
Settings class, per the isolation contract):

    DEALER_OS_PLAID_CLIENT_ID
    DEALER_OS_PLAID_SECRET
    DEALER_OS_PLAID_ENV           sandbox | production   (default sandbox)
    DEALER_OS_PLAID_REDIRECT_URI  OAuth return URL       (optional, see below)
    DEALER_OS_PLAID_WEBHOOK_URL   Inbound webhook URL    (optional, see below)

When the keys are absent every entrypoint raises PlaidUnavailable — the API
layer turns that into {enabled: false}, never a 500.

Token encryption mirrors app/services/provider_secrets' Fernet recipe
(PROVIDER_SECRETS_ENCRYPTION_KEY, falling back to CLERK_SECRET_KEY, hashed
into a valid Fernet key) so a later consolidation is trivial.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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


def redirect_uri() -> str:
    """The OAuth return URL, or "" when OAuth is not configured yet.

    Plaid requires OAuth for every integration that connects to a US, EU or UK
    institution — which is most of the largest US banks. Without it those banks
    simply cannot be linked, so this is not optional for production.

    It is env-gated on purpose. Plaid REJECTS /link/token/create outright if the
    redirect_uri is not already registered under "Allowed redirect URIs" in the
    Plaid Dashboard, so sending one before that registration exists would break
    bank linking entirely rather than improve it. Leaving this unset keeps the
    current non-OAuth behaviour working; setting it turns OAuth on. Register the
    URI in the Dashboard FIRST, then set this.

    Must be https (localhost is allowed in Sandbox) and must not use hash
    routing.
    """
    return _env("DEALER_OS_PLAID_REDIRECT_URI")


def webhook_url() -> str:
    """Where Plaid should POST item events, or "" when not configured.

    Without this we only learn an Item is broken by trying to use it, which on a
    30-day refresh cadence means up to a month of silence. Worse, the two events
    that matter most are ones we can never discover by polling: the user
    revoking access at my.plaid.com, and Plaid scheduling a disconnect. Our
    privacy policy points users at those controls, so honouring them is a
    commitment, not a nicety.

    Unlike redirect_uri this needs no prior dashboard registration — it is sent
    per link token — but it is still env-gated so it can be turned on
    deliberately, and so a half-deployed receiver never gets traffic.
    """
    return _env("DEALER_OS_PLAID_WEBHOOK_URL")


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
            "client_name": "Qualified Commercial — Capital OS",
            "user": {"client_user_id": dealer_id},
            "products": ["statements"],
            "statements": {
                "start_date": (today - timedelta(days=STATEMENT_LOOKBACK_DAYS)).isoformat(),
                "end_date": today.isoformat(),
            },
            "country_codes": ["US"],
            "language": "en",
            # Only present once the URI is registered with Plaid — see
            # redirect_uri() for why sending it early is worse than omitting it.
            **({"redirect_uri": redirect_uri()} if redirect_uri() else {}),
            **({"webhook": webhook_url()} if webhook_url() else {}),
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
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for st in out:
        if st["statement_id"] not in seen:
            seen.add(st["statement_id"])
            deduped.append(st)
    return {"institution_name": data.get("institution_name"), "statements": deduped}


async def statements_download(access_token: str, statement_id: str) -> bytes:
    resp = await _post(
        "/statements/download",
        {"access_token": access_token, "statement_id": statement_id},
        timeout=90.0,
    )
    return resp.content


async def accounts_get(access_token: str) -> list[dict[str, Any]]:
    """-> [{name, mask}] for the row label ("Plaid Checking ··1111")."""
    resp = await _post("/accounts/get", {"access_token": access_token})
    out = []
    for a in resp.json().get("accounts") or []:
        out.append({"name": a.get("name") or a.get("official_name"), "mask": a.get("mask")})
    return out


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


# ── Webhook verification ────────────────────────────────────────────────────
#
# A webhook endpoint is unauthenticated by definition: Plaid has to be able to
# reach it. The only identifier in the body is item_id, so without verification
# anyone who learned an item_id could tell us a connection was revoked, or
# trigger syncs at will. Plaid signs every webhook with an ES256 JWT in the
# Plaid-Verification header, and that signature is what makes the endpoint safe.
#
# Three things are checked, and all three matter:
#   1. the JWT signature, against a key fetched from Plaid by its key id;
#   2. the issued-at time, so a captured webhook cannot be replayed later;
#   3. the SHA-256 of the RAW request body against the JWT's claim, which is
#      what stops a valid signature being reused with a swapped body.

WEBHOOK_MAX_AGE_SECONDS = 300  # Plaid's own guidance

_key_cache: dict[str, Any] = {}


async def _verification_key(key_id: str) -> Any:
    """The public KEY for a key id, cached.

    Returns a key object rather than the raw JWK: the caller is verifying a
    signature and should not have to know how Plaid packages its keys. It is
    also the seam tests substitute, which keeps them from monkeypatching the
    jwt module globally — `jwt.PyJWK` resolves lazily, so patching it works or
    fails depending on import order elsewhere in the suite.
    """
    from jwt import PyJWK

    if key_id in _key_cache:
        return _key_cache[key_id]
    resp = await _post("/webhook_verification_key/get", {"key_id": key_id})
    jwk = resp.json().get("key")
    if not isinstance(jwk, dict):
        raise PlaidUnavailable("Plaid returned no verification key")
    key = PyJWK.from_dict(jwk).key
    _key_cache[key_id] = key
    return key


async def verify_webhook(raw_body: bytes, verification_header: str) -> bool:
    """True only if this really came from Plaid, unmodified and recent."""
    import time

    import jwt

    if not verification_header:
        return False
    try:
        header = jwt.get_unverified_header(verification_header)
    except Exception:  # noqa: BLE001 - malformed token is simply not verified
        logger.warning("plaid webhook: unparseable verification header")
        return False

    # Algorithm is pinned. Accepting whatever the token declares is the classic
    # JWT downgrade hole ("alg": "none", or HS256 signed with the public key).
    if header.get("alg") != "ES256":
        logger.warning("plaid webhook: unexpected alg %r", header.get("alg"))
        return False
    key_id = header.get("kid")
    if not key_id:
        return False

    try:
        key = await _verification_key(str(key_id))
        claims = jwt.decode(
            verification_header,
            key=key,
            algorithms=["ES256"],
            options={"verify_aud": False},
        )
    except PlaidUnavailable:
        raise
    except Exception:  # noqa: BLE001 - any signature failure is a rejection
        logger.warning("plaid webhook: signature did not verify")
        return False

    issued = claims.get("iat")
    if not isinstance(issued, (int, float)) or time.time() - issued > WEBHOOK_MAX_AGE_SECONDS:
        logger.warning("plaid webhook: stale or missing iat — possible replay")
        return False

    expected = claims.get("request_body_sha256")
    actual = hashlib.sha256(raw_body).hexdigest()
    # Constant-time: this is a secret comparison, not a string comparison.
    if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
        logger.warning("plaid webhook: body hash mismatch — body was altered")
        return False

    return True
