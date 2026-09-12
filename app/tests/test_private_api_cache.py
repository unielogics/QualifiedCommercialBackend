from __future__ import annotations

import asyncio

from app.private_api_cache import PrivateApiCacheMiddleware


def _response_headers(*, path: str, method: str = "GET", existing=()):
    messages = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": list(existing)})
        await send({"type": "http.response.body", "body": b"{}"})

    async def capture(message):
        messages.append(message)

    asyncio.run(
        PrivateApiCacheMiddleware(app)(
            {"type": "http", "method": method, "path": path},
            None,
            capture,
        )
    )
    return dict(messages[0]["headers"])


def test_api_gets_are_private_and_never_cached():
    headers = _response_headers(path="/api/v1/communications/threads/sms:1")
    assert headers[b"cache-control"] == b"private, no-store"


def test_existing_cache_policy_cannot_override_live_api_reads():
    headers = _response_headers(
        path="/api/v1/messages",
        existing=[(b"cache-control", b"public, max-age=300")],
    )
    assert headers[b"cache-control"] == b"private, no-store"


def test_non_gets_and_health_routes_are_left_alone():
    assert b"cache-control" not in _response_headers(path="/api/v1/messages", method="POST")
    assert b"cache-control" not in _response_headers(path="/ready")
