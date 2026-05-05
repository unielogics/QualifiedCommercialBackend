"""Auth dependencies — Clerk JWT verification + role gates.

When CLERK_SECRET_KEY is unset (local dev without Clerk wired yet), every
request is treated as a dev super_admin so screens can be built end-to-end.
The runtime warns once at startup if dev mode is active.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.enums import Role
from app.models.user import User

log = logging.getLogger(__name__)
_jwks_cache: dict[str, object] | None = None


async def _get_jwks() -> dict[str, object]:
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    settings = get_settings()
    if not settings.clerk_jwks_url:
        return {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(settings.clerk_jwks_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        return _jwks_cache


async def _verify_clerk_jwt(token: str) -> dict:
    """Verify a Clerk-issued JWT against the project's JWKS endpoint."""
    settings = get_settings()
    jwks = await _get_jwks()
    if not jwks:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Clerk JWKS not configured")
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    keys = jwks.get("keys", [])
    key_data = next((k for k in keys if k.get("kid") == kid), None)
    if key_data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown signing key")
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
    try:
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer or None,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"JWT invalid: {exc}") from exc


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Returns the User row matching the Clerk JWT subject.

    In dev mode (no Clerk config), the optional `X-Dev-User` header selects a
    seeded user by email; default to the first super_admin so the desktop
    Dashboard works without Clerk being wired.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        # Dev mode: short-circuit auth
        stmt = (
            select(User).where(User.email == x_dev_user)
            if x_dev_user
            else select(User).where(User.role == Role.SUPER_ADMIN).limit(1)
        )
        user = (await db.execute(stmt)).scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "No dev user found. Run `python -m app.seed` first.",
            )
        return user

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    payload = await _verify_clerk_jwt(token)
    clerk_id = payload.get("sub")
    if not clerk_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token missing subject")
    user = (await db.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one_or_none()
    if user is None:
        # Auto-provision on first sign-in. Default to CLIENT role — super_admin
        # promotes from there. Email comes from the JWT (Clerk includes it
        # under `email` when the email-address claim mapping is enabled).
        email = payload.get("email") or payload.get("primary_email_address") or f"{clerk_id}@unknown"
        name = payload.get("name") or payload.get("first_name") or email.split("@")[0]
        user = User(clerk_id=clerk_id, email=email, name=name, role=Role.CLIENT)
        db.add(user)
        await db.flush()
        await db.refresh(user)
        log.info("Auto-provisioned user clerk_id=%s email=%s", clerk_id, email)
    return user


def require_role(*roles: Role):
    """Dependency factory for role-gated endpoints."""

    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return checker


CurrentUser = Annotated[User, Depends(get_current_user)]
