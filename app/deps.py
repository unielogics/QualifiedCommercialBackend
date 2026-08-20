"""Auth dependencies — Clerk JWT verification + role gates.

When CLERK_SECRET_KEY is unset (local dev without Clerk wired yet), every
request is treated as a dev super_admin so screens can be built end-to-end.
The runtime warns once at startup if dev mode is active.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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


# Clerk marks a session that still owes a required task — setting up two-step
# verification, for instance — with session status "pending". The task lives in
# the session, not the user, so a token minted for a pending session verifies
# against JWKS perfectly well and would otherwise sail through every guard
# below. Rejecting it here is the only place that catches a client which skips
# the setup step, whether by an old app build or a crafted request.
#
# Claim shapes differ by Clerk token version, so read all of them rather than
# betting on one:
#   v2 tokens carry "sts" ("pending" | "active").
#   Some carry "act"/"tasks" describing the outstanding task.
#   "fva" (factor verification age) is [first_factor_age, second_factor_age]
#   with -1 in the second slot when no second factor has ever been verified.
_PENDING_STATUSES = {"pending"}


def _session_is_pending(payload: dict) -> bool:
    sts = payload.get("sts")
    if isinstance(sts, str) and sts.lower() in _PENDING_STATUSES:
        return True
    tasks = payload.get("tasks") or payload.get("act")
    if isinstance(tasks, list) and tasks:
        return True
    if isinstance(tasks, dict) and tasks.get("key"):
        return True
    return False


def _second_factor_missing(payload: dict) -> bool:
    """True only when the token positively says no second factor was verified.

    Deliberately conservative: an absent `fva` means an older token format, not
    a missing factor, and treating absence as failure would lock out everyone
    the moment this flag is switched on.
    """
    fva = payload.get("fva")
    if isinstance(fva, (list, tuple)) and len(fva) >= 2:
        try:
            return int(fva[1]) < 0
        except (TypeError, ValueError):
            return False
    return False


def _enforce_mfa(payload: dict) -> None:
    # get_settings() per call, matching every other function in this module.
    # A module-level binding would also freeze the flag at import time, which
    # defeats the point of being able to flip it without a rebuild.
    if not get_settings().require_mfa:
        return
    if _session_is_pending(payload) or _second_factor_missing(payload):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Two-step verification is required. Set it up at /account/security, "
            "then sign in again.",
        )


async def _verify_clerk_jwt(token: str) -> dict:
    """Verify a Clerk-issued JWT against the project's JWKS endpoint."""
    settings = get_settings()
    jwks = await _get_jwks()
    if not jwks:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Clerk JWKS not configured")
    try:
        headers = jwt.get_unverified_header(token)
    except Exception as exc:
        # A non-JWT string (corrupted storage, truncation, plain garbage)
        # raises ValueError/DecodeError BEFORE the guarded decode below —
        # that must be a 401 so clients re-authenticate, never a 500.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed bearer token") from exc
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
    # Eager-load BOTH User.client and User.broker on every load path.
    # Multiple downstream routes touch `user.client.id` (CLIENT scoping)
    # or `user.broker.id` (BROKER scoping in /loans, /clients, /reports,
    # /ai-tasks, /agents/me/*). Bare relationship access in async session
    # context raises MissingGreenlet because lazy-loading needs greenlet
    # support. Loading them once here makes both relationships safe to
    # touch anywhere downstream.
    _with_client = selectinload(User.client)
    _with_broker = selectinload(User.broker)

    if not settings.clerk_secret_key:
        # Dev mode: short-circuit auth
        stmt = (
            select(User).options(_with_client, _with_broker).where(User.email == x_dev_user)
            if x_dev_user
            else select(User).options(_with_client, _with_broker).where(User.role == Role.SUPER_ADMIN).limit(1)
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
    _enforce_mfa(payload)
    clerk_id = payload.get("sub")
    if not clerk_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token missing subject")
    user = (
        await db.execute(
            select(User).options(_with_client, _with_broker).where(User.clerk_id == clerk_id)
        )
    ).scalar_one_or_none()
    if user is None:
        # Auto-provision on first sign-in. Default Clerk JWTs only carry the
        # `sub` claim, so we hit Clerk's REST API to pull the real email +
        # name; otherwise we'd end up with rows like `user_xxx@unknown` that
        # the dashboard can't personalize.
        clerk_user = await _fetch_clerk_user(clerk_id)
        email, name = _profile_from_clerk(payload, clerk_user, clerk_id)
        # If a row was pre-created by a team invite (has email + role but no
        # clerk_id yet), bind it instead of creating a duplicate.
        # Case-insensitive match: invited rows (e.g. vendor bucket access) are
        # stored lower-cased, but Clerk may return the email in a different
        # case. A case-sensitive compare would miss the invite and mint a
        # duplicate CLIENT row, silently stripping the user's granted access.
        invited = (
            await db.execute(
                select(User).options(_with_client, _with_broker).where(
                    func.lower(User.email) == (email or "").lower(), User.clerk_id.is_(None)
                )
            )
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
            # Adoption guard — when an agent pre-creates a Client by
            # email (operator-side intake, agent invite, dashboard "+ New
            # lead"), that row sits with `user_id IS NULL` until the
            # borrower actually signs in via Clerk and a User row gets
            # auto-provisioned. Without this lookup the new User has no
            # linked Client → /clients/me 404s → mobile silently falls
            # back to self_directed AND the borrower can't see the
            # credit pull / docs / loan history the agent attached.
            # Match by email + null user_id so we never adopt a Client
            # that already belongs to someone else.
            #
            # We also stamp client_experience_mode=guided here when the
            # adopted row had a broker (the same default the operator
            # intake flow uses) so the deriveExperienceMode fallback on
            # the client side does the right thing immediately.
            from app.models.client import Client as _Client

            adoptable = (
                await db.execute(
                    select(_Client)
                    # Case-insensitive for the same reason the invite bind
                    # above is: Clerk returns whatever case the person typed,
                    # and an agent-created Client row is stored lower-cased.
                    # A case-sensitive compare here silently fails to adopt,
                    # which costs the borrower their documents, credit pull and
                    # loan history without any error being raised.
                    .where(
                        func.lower(_Client.email) == (email or "").lower(),
                        _Client.user_id.is_(None),
                    )
                    .order_by(_Client.created_at.desc())
                )
            ).scalars().all()
            if adoptable:
                target = adoptable[0]
                target.user_id = user.id
                if target.client_experience_mode is None and target.broker_id is not None:
                    target.client_experience_mode = "guided"
                    if target.client_experience_mode_reason is None:
                        target.client_experience_mode_reason = "agent_invited"
                    if target.client_experience_mode_locked_by is None:
                        target.client_experience_mode_locked_by = "agent"
                await db.flush()
                log.info(
                    "Adopted pre-existing Client id=%s email=%s onto auto-provisioned user clerk_id=%s",
                    target.id, email, clerk_id,
                )
                if len(adoptable) > 1:
                    # Surface duplicates so we don't silently leave
                    # historical credit pulls / docs stranded on a
                    # different row. Operators will see this in logs
                    # and can merge via the support runbook.
                    log.warning(
                        "Multiple orphan Client rows for email=%s (%d found). Adopted the newest; "
                        "older rows still hold credit pulls / docs / loans and need manual merge.",
                        email, len(adoptable),
                    )
            else:
                from app.services.payment_authorization import primary_super_admin

                owner = await primary_super_admin(db)
                client = _Client(
                    user_id=user.id,
                    name=name or email.split("@")[0],
                    email=email,
                    originating_agent_id=owner.id if owner else None,
                    current_agent_id=owner.id if owner else None,
                    source_channel="self_signup",
                    client_experience_mode="self_directed",
                    client_experience_mode_reason="self_signup_super_admin_assigned",
                    client_experience_mode_locked_by="firm",
                )
                db.add(client)
                await db.flush()
                log.info(
                    "Auto-created Client id=%s for self-signup email=%s assigned_owner=%s",
                    client.id,
                    email,
                    owner.email if owner else None,
                )
            # Re-fetch with the relationship eagerly loaded so downstream
            # `user.client` accesses don't trigger a lazy-load. A freshly
            # added User without a Client row would still need the eager
            # load; one that just adopted a Client needs the relationship
            # to reflect the link.
            user = (
                await db.execute(
                    select(User).options(_with_client, _with_broker).where(User.id == user.id)
                )
            ).scalar_one()
            log.info("Auto-provisioned user clerk_id=%s email=%s name=%s", clerk_id, email, name)
    elif (
        user.email.endswith("@unknown")
        or user.name.startswith("user_")
        or _looks_like_email_fallback_name(user.name, user.email)
    ):
        # Backfill: an existing row that never got a real email/name
        # (because the Clerk fetch wasn't wired up at sign-in time, or
        # because the user's Clerk profile was empty at first sign-in
        # and we wrote the email local-part as a placeholder name) gets
        # repaired on the next /auth/me. Idempotent.
        #
        # We also propagate any name change down to user.client.name
        # when it's safe to do so (Client.name matched the old
        # user.name, or it also looks like an email-fallback). This
        # keeps the borrower's display identity consistent across
        # User-driven UI (auth header) and Client-driven UI (PDF
        # letter, credit pulls, calendar invitees) without clobbering
        # a Client.name that an operator manually customized.
        clerk_user = await _fetch_clerk_user(clerk_id)
        email, name = _profile_from_clerk(payload, clerk_user, clerk_id)
        if email != user.email or name != user.name:
            old_name = user.name
            user.email = email
            user.name = name
            client = getattr(user, "client", None)
            if client is not None and client.name and (
                client.name == old_name
                or _looks_like_email_fallback_name(client.name, user.email)
            ):
                client.name = name
            await db.flush()
            log.info(
                "Backfilled identity for clerk_id=%s email=%s name=%r (was %r)",
                clerk_id, email, name, old_name,
            )

    # Ensure every BROKER-role user has a Broker row. Without one, the
    # `user.broker.id` scope filter in /loans, /clients, /reports,
    # /ai-tasks, /agents/me/* silently no-ops — meaning the broker
    # would see firm-wide data instead of their own book. Auto-create
    # on first authed request (idempotent — only fires when missing).
    if user.role == Role.BROKER and user.broker is None:
        from app.models.broker import Broker as _Broker
        broker = _Broker(
            user_id=user.id,
            display_name=user.name or user.email or "Broker",
        )
        db.add(broker)
        await db.flush()
        # Re-fetch with relationships loaded so downstream
        # `user.broker.id` resolves without a lazy round-trip.
        user = (
            await db.execute(
                select(User)
                .options(_with_client, _with_broker)
                .where(User.id == user.id)
            )
        ).scalar_one()
        log.info(
            "Auto-provisioned Broker row for user=%s email=%s",
            user.id, user.email,
        )
    # Presence (alembic 0046) — bump on every authed request. The
    # column is indexed (partial, WHERE NOT NULL) so future "who's
    # online" queries stay cheap. We update at most once per minute to
    # avoid hammering Postgres on the chatty endpoints (workspace,
    # secretary, recalc) that fire 5-10x per page load.
    from datetime import timedelta as _td
    now_dt = datetime.now(timezone.utc)
    if user.last_seen_at is None or (now_dt - user.last_seen_at) >= _td(seconds=60):
        user.last_seen_at = now_dt
        await db.flush()
    return user


def _looks_like_email_fallback_name(name: str | None, email: str | None) -> bool:
    """Heuristic for "this name was auto-generated from the email
    local-part because Clerk had no first/last on file at first
    sign-in." When True we treat the name as stale and re-pull from
    Clerk on the next authed request.

    Matches: lowercase / no whitespace / equals the email local-part
    case-insensitively. Doesn't match a real one-word name like
    "Madonna" because Clerk profiles for those would typically be set
    explicitly anyway and not equal the email's local-part."""
    if not name or not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    return name.strip().lower() == local


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
    from app.services.payment_authorization import require_payment_authorized_for_credit

    await require_payment_authorized_for_credit(db, user)
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
