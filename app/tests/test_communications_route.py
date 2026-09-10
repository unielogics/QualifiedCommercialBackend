"""The communication event stream, at the route.

`GET /api/v1/communications/events` authenticates on a session it owns, then
streams forever. Every connection to it 500'd for weeks after `get_current_user`
lost its four-parameter shape, and CI stayed green because nothing opened the
route — the broker tests never touched it. These do.

The stream is unbounded, so it is driven through the ASGI interface directly:
httpx's ASGITransport buffers the whole body before returning, and Starlette's
TestClient withholds `http.disconnect` until the response completes. A
hand-rolled `receive`/`send` pair reads the first frames and then disconnects.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException

from app import deps, request_context
from app.routers import communications

PATH = "/api/v1/communications/events"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(communications.router, prefix="/api/v1")
    return app


class _FakeSession:
    """The stream's own auth session: commit/rollback recorded, nothing else."""

    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


@asynccontextmanager
async def _session_factory_for(session: _FakeSession):
    yield session


async def _drive(
    app: FastAPI,
    *,
    headers: dict[str, str],
    stop_when: bytes,
) -> tuple[int, list[bytes]]:
    """Run one GET through the app; disconnect once `stop_when` is in the body."""
    sent: list[dict[str, Any]] = []
    disconnect = asyncio.Event()
    request_delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and stop_when in (message.get("body") or b""):
            disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": PATH,
        "raw_path": PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": "testserver", **headers}.items()],
        "client": ("127.0.0.1", 40000),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(m for m in sent if m["type"] == "http.response.start")
    chunks = [m.get("body") or b"" for m in sent if m["type"] == "http.response.body"]
    return int(start["status"]), chunks


@pytest.mark.asyncio
async def test_events_route_authenticates_and_sends_the_ready_frame() -> None:
    user = SimpleNamespace(id=uuid4(), role="super_admin")
    session = _FakeSession()
    resolver = AsyncMock(return_value=user)

    with (
        patch.object(communications, "SessionLocal", lambda: _session_factory_for(session)),
        patch.object(communications, "resolve_user_from_headers", resolver),
    ):
        status, chunks = await _drive(
            _app(),
            headers={"authorization": "Bearer t0k", "x-dev-user": "admin@qc.dev"},
            stop_when=b"sync.required",
        )

    assert status == 200
    body = b"".join(chunks)
    assert body.startswith(b"retry: 3000\n\n")
    assert b"event: sync.required\n" in body
    assert f'"id":"ready:{user.id}"'.encode() in body

    # The seam is called the way it is declared: request positional, the
    # three credentials keyword-only, on the route's own session — and that
    # session is committed, not rolled back.
    resolver.assert_awaited_once()
    args, kwargs = resolver.await_args.args, resolver.await_args.kwargs
    assert len(args) == 1 and args[0].url.path == PATH
    assert "request" not in kwargs
    assert kwargs["authorization"] == "Bearer t0k"
    assert kwargs["x_dev_user"] == "admin@qc.dev"
    assert kwargs["db"] is session
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_events_route_reports_a_broken_resolver_instead_of_swallowing_it(caplog) -> None:
    """The regression itself: a resolver that blows up must leave a log line."""
    session = _FakeSession()

    async def broken(*_args, **_kwargs):
        raise TypeError("got an unexpected keyword argument 'request'")

    with (
        patch.object(communications, "SessionLocal", lambda: _session_factory_for(session)),
        patch.object(communications, "resolve_user_from_headers", broken),
        caplog.at_level(logging.ERROR, logger="app.routers.communications"),
        pytest.raises(TypeError),
    ):
        await _drive(_app(), headers={"authorization": "Bearer t0k"}, stop_when=b"never")

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    record = next(r for r in caplog.records if "communication event stream auth failed" in r.getMessage())
    assert record.exc_info and record.exc_info[0] is TypeError


@pytest.mark.asyncio
async def test_events_route_refuses_a_bad_credential_with_its_status(caplog) -> None:
    session = _FakeSession()

    async def refused(*_args, **_kwargs):
        raise HTTPException(401, "Invalid token")

    with (
        patch.object(communications, "SessionLocal", lambda: _session_factory_for(session)),
        patch.object(communications, "resolve_user_from_headers", refused),
        caplog.at_level(logging.WARNING, logger="app.routers.communications"),
    ):
        status, _chunks = await _drive(_app(), headers={"authorization": "Bearer bad"}, stop_when=b"never")

    assert status == 401
    session.rollback.assert_awaited_once()
    assert any("communication event stream refused: status=401" in r.getMessage() for r in caplog.records)
    # An ordinary refusal is a warning line, not a traceback.
    assert not any(r.exc_info for r in caplog.records if r.name == "app.routers.communications")


@pytest.mark.asyncio
async def test_resolve_user_from_headers_wraps_the_resolver_and_names_the_actor() -> None:
    user = SimpleNamespace(id=uuid4(), role="loan_exec")
    request = SimpleNamespace(url=SimpleNamespace(path=PATH))
    db = object()
    resolver = AsyncMock(return_value=user)

    with patch.object(deps, "_resolve_current_user", resolver), request_context.bind(request_id="r-sse"):
        got = await deps.resolve_user_from_headers(request, authorization="Bearer t", x_dev_user=None, db=db)
        assert got is user
        assert request_context.current().actor_user_id == user.id
        assert request_context.current().actor_label == "loan_exec"

    resolver.assert_awaited_once_with(request, "Bearer t", None, db)
