"""One name shared by an action and the messages it causes.

Nothing in this codebase could say which user activity produced which email or
text. There was no correlation id anywhere, no middleware to mint one, and no
ContextVar in 731 files — so the only join available was a guess: same actor,
same subject, adjacent timestamps.

These tests pin the spine that replaces the guess, and the two properties that
make it trustworthy: reading a context is always safe, and a scheduler tick is
attributable to its job rather than to nobody.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import request_context as rc

# --- reading is always safe -------------------------------------------------------


def test_an_unbound_context_reads_empty_rather_than_raising():
    """Logging must never be the reason a send fails."""
    assert rc.current().request_id == ""
    assert rc.request_id() == ""
    assert rc.current().actor_user_id is None
    assert rc.current().actor_label == "system"


def test_binding_restores_what_was_there_before():
    with rc.bind(request_id="outer"):
        assert rc.request_id() == "outer"
        with rc.bind(request_id="inner"):
            assert rc.request_id() == "inner"
        assert rc.request_id() == "outer", "an inner bind leaked outward"
    assert rc.request_id() == ""


def test_a_bind_with_no_id_still_gets_one():
    with rc.bind() as ctx:
        assert len(ctx.request_id) == 32


# --- the actor, named later than the request --------------------------------------


def test_the_actor_is_stamped_onto_the_context_already_bound():
    """The id is bound before anyone has read the token, so the actor has to be
    filled in afterwards rather than starting a fresh context."""
    with rc.bind(request_id="r1", actor_label="public"):
        rc.set_actor("user-1", actor_label="super_admin")
        assert rc.request_id() == "r1", "stamping the actor must not change the request id"
        assert rc.current().actor_user_id == "user-1"
        assert rc.current().actor_label == "super_admin"


def test_naming_an_actor_with_nothing_bound_is_a_no_op():
    rc.set_actor("user-1")
    assert rc.current().actor_user_id is None


# --- the middleware ---------------------------------------------------------------


def _run(mw, headers=()):
    seen = {}

    async def app(scope, receive, send):
        seen["id"] = rc.request_id()
        seen["label"] = rc.current().actor_label

    asyncio.run(mw(app)({"type": "http", "headers": list(headers)}, None, None))
    return seen


def test_the_middleware_is_pure_asgi_and_not_a_base_http_middleware():
    """Load-bearing. BaseHTTPMiddleware runs the downstream app in its own
    anyio task, and a ContextVar set on one side of that boundary is not the
    same variable on the other — the id would silently never reach the endpoint.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    assert not issubclass(rc.RequestContextMiddleware, BaseHTTPMiddleware)
    assert callable(rc.RequestContextMiddleware(lambda *a: None))


def test_every_request_gets_an_id():
    seen = _run(rc.RequestContextMiddleware)
    assert len(seen["id"]) == 32
    assert seen["label"] == "public"


def test_an_inbound_request_id_is_honoured_so_a_trace_survives_the_hop():
    seen = _run(rc.RequestContextMiddleware, [(b"x-request-id", b"from-the-frontend")])
    assert seen["id"] == "from-the-frontend"


def test_an_oversized_inbound_id_is_bounded():
    seen = _run(rc.RequestContextMiddleware, [(b"x-request-id", b"z" * 500)])
    assert len(seen["id"]) == 64


def test_a_non_http_scope_passes_straight_through():
    called = {}

    async def app(scope, receive, send):
        called["yes"] = True

    asyncio.run(rc.RequestContextMiddleware(app)({"type": "lifespan"}, None, None))
    assert called == {"yes": True}


# --- cron is not anonymous ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_scheduler_tick_names_its_job():
    from app.services.scheduler import _wrap

    seen = {}

    async def job_admin_activity_digest():
        seen["label"] = rc.current().actor_label
        seen["job"] = rc.current().job
        seen["id"] = rc.request_id()

    await _wrap(job_admin_activity_digest)()
    assert seen["label"] == "cron"
    assert seen["job"] == "job_admin_activity_digest"
    assert seen["id"], "a tick still needs an id, or its sends join to nothing"


@pytest.mark.asyncio
async def test_a_failing_job_still_unbinds_its_context():
    from app.services.scheduler import _wrap

    async def job_that_explodes():
        raise RuntimeError("boom")

    await _wrap(job_that_explodes)()  # _wrap swallows, by design
    assert rc.request_id() == ""


# --- the trails carry it ------------------------------------------------------------


def test_all_three_audit_trails_can_record_the_request():
    from app.dealer_os.models import DealerAuditLog
    from app.models.activity import Activity
    from app.models.bucket import BucketActivityLog

    for model in (BucketActivityLog, DealerAuditLog, Activity):
        col = model.__table__.columns["request_id"]
        assert col.nullable, f"{model.__tablename__}: rows written before this exist"
        assert col.default is not None, f"{model.__tablename__}: a writer could forget"


def test_the_column_default_picks_up_whatever_is_bound():
    """Set by default rather than by the writers — bucket_activity_logs alone
    has five of them, and the next one has not been written yet."""
    from app.models.activity import Activity

    default = Activity.__table__.columns["request_id"].default.arg
    with rc.bind(request_id="abc123"):
        assert default(None) == "abc123"
    assert default(None) is None


@pytest.mark.asyncio
async def test_the_current_user_dependency_names_the_actor():
    from app.deps import get_current_user

    user = SimpleNamespace(id=uuid4(), role="loan_exec")
    with rc.bind(request_id="r9"):
        assert await get_current_user(user) is user
        assert rc.current().actor_user_id == user.id
        assert rc.current().actor_label == "loan_exec"
