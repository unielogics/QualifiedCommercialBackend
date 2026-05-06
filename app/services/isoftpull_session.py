"""Server-side iSoftPull dashboard session.

Bridge integration: until the API token has the "Full Feed" option
enabled, the iSoftPull `POST /api/v2/reports` response only carries a
report link, not the parsed score. To get the FICO out, we log into the
iSoftPull web dashboard as a real user, fetch the report HTML page, and
parse the score out of it.

This module owns the HTTP client + cookie jar + login lifecycle. It is
lock-protected so concurrent credit pulls don't pile up duplicate
logins. Sessions auto-refresh on 401/302-to-sign-in.

Goes away the moment the API token is upgraded — search the codebase
for `isoftpull_session` and `isoftpull_report_parser` to find every
reference to delete.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

# Treat the session as fresh for this many seconds before re-validating.
# Rails default session lifetime is hours, but we cap our trust window
# short so we re-login proactively if iSoftPull rotates anything.
_SESSION_VALID_SECONDS = 30 * 60  # 30 minutes


class IsoftpullSessionError(Exception):
    """Login or session-renewal failed."""


@dataclass
class _CachedSession:
    client: httpx.AsyncClient
    obtained_at: float


class IsoftpullSession:
    """Singleton holder for the authenticated dashboard client."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cached: _CachedSession | None = None

    async def get(self) -> httpx.AsyncClient:
        """Return an authenticated httpx client. Logs in / refreshes on demand."""
        async with self._lock:
            if self._cached and (time.time() - self._cached.obtained_at) < _SESSION_VALID_SECONDS:
                return self._cached.client
            await self._login()
            assert self._cached is not None
            return self._cached.client

    async def invalidate(self) -> None:
        """Force the next `get()` to re-login. Call after a 401/302-to-login."""
        async with self._lock:
            if self._cached is not None:
                try:
                    await self._cached.client.aclose()
                except Exception:  # noqa: BLE001
                    pass
                self._cached = None

    async def _login(self) -> None:
        settings = get_settings()
        if not settings.isoftpull_login_email or not settings.isoftpull_login_password:
            raise IsoftpullSessionError(
                "iSoftPull dashboard login is not configured. "
                "Set ISOFTPULL_LOGIN_EMAIL and ISOFTPULL_LOGIN_PASSWORD."
            )

        # Each cached session has its own client + cookie jar.
        client = httpx.AsyncClient(
            base_url=settings.isoftpull_dashboard_url,
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            follow_redirects=False,
        )
        try:
            r = await client.get("/users/sign_in")
            if r.status_code >= 400:
                raise IsoftpullSessionError(f"GET /users/sign_in returned {r.status_code}")
            m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', r.text)
            if not m:
                raise IsoftpullSessionError("CSRF token not found on sign-in page")
            token = m.group(1)

            r = await client.post(
                "/users/sign_in",
                data={
                    "authenticity_token": token,
                    "user[email]": settings.isoftpull_login_email,
                    "user[password]": settings.isoftpull_login_password,
                    "user[remember_me]": "0",
                    "commit": "Log in",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": settings.isoftpull_dashboard_url + "/users/sign_in",
                },
            )
            # Successful Devise login → 302 to dashboard. Failed login → 200 back
            # on /users/sign_in with an error in the body.
            if r.status_code != 302:
                raise IsoftpullSessionError(
                    f"login POST returned {r.status_code} (expected 302)"
                )
            location = r.headers.get("location") or ""
            if "/sign_in" in location:
                raise IsoftpullSessionError(
                    "login POST redirected back to /sign_in — invalid credentials?"
                )
            log.info("iSoftPull dashboard login succeeded; redirected to %s", location)
        except IsoftpullSessionError:
            await client.aclose()
            raise
        except Exception as exc:
            await client.aclose()
            raise IsoftpullSessionError(f"login failed: {exc}") from exc

        self._cached = _CachedSession(client=client, obtained_at=time.time())

    async def fetch(self, url: str) -> httpx.Response:
        """GET a URL with the cached session. Auto-renews on 302→/sign_in.

        `url` may be a full https URL or a path relative to the dashboard.
        """
        client = await self.get()
        # follow_redirects=False so we can detect session expiration cleanly.
        resp = await client.get(url, follow_redirects=False)
        location = resp.headers.get("location") or ""
        if resp.status_code in (301, 302) and "/users/sign_in" in location:
            log.info("iSoftPull session expired (302 -> sign_in); re-logging in")
            await self.invalidate()
            client = await self.get()
            resp = await client.get(url, follow_redirects=False)

        # If we got a non-redirect non-2xx, return as-is for the caller to
        # decide. If we got a 2xx, return as-is.
        if 300 <= resp.status_code < 400 and "/users/sign_in" in (resp.headers.get("location") or ""):
            raise IsoftpullSessionError("session refresh failed: still bouncing to /sign_in")
        return resp


# Module-level singleton — share the cookie jar across all credit pulls.
_singleton: IsoftpullSession | None = None


def get_session() -> IsoftpullSession:
    global _singleton
    if _singleton is None:
        _singleton = IsoftpullSession()
    return _singleton
