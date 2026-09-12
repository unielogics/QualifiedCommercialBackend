"""Prevent mutable API reads from being reused by browsers or proxies.

The UI owns short-lived state through React Query. HTTP caching underneath it
is harmful: a conversation can poll successfully while receiving the same old
response until the user reloads the page. Applying the rule at the API boundary
keeps every current and future message endpoint protected without relying on
each router author to remember a header.
"""

from __future__ import annotations


class PrivateApiCacheMiddleware:
    """Set ``private, no-store`` on every GET below the versioned API root."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() != "GET"
            or not scope.get("path", "").startswith("/api/v1")
        ):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message):
            if message.get("type") == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"private, no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_no_store)
