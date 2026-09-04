"""The client conversation on an AI intake file is a human conversation.

An underwriter answering the borrower there used to run through the same path as
the borrower's own message, so the assistant answered the underwriter — and the
borrower saw that answer in their own room. These tests pin the rule: a person's
message is recorded and never sent to the model, and while the desk has taken the
conversation over the assistant stays out of it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.ai import engagement
from app.services.bucket_ai import create_human_message


def _bucket():
    return SimpleNamespace(id=uuid4(), ai_context={})


def _link(paused_until=None):
    return SimpleNamespace(id=uuid4(), ai_paused_until=paused_until, recipient_name="Loyd Bradley")


def _db():
    db = SimpleNamespace(added=[])
    db.add = db.added.append
    db.flush = AsyncMock()
    return db


# --- the pause window itself -------------------------------------------------


def test_pause_sets_an_hour_and_reads_back_as_paused():
    link = _link()
    until = engagement.pause(link)
    assert link.ai_paused_until == until
    assert engagement.is_paused(link) is True
    # The product's takeover window is one hour, the same as a loan Live Chat.
    assert timedelta(minutes=59) < until - datetime.now(UTC) <= timedelta(hours=1)


def test_pause_lapses_on_its_own_without_anyone_clearing_it():
    link = _link(paused_until=datetime.now(UTC) - timedelta(seconds=1))
    assert engagement.is_paused(link) is False


def test_a_naive_timestamp_does_not_blow_up_the_comparison():
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    link = _link(paused_until=naive)
    assert engagement.is_paused(link) is True


def test_resume_hands_the_conversation_back_immediately():
    link = _link()
    engagement.pause(link)
    engagement.resume(link)
    assert link.ai_paused_until is None
    assert engagement.is_paused(link) is False


def test_an_untouched_link_is_never_paused():
    assert engagement.is_paused(_link()) is False


# --- a human's message never reaches the model -------------------------------


@pytest.mark.asyncio
async def test_human_message_is_recorded_and_the_model_is_never_called():
    db, bucket, link = _db(), _bucket(), _link()
    user = SimpleNamespace(id=uuid4(), name="Jonathan Franco", email="jf@example.com")
    with patch("app.services.bucket_ai.tracked_messages_create", new=AsyncMock()) as model, \
         patch("app.services.bucket_ai.log_bucket_ai_activity", new=AsyncMock()) as activity:
        row = await create_human_message(
            db,
            bucket=bucket,
            audience="uploader",
            message="are you still inside of the system?",
            actor_name="Underwriter — Jonathan Franco",
            user=user,
            upload_link=link,
        )

    model.assert_not_awaited()
    assert row.role == "user"
    assert row.sender_kind == "operator"
    assert row.author_name == "Underwriter — Jonathan Franco"
    assert row.audience == "uploader"
    assert row.upload_link_id == link.id
    # Exactly one row: the person's message. No assistant row of any kind.
    assert len(db.added) == 1
    assert activity.await_count == 1


@pytest.mark.asyncio
async def test_a_borrower_turn_during_a_takeover_is_marked_as_theirs():
    db, bucket, link = _db(), _bucket(), _link()
    with patch("app.services.bucket_ai.tracked_messages_create", new=AsyncMock()) as model, \
         patch("app.services.bucket_ai.log_bucket_ai_activity", new=AsyncMock()):
        row = await create_human_message(
            db,
            bucket=bucket,
            audience="uploader",
            message="sure, my number is 555 0148",
            actor_name="Loyd Bradley",
            upload_link=link,
            sender_kind="client",
        )

    model.assert_not_awaited()
    assert row.sender_kind == "client"
    assert row.user_id is None


# --- the borrower's turn, gated on the takeover ------------------------------


@pytest.mark.asyncio
async def test_client_turn_asks_the_model_when_no_one_has_taken_over():
    from app.routers.dealer_ai_intake import _client_chat_turn

    intake = SimpleNamespace(bucket=_bucket(), bucket_upload_link=_link(), preferred_language="en")
    answer = SimpleNamespace(content="Upload the second agreement.", metadata_json={"raw": {}})
    with patch("app.routers.dealer_ai_intake.create_chat_reply",
               new=AsyncMock(return_value=([SimpleNamespace(), answer], [], None))) as reply:
        messages, assistant_message, paused = await _client_chat_turn(
            None, intake, "can I add them later?", actor_name="Loyd Bradley"
        )

    assert paused is False
    assert assistant_message == "Upload the second agreement."
    assert messages[-1] is answer
    assert reply.await_args.kwargs["sender_kind"] == "client"


@pytest.mark.asyncio
async def test_client_turn_is_recorded_but_unanswered_during_a_takeover():
    from app.routers.dealer_ai_intake import TAKEOVER_CLIENT_NOTICE, _client_chat_turn

    link = _link(paused_until=datetime.now(UTC) + timedelta(minutes=30))
    intake = SimpleNamespace(bucket=_bucket(), bucket_upload_link=link, preferred_language="en")
    row = SimpleNamespace(content="sure, my number is 555 0148")
    with patch("app.routers.dealer_ai_intake.create_chat_reply", new=AsyncMock()) as reply, \
         patch("app.routers.dealer_ai_intake.create_human_message", new=AsyncMock(return_value=row)) as human:
        messages, assistant_message, paused = await _client_chat_turn(
            None, intake, "sure, my number is 555 0148", actor_name="Loyd Bradley"
        )

    reply.assert_not_awaited()
    assert paused is True
    assert messages == [row]
    # The borrower is told a person is answering — never a stale AI recap.
    assert assistant_message == TAKEOVER_CLIENT_NOTICE
    assert human.await_args.kwargs["sender_kind"] == "client"


@pytest.mark.asyncio
async def test_the_model_answers_again_once_the_window_lapses():
    from app.routers.dealer_ai_intake import _client_chat_turn

    link = _link(paused_until=datetime.now(UTC) - timedelta(minutes=1))
    intake = SimpleNamespace(bucket=_bucket(), bucket_upload_link=link, preferred_language="en")
    answer = SimpleNamespace(content="Thanks — I have that now.", metadata_json=None)
    with patch("app.routers.dealer_ai_intake.create_chat_reply",
               new=AsyncMock(return_value=([SimpleNamespace(), answer], [], None))) as reply:
        _messages, assistant_message, paused = await _client_chat_turn(
            None, intake, "anything else?", actor_name="Loyd Bradley"
        )

    assert paused is False
    assert assistant_message == "Thanks — I have that now."
    reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_room_with_no_upload_link_still_answers():
    from app.routers.dealer_ai_intake import _client_chat_turn

    intake = SimpleNamespace(bucket=_bucket(), bucket_upload_link=None, preferred_language="en")
    answer = SimpleNamespace(content="Sure.", metadata_json=None)
    with patch("app.routers.dealer_ai_intake.create_chat_reply",
               new=AsyncMock(return_value=([answer], [], None))):
        _messages, _assistant, paused = await _client_chat_turn(
            None, intake, "hello", actor_name="Loyd Bradley"
        )

    assert paused is False


# --- the routes exist ---------------------------------------------------------


def test_both_client_thread_routes_are_registered():
    from app.routers.dealer_ai_intake import admin_router

    contract = {
        (route.path, method)
        for route in admin_router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/admin/ai-underwriter-leads/{intake_id}/client-thread", "GET") in contract
    assert ("/admin/ai-underwriter-leads/{intake_id}/client-thread/reply", "POST") in contract
    assert ("/admin/ai-underwriter-leads/{intake_id}/client-thread/resume", "POST") in contract


def test_the_takeover_notice_never_pretends_to_be_the_assistant():
    from app.routers.dealer_ai_intake import TAKEOVER_CLIENT_NOTICE

    text = TAKEOVER_CLIENT_NOTICE.lower()
    assert "underwriter" in text
    assert "bucket ai" not in text and "assistant is standing by" in text


# --- the production signing gate reaches the signed-in client too --------------


@pytest.mark.asyncio
async def test_a_signed_in_client_owing_a_signature_cannot_keep_working():
    """The gate closed the token room but not the login.

    A dealer client with a login could keep chatting and uploading while owing a
    signature — the same actions the token room refuses — and could not sign
    either, because signing lives in the room the gate had closed.
    """
    from fastapi import HTTPException

    from app.routers import dealer_ai_intake as router

    intake = SimpleNamespace(id=uuid4(), variant="dealer_gatekeeper_v1", bucket=SimpleNamespace(archived_at=None))
    package = SimpleNamespace(id=uuid4(), stage=1)
    revision = SimpleNamespace(revision_no=2, stage=1, document_title="Production Commitment")

    with patch.object(router, "pending_client_signature", new=AsyncMock(return_value=(package, revision, None)), create=True), \
         patch("app.services.production_signing.pending_client_signature",
               new=AsyncMock(return_value=(package, revision, None))):
        with pytest.raises(HTTPException) as err:
            await router._enforce_production_gate(None, intake)

    assert err.value.status_code == 403
    assert err.value.detail["code"] == "production_signing_required"


@pytest.mark.asyncio
async def test_the_gate_still_ignores_every_non_dealer_file():
    """Production packages exist only on car-industry files, so the gate must
    never close a real-estate, main-street or MCA room."""
    from app.routers import dealer_ai_intake as router

    for variant in ("real_estate_dscr_v1", "main_street_v1", "mca_refi_v1"):
        intake = SimpleNamespace(id=uuid4(), variant=variant)
        # Returns without ever asking whether a signature is pending.
        assert await router._enforce_production_gate(None, intake) is None


def test_the_signed_in_loader_gates_writes_and_lets_reads_render_the_gate():
    import inspect

    from app.routers import dealer_ai_intake as router

    source = inspect.getsource(router._load_client_intake)
    assert "_enforce_production_gate" in source
    assert "allow_pending_signing" in source

    # Reads pass the flag so the client can see what they owe; writes do not.
    body = inspect.getsource(router)
    calls = [line for line in body.splitlines() if "_load_client_intake(db, user, intake_id" in line]
    allowed = [c for c in calls if "allow_pending_signing=True" in c]
    assert len(calls) >= 6
    assert len(allowed) == 2, calls
