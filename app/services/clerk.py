"""Clerk admin API wrapper — currently used to send team invitations.

Lives in services/ so the auth path can stay focused on JWT verification
(deps.py) and the Users router can stay thin.

Network calls are best-effort: when CLERK_SECRET_KEY is unset we no-op and
return None so dev environments without Clerk wired keep working — the local
User row is still created so the team list updates immediately.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.enums import Role

log = logging.getLogger(__name__)


async def invite_user(
    email: str,
    name: str,
    role: Role,
    redirect_url: str | None = None,
) -> dict | None:
    """Send a Clerk invitation email. Returns the Clerk invitation object on
    success, or None when Clerk isn't configured (dev mode) or the call fails.

    The invited user lands on `redirect_url` (defaults to the desktop sign-up
    page). `public_metadata` carries the assigned role + display name so the
    JIT-provision step in deps.get_current_user can apply them on first sign-in.
    """
    settings = get_settings()
    if not settings.clerk_secret_key:
        log.warning(
            "Clerk invite skipped (CLERK_SECRET_KEY unset) — local user row created only."
        )
        return None
    payload: dict = {
        "email_address": email,
        "public_metadata": {"role": role.value, "name": name},
    }
    if redirect_url:
        payload["redirect_url"] = redirect_url
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.clerk.com/v1/invitations",
                headers={
                    "Authorization": f"Bearer {settings.clerk_secret_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code >= 400:
                log.error("Clerk invitation failed (%s): %s", resp.status_code, resp.text)
                return None
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — network failures shouldn't block the row write
        log.exception("Clerk invitation call errored: %s", exc)
        return None


async def revoke_user(clerk_id: str) -> bool:
    """Soft-revoke a Clerk user (disables sign-in, preserves history).

    Used when a super-admin removes a team member. Returns True on success.
    """
    settings = get_settings()
    if not settings.clerk_secret_key or not clerk_id or clerk_id.startswith("pending:"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                f"https://api.clerk.com/v1/users/{clerk_id}",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
            return resp.status_code < 400
    except Exception as exc:  # noqa: BLE001
        log.warning("Clerk revoke failed for %s: %s", clerk_id, exc)
        return False
