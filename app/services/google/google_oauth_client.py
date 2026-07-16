"""Per-user Google OAuth 2.0 (authorization-code) client.

Handles the end-user consent flow and per-user credential resolution, backed by
the `google_accounts` table. Refresh tokens are encrypted at rest with the same
Fernet/KMS helpers ProviderSecret uses.

Design notes:
- The authorization-code exchange + refresh are hand-rolled against Google's
  token endpoint with httpx (no google-auth-oauthlib Flow needed). Live API
  calls use a `google.oauth2.credentials.Credentials` built from the fresh
  access token.
- The OAuth `state` is a stateless HMAC-signed token (bound to user_id + scopes
  + expiry) so the callback needs no session and no DB table for CSRF.
- Scopes are requested incrementally per feature (Gmail, then Calendar, then
  Drive) via include_granted_scopes=true.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.google_account import GoogleAccount
from app.services.provider_secrets import _decrypt_fernet, _decrypt_kms, _encrypt_fernet, _encrypt_kms

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# Scopes requested per service. openid+email always included for identity.
BASE_SCOPES = ["openid", "email"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

SERVICE_SCOPES = {
    "gmail": GMAIL_SCOPES,
    "calendar": CALENDAR_SCOPES,
    "drive": DRIVE_SCOPES,
}

_STATE_TTL_SECONDS = 600  # 10 min to complete consent


class GoogleNotConnected(RuntimeError):
    """User has no active Google grant."""


class GoogleScopeMissing(RuntimeError):
    """Grant exists but a required scope was not consented."""


class GoogleTokenRevoked(RuntimeError):
    """Refresh failed with invalid_grant — user must reconnect."""


# ─────────────────────────────────────────────────────────────────────────────
# Signed CSRF state (stateless)
# ─────────────────────────────────────────────────────────────────────────────
def _state_secret() -> bytes:
    s = get_settings()
    raw = s.clerk_secret_key or s.provider_secrets_encryption_key or "qc-google-oauth-state"
    return hashlib.sha256(raw.encode()).digest()


def sign_state(user_id: uuid.UUID, services: list[str]) -> str:
    payload = {"uid": str(user_id), "svc": services, "exp": int(time.time()) + _STATE_TTL_SECONDS, "n": uuid.uuid4().hex}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_state_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_state(state: str) -> dict:
    try:
        body, sig = state.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed state") from exc
    expected = hmac.new(_state_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad state signature")
    padded = body + "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("state expired")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Encryption (reuse ProviderSecret helpers)
# ─────────────────────────────────────────────────────────────────────────────
def _encrypt(value: str) -> tuple[str, str, str | None]:
    """Return (encrypted, provider, kms_key_id)."""
    s = get_settings()
    if s.provider_secrets_kms_key_id:
        return _encrypt_kms(value), "aws_kms", s.provider_secrets_kms_key_id
    return _encrypt_fernet(value), "fernet", None


def _decrypt(value: str, provider: str) -> str:
    if provider == "aws_kms":
        return _decrypt_kms(value)
    return _decrypt_fernet(value)


def _flags_from_scopes(scopes: list[str]) -> dict[str, bool]:
    granted = set(scopes or [])
    return {
        "gmail_connected": any(s in granted for s in GMAIL_SCOPES),
        "calendar_connected": any(s in granted for s in CALENDAR_SCOPES),
        "drive_connected": any(s in granted for s in DRIVE_SCOPES),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Auth URL + code exchange
# ─────────────────────────────────────────────────────────────────────────────
def _oauth_configured() -> bool:
    s = get_settings()
    return bool(s.gmail_oauth_client_id and s.gmail_oauth_client_secret and s.google_oauth_redirect_uri)


def build_auth_url(user_id: uuid.UUID, services: list[str]) -> str:
    s = get_settings()
    scopes = list(BASE_SCOPES)
    for svc in services:
        scopes += SERVICE_SCOPES.get(svc, [])
    params = {
        "client_id": s.gmail_oauth_client_id,
        "redirect_uri": s.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(dict.fromkeys(scopes)),
        "access_type": "offline",
        "prompt": "consent",  # force a refresh_token even on re-consent
        "include_granted_scopes": "true",  # incremental auth across features
        "state": sign_state(user_id, services),
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    scope: str
    id_token: str | None


async def exchange_code(code: str) -> TokenResponse:
    s = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": s.gmail_oauth_client_id,
                "client_secret": s.gmail_oauth_client_secret,
                "redirect_uri": s.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    resp.raise_for_status()
    data = resp.json()
    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_in=int(data.get("expires_in", 3600)),
        scope=data.get("scope", ""),
        id_token=data.get("id_token"),
    )


def _decode_id_token_claims(id_token: str | None) -> dict:
    """Best-effort decode of the id_token payload (email, sub). We trust it
    because it came straight from Google's token endpoint over TLS in exchange
    for our client secret — no separate signature verification needed here."""
    if not id_token:
        return {}
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:  # noqa: BLE001
        return {}


async def save_grant(db: AsyncSession, user_id: uuid.UUID, tokens: TokenResponse) -> GoogleAccount:
    """Upsert the user's GoogleAccount from a fresh token response. Merges scopes
    (incremental auth) and preserves an existing refresh token if Google didn't
    return a new one."""
    row = (await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))).scalar_one_or_none()
    claims = _decode_id_token_claims(tokens.id_token)
    new_scopes = [x for x in tokens.scope.split(" ") if x]

    if row is None:
        row = GoogleAccount(id=uuid.uuid4(), user_id=user_id)
        db.add(row)

    # Merge granted scopes across incremental consents.
    merged = list(dict.fromkeys([*(row.scopes or []), *new_scopes]))
    row.scopes = merged

    if tokens.refresh_token:
        enc, provider, kms = _encrypt(tokens.refresh_token)
        row.encrypted_refresh_token = enc
        row.encryption_provider = provider
        row.kms_key_id = kms
    # else: keep the existing refresh token (Google omits it on re-consent when
    # prompt!=consent; we force prompt=consent, but be defensive).

    # Cache the access token too.
    enc_at, at_provider, at_kms = _encrypt(tokens.access_token)
    row.access_token_cache = enc_at
    row.access_token_expiry = datetime.now(UTC) + timedelta(seconds=max(tokens.expires_in - 60, 60))

    if claims.get("email"):
        row.google_email = claims["email"]
    if claims.get("sub"):
        row.google_sub = claims["sub"]

    flags = _flags_from_scopes(merged)
    row.gmail_connected = flags["gmail_connected"]
    row.calendar_connected = flags["calendar_connected"]
    row.drive_connected = flags["drive_connected"]
    row.status = "active"
    row.last_error = None
    row.connected_at = row.connected_at or datetime.now(UTC)
    await db.flush()
    return row


async def _refresh_access_token(db: AsyncSession, row: GoogleAccount) -> str:
    """Exchange the stored refresh token for a fresh access token. On
    invalid_grant, mark the row revoked and raise GoogleTokenRevoked."""
    if not row.encrypted_refresh_token:
        raise GoogleNotConnected("no refresh token on file")
    s = get_settings()
    refresh_token = _decrypt(row.encrypted_refresh_token, row.encryption_provider)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": s.gmail_oauth_client_id,
                "client_secret": s.gmail_oauth_client_secret,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code >= 400:
        detail = resp.text
        if "invalid_grant" in detail:
            row.status = "revoked"
            row.gmail_connected = row.calendar_connected = row.drive_connected = False
            row.last_error = "invalid_grant (reconnect required)"
            await db.flush()
            raise GoogleTokenRevoked("refresh token invalid; reconnect required")
        row.last_error = f"refresh failed: {detail[:200]}"
        await db.flush()
        raise RuntimeError(f"google token refresh failed: {resp.status_code}")
    data = resp.json()
    access_token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    enc_at, _, _ = _encrypt(access_token)
    row.access_token_cache = enc_at
    row.access_token_expiry = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 60))
    row.status = "active"
    row.last_error = None
    await db.flush()
    return access_token


async def credentials_for_user(db: AsyncSession, user_id: uuid.UUID, required_scopes: list[str]):
    """Return a live google.oauth2.credentials.Credentials for the user, refreshing
    if needed. Raises GoogleNotConnected / GoogleScopeMissing / GoogleTokenRevoked."""
    row = (await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))).scalar_one_or_none()
    if row is None or row.status != "active" or not row.encrypted_refresh_token:
        raise GoogleNotConnected(f"user {user_id} has no active Google connection")
    granted = set(row.scopes or [])
    missing = [s for s in required_scopes if s not in granted]
    if missing:
        raise GoogleScopeMissing(f"missing scopes: {missing}")

    # Use cached access token if still valid, else refresh.
    now = datetime.now(UTC)
    access_token: str | None = None
    if row.access_token_cache and row.access_token_expiry and row.access_token_expiry > now:
        try:
            access_token = _decrypt(row.access_token_cache, row.encryption_provider)
        except Exception:  # noqa: BLE001
            access_token = None
    if not access_token:
        access_token = await _refresh_access_token(db, row)

    from google.oauth2.credentials import Credentials

    return Credentials(token=access_token, scopes=list(granted))


async def get_account(db: AsyncSession, user_id: uuid.UUID) -> GoogleAccount | None:
    return (await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))).scalar_one_or_none()


async def disconnect(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Best-effort revoke at Google + delete the local grant."""
    row = await get_account(db, user_id)
    if row is None:
        return False
    if row.encrypted_refresh_token:
        try:
            token = _decrypt(row.encrypted_refresh_token, row.encryption_provider)
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(REVOKE_ENDPOINT, params={"token": token})
        except Exception:  # noqa: BLE001
            log.warning("google revoke failed for user=%s (deleting local grant anyway)", user_id)
    await db.delete(row)
    await db.flush()
    return True
