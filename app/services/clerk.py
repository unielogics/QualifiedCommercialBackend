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
    account_types: list[str] | None = None,
    account_status: str = "active",
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
    public_metadata: dict = {"role": role.value, "name": name}
    if account_types is not None:
        public_metadata["account_types"] = sorted(set(account_types))
        public_metadata["account_status"] = account_status
    payload: dict = {
        "email_address": email,
        "public_metadata": public_metadata,
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


async def update_user_access_metadata(
    clerk_id: str,
    *,
    role: Role,
    account_types: list[str],
    account_status: str,
) -> bool:
    """Best-effort Clerk routing metadata sync.

    Backend guards remain authoritative; this metadata only lets frontends pick
    the correct landing page before ``/auth/me`` completes.
    """

    settings = get_settings()
    if not settings.clerk_secret_key or not clerk_id or clerk_id.startswith("pending:"):
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                f"https://api.clerk.com/v1/users/{clerk_id}",
                headers={
                    "Authorization": f"Bearer {settings.clerk_secret_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "public_metadata": {
                        "role": role.value,
                        "account_types": sorted(set(account_types)),
                        "account_status": account_status,
                    }
                },
            )
            if resp.status_code >= 400:
                log.warning("Clerk metadata update failed (%s): %s", resp.status_code, resp.text)
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Clerk metadata update errored for %s: %s", clerk_id, exc)
        return False


async def set_user_suspended(clerk_id: str, suspended: bool) -> bool:
    settings = get_settings()
    if not settings.clerk_secret_key or not clerk_id or clerk_id.startswith("pending:"):
        return False
    action = "ban" if suspended else "unban"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.clerk.com/v1/users/{clerk_id}/{action}",
                headers={"Authorization": f"Bearer {settings.clerk_secret_key}"},
            )
            if resp.status_code >= 400:
                log.warning("Clerk %s failed (%s): %s", action, resp.status_code, resp.text)
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Clerk %s errored for %s: %s", action, clerk_id, exc)
        return False


async def revoke_user_sessions(clerk_id: str) -> bool:
    settings = get_settings()
    if not settings.clerk_secret_key or not clerk_id or clerk_id.startswith("pending:"):
        return False
    headers = {"Authorization": f"Bearer {settings.clerk_secret_key}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            success = True
            offset = 0
            while True:
                listed = await client.get(
                    "https://api.clerk.com/v1/sessions",
                    headers=headers,
                    params={"user_id": clerk_id, "limit": 500, "offset": offset},
                )
                if listed.status_code >= 400:
                    log.warning(
                        "Clerk session listing failed (%s): %s",
                        listed.status_code,
                        listed.text,
                    )
                    return False
                payload = listed.json()
                sessions = payload.get("data", []) if isinstance(payload, dict) else payload
                for session in sessions or []:
                    session_id = session.get("id") if isinstance(session, dict) else None
                    if not session_id or session.get("status") == "revoked":
                        continue
                    response = await client.post(
                        f"https://api.clerk.com/v1/sessions/{session_id}/revoke",
                        headers=headers,
                    )
                    success = success and response.status_code < 400
                count = len(sessions or [])
                total = payload.get("total_count", count) if isinstance(payload, dict) else count
                offset += count
                if count == 0 or offset >= total:
                    break
            return success
    except Exception as exc:  # noqa: BLE001
        log.warning("Clerk session revoke errored for %s: %s", clerk_id, exc)
        return False


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
