"""Who is doing this, and which request is it part of.

Nothing in this codebase could answer either question. There was no
correlation id, no request id, and no middleware of any kind — `main.py`
registered CORS and nothing else — so a message and the action that caused it
were two rows written in one transaction with no name in common. Joining them
meant guessing from actor and timestamp.

One ContextVar fixes that. A request binds an id; the audit writers stamp it on
the row they write; the message log stamps the same id on the row it writes.
"Which user activity triggered what" becomes an exact join.

Two rules this module exists to keep:

- **Reading is always safe.** `current()` returns an empty context rather than
  raising when nothing is bound — a background task, a test, a script. Logging
  must never be the reason a send fails.
- **Cron is not anonymous.** A scheduled job binds its own context naming the
  job, so a message from the five-minute digest is attributable to the digest
  rather than to nobody.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RequestContext:
    request_id: str = ""
    actor_user_id: object = None
    #: user | cron | system | public — who is acting, when there is no user id.
    actor_label: str = "system"
    #: the scheduler job name, for cron-originated work.
    job: str = ""


_EMPTY = RequestContext()
_ctx: ContextVar[RequestContext] = ContextVar("qc_request_context", default=_EMPTY)


def current() -> RequestContext:
    """The bound context, or an empty one. Never raises."""
    try:
        return _ctx.get()
    except LookupError:  # pragma: no cover - default makes this unreachable
        return _EMPTY


def request_id() -> str:
    return current().request_id


def new_id() -> str:
    return uuid.uuid4().hex


@contextmanager
def bind(*, request_id: str = "", actor_user_id=None, actor_label: str = "system", job: str = ""):
    """Bind a context for the duration of the block, then restore the previous
    one. Nested binds are fine — an inner block does not leak outward."""
    token = _ctx.set(
        RequestContext(
            request_id=request_id or new_id(),
            actor_user_id=actor_user_id,
            actor_label=actor_label,
            job=job,
        )
    )
    try:
        yield current()
    finally:
        _ctx.reset(token)


def set_actor(user_id, *, actor_label: str = "user") -> None:
    """Name the actor on the context already bound.

    The actor is not known when the request is first bound — it is resolved
    later, by the dependency that reads the token — so this fills it in rather
    than starting a new context.
    """
    ctx = current()
    if not ctx.request_id:
        return
    _ctx.set(replace(ctx, actor_user_id=user_id, actor_label=actor_label))


class RequestContextMiddleware:
    """Bind a request id for the life of every HTTP request.

    Deliberately a pure ASGI middleware rather than a BaseHTTPMiddleware
    subclass: BaseHTTPMiddleware runs the downstream app in its own anyio task,
    and a ContextVar set on one side of that boundary is not the same variable
    on the other. Pure ASGI stays in the caller's task, so the id a request
    binds here is the id its endpoint, its audit rows and its sends all see.

    An inbound `X-Request-Id` is honoured so a trace can be followed across the
    frontend and the API, but it is bounded and stripped — it lands in a log
    that people read.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        incoming = ""
        for key, value in scope.get("headers") or ():
            if key == b"x-request-id":
                incoming = value.decode("latin-1", "ignore").strip()[:64]
                break
        with bind(request_id=incoming or new_id(), actor_label="public"):
            await self.app(scope, receive, send)
