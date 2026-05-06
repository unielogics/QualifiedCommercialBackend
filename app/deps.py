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


async def _fetch_clerk_user(clerk_id: str) -> dict | None:
    """Fetch the full user record from Clerk's REST API.

    Default Clerk JWTs only include the `sub` claim (user ID). To get the
    user's email + first/last name we have to call the backend API with the
    secret key. Returns None if the call fails — callers fall back to
    safe defaults so a Clerk outage never blocks sign-in.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://api.clerk.com/v1/users/{clerk_id}",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — Clerk hiccups shouldn't block auth
        log.warning("Clerk user fetch failed for %s: %s", clerk_id, exc)
        return None


def _profile_from_clerk(payload: dict, clerk_user: dict | None, clerk_id: str) -> tuple[str, str]:
    """Resolve (email, display_name) from JWT claims + Clerk API user object."""
    email: str | None = None
    name: str | None = None

    if clerk_user:
        # Pick the primary email address out of the email_addresses array.
        primary_id = clerk_user.get("primary_email_address_id")
        for addr in clerk_user.get("email_addresses", []) or []:
            if addr.get("id") == primary_id or email is None:
                email = addr.get("email_address") or email
                if addr.get("id") == primary_id:
                    break
        first = (clerk_user.get("first_name") or "").strip()
        last = (clerk_user.get("last_name") or "").strip()
        if first or last:
            name = (first + " " + last).strip()
        elif clerk_user.get("username"):
            name = clerk_user.get("username")

    # Fall back to JWT claims if Clerk fetch was unavailable.
    if not email:
        email = payload.get("email") or payload.get("primary_email_address")
    if not name:
        n = (payload.get("name") or payload.get("first_name") or "").strip()
        if n:
            name = n

    # Last-resort fallbacks — never leave the row empty.
    if not email:
        email = f"{clerk_id}@unknown"
    if not name:
        name = email.split("@")[0]
    return email, name


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
        # Auto-provision on first sign-in. Default Clerk JWTs only carry the
        # `sub` claim, so we hit Clerk's REST API to pull the real email +
        # name; otherwise we'd end up with rows like `user_xxx@unknown` that
        # the dashboard can't personalize.
        clerk_user = await _fetch_clerk_user(clerk_id)
        email, name = _profile_from_clerk(payload, clerk_user, clerk_id)
        # If a row was pre-created by a team invite (has email + role but no
        # clerk_id yet), bind it instead of creating a duplicate.
        invited = (
            await db.execute(select(User).where(User.email == email, User.clerk_id.is_(None)))
        ).scalar_one_or_none()
        if invited is not None:
            invited.clerk_id = clerk_id
            if name and not invited.name:
                invited.name = name
            user = invited
            await db.flush()
            log.info("Bound invited row to clerk_id=%s for %s (role=%s)", clerk_id, email, user.role)
        else:
            user = User(clerk_id=clerk_id, email=email, name=name, role=Role.CLIENT)
            db.add(user)
            await db.flush()
            await db.refresh(user)
            log.info("Auto-provisioned user clerk_id=%s email=%s name=%s", clerk_id, email, name)
    elif user.email.endswith("@unknown") or user.name.startswith("user_"):
        # Backfill: an existing row that never got a real email/name (because
        # the Clerk fetch wasn't wired up at sign-in time) gets repaired on
        # the next /auth/me. Idempotent.
        clerk_user = await _fetch_clerk_user(clerk_id)
        email, name = _profile_from_clerk(payload, clerk_user, clerk_id)
        if email != user.email or name != user.name:
            user.email = email
            user.name = name
            await db.flush()
            log.info("Backfilled identity for clerk_id=%s -> %s / %s", clerk_id, email, name)
    return user


def require_role(*roles: Role):
    """Dependency factory for role-gated endpoints."""

    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return user

    return checker


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_valid_credit_pull(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Soft-pull gate for client-facing rate views.

    CLIENT role must have a non-expired completed credit pull on file. Any
    other role (BROKER, LOAN_EXEC, SUPER_ADMIN) is exempt — they need to
    see rates to advise. Returns the original user object so callers can
    chain it as their auth dependency.

    The 403 body uses a structured `code` so the frontend fetch wrapper
    can detect it and trigger the repull modal — distinct from generic
    role 403s.
    """
    from datetime import datetime, timezone

    from app.enums import CreditPullStatus
    from app.models.credit_pull import CreditPull

    if user.role != Role.CLIENT:
        return user
    cid = user.client.id if user.client else None
    if cid is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "credit_pull_required",
                "message": "Soft credit pull required to view rates.",
            },
        )
    stmt = (
        select(CreditPull)
        .where(CreditPull.client_id == cid)
        .where(CreditPull.status == CreditPullStatus.COMPLETED)
        .where(CreditPull.expires_at > datetime.now(timezone.utc))
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "code": "credit_pull_required",
                "message": "Soft credit pull required to view rates.",
            },
        )
    return user


GatedUser = Annotated[User, Depends(require_valid_credit_pull)]
