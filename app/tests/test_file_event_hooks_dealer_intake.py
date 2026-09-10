"""File-timeline hooks in the dealer AI intake router and the bucket AI service.

Runs without a database or a model call. Source pins keep every hook where
it was placed, at the tier it was given; the behavioural tests run the hooked
functions against fakes and check what `file_events.emit` was asked to record
— what happened, never what was said.
"""

from __future__ import annotations

import inspect
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.routers import dealer_ai_intake as intake_router
from app.services import bucket_ai as ai
from app.services import file_events
from app.services import merchant_processing as mp

# ── source pins ─────────────────────────────────────────────────────────────


def _emit_calls(src: str) -> list[str]:
    """The text of each `file_events.emit(...)` call in a function's source."""
    return re.findall(r"file_events\.emit\((.*?)\n\s*\)\n", src, flags=re.DOTALL)


@pytest.mark.parametrize(
    ("handler", "kind", "visibilities"),
    [
        (intake_router._ensure_requested_document, "document.requested", ["VISIBILITY_CLIENT"]),
        (intake_router._complete_upload, "document.received", ["VISIBILITY_CLIENT", "VISIBILITY_TEAM"]),
        (intake_router._submit_pfs_form, "document.received", ["VISIBILITY_CLIENT"]),
        (intake_router._submit_debt_schedule_form, "document.received", ["VISIBILITY_CLIENT"]),
        (intake_router.create_admin_lead_note, "message.sent", ["VISIBILITY_TEAM"]),
        (intake_router.create_broker_lead_note, "message.sent", ["VISIBILITY_TEAM"]),
        (intake_router.update_lead_outcome_status, "status.changed", ["VISIBILITY_CLIENT"]),
        (ai.create_human_message, "message.sent", ["VISIBILITY_CLIENT"]),
        (ai.create_chat_reply, "message.sent", ["VISIBILITY_CLIENT"]),
        (ai.run_bucket_ai_review, "review.completed", ["VISIBILITY_TEAM"]),
    ],
)
def test_each_hook_is_pinned_in_source(handler, kind, visibilities):
    src = inspect.getsource(handler)
    calls = _emit_calls(src)
    assert "file_events.emit(" in src and len(calls) == 1
    assert f'kind="{kind}"' in calls[0]
    for visibility in visibilities:
        assert f"file_events.{visibility}" in calls[0]
    # Only the three tiers named for the site, nothing else.
    for other in {"VISIBILITY_CLIENT", "VISIBILITY_TEAM", "VISIBILITY_DESK"} - set(visibilities):
        assert f"file_events.{other}" not in calls[0]


def test_the_upload_hook_skips_the_offer_sheet_and_sits_before_the_commit():
    src = inspect.getsource(intake_router._complete_upload)
    assert "if not merchant_processing.is_offer_document(file):" in src
    assert src.index("file_events.emit(") < src.index("await db.commit()")


def test_the_note_hooks_carry_the_author_not_the_body():
    for handler in (intake_router.create_admin_lead_note, intake_router.create_broker_lead_note):
        src = inspect.getsource(handler)
        (call,) = _emit_calls(src)
        assert "user.name" in call
        assert "payload.content" not in call and "note.content" not in call
        assert src.index("file_events.emit(") < src.index("await db.commit()")


def test_the_outcome_hook_fires_only_on_a_change_and_names_the_outcome_in_plain_words():
    src = inspect.getsource(intake_router.update_lead_outcome_status)
    assert "previous_outcome = intake.outcome_status" in src
    assert "if payload.outcome_status != previous_outcome:" in src
    assert intake_router._OUTCOME_EVENT_TITLES == {"closed": "File closed", "denied": "File denied", "submitted": "File reopened"}


def test_the_chat_hooks_watch_only_the_client_thread_and_use_a_local_import():
    for handler in (ai.create_human_message, ai.create_chat_reply):
        src = inspect.getsource(handler)
        assert 'if audience == "uploader":' in src
        assert "from app.services import file_events" in src
        (call,) = _emit_calls(src)
        assert '"Message from {actor_name}" if sender_kind == "client" else "Reply from the desk"' in call
        assert "message" not in call.replace("bucket_ai_message", "").replace("message.sent", "")
    # The chat-reply hook sits on the user row, before the model is asked anything.
    src = inspect.getsource(ai.create_chat_reply)
    assert src.index("file_events.emit(") < src.index("context = await _chat_context(")


def test_the_review_hook_fires_once_the_row_is_completed_and_carries_only_the_verdict():
    src = inspect.getsource(ai.run_bucket_ai_review)
    assert 'if review.status == "completed":' in src
    assert src.index('review.status = "failed"') < src.index("file_events.emit(")
    (call,) = _emit_calls(src)
    assert 'f"Review completed: {probability}" if probability else "Review completed"' in call
    assert "body=" not in call and "result" not in call


# ── behaviour: dealer_ai_intake ─────────────────────────────────────────────


def _db(**extra):
    base = dict(add=lambda row: None, flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    base.update(extra)
    return SimpleNamespace(**base)


async def test_requesting_a_new_document_tells_the_client_and_an_existing_one_is_quiet():
    bucket = SimpleNamespace(id=uuid.uuid4(), requested_documents=[])
    actor = SimpleNamespace(id=uuid.uuid4(), name="Ana Lopez", email="ana@example.com")
    with patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit:
        doc = await intake_router._ensure_requested_document(
            _db(), bucket, name="Debt schedule", category="Debts", description="d", actor=actor
        )
        assert doc.name == "Debt schedule" and bucket.requested_documents == [doc]
        emit.assert_awaited_once()
        kwargs = emit.await_args.kwargs
        assert kwargs["bucket_id"] == bucket.id
        assert kwargs["kind"] == "document.requested"
        assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT
        assert kwargs["title"] == "We asked for Debt schedule"
        assert kwargs["actor"] is actor
        assert kwargs["target_type"] == "requested_document" and kwargs["target_id"] == doc.id

        again = await intake_router._ensure_requested_document(_db(), bucket, name="Debt schedule", category="Debts")
        assert again is doc
        emit.assert_awaited_once()  # the idempotent path adds nothing to the timeline


def _upload_fixture(**file_overrides):
    bucket_id, link_id = uuid.uuid4(), uuid.uuid4()
    file = SimpleNamespace(
        id=uuid.uuid4(),
        bucket_id=bucket_id,
        upload_link_id=link_id,
        deleted_at=None,
        status="pending",
        requested_document_id=None,
        source_kind="client_room",
        source_detail=None,
        file_name="Bank statement.pdf",
        uploaded_by_user_id=None,
    )
    for key, value in file_overrides.items():
        setattr(file, key, value)
    intake = SimpleNamespace(id=uuid.uuid4(), bucket_id=bucket_id, bucket_upload_link_id=link_id, bucket_upload_link=None)
    payload = SimpleNamespace(file_id=file.id, note=None)
    db = _db(
        get=AsyncMock(return_value=file),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))),
    )
    return db, intake, payload, file


async def _complete(db, intake, payload, *, expect_emit: bool = True):
    with (
        patch.object(intake_router, "_extract_zip_bucket_files", AsyncMock()),
        patch.object(intake_router, "_log", AsyncMock()),
        patch("app.services.bucket_evidence.reconcile_uploaded_file", AsyncMock()),
        patch("app.services.bucket_ai.enqueue_file_analysis", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        # The line must be on the timeline before the request commits.
        db.commit.side_effect = lambda: None if emit.await_count == (1 if expect_emit else 0) else pytest.fail("emit/commit order")
        await intake_router._complete_upload(db, intake, payload, SimpleNamespace(), actor_name="Ana Lopez", actor_email="ana@example.com")
        return emit


async def test_a_client_room_upload_is_received_at_the_client_tier_before_the_commit():
    db, intake, payload, file = _upload_fixture()
    emit = await _complete(db, intake, payload)
    assert file.status == "uploaded"
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["intake_id"] == intake.id
    assert kwargs["kind"] == "document.received"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT
    assert kwargs["title"] == "Bank statement.pdf was received"
    assert kwargs["actor"] is None and kwargs["actor_label"] == "Ana Lopez"
    assert kwargs["target_type"] == "file" and kwargs["target_id"] == file.id
    db.commit.assert_awaited_once()


async def test_a_desk_side_upload_is_received_at_the_team_tier():
    db, intake, payload, file = _upload_fixture(source_kind="internal_upload", uploaded_by_user_id=uuid.uuid4())
    emit = await _complete(db, intake, payload)
    assert emit.await_args.kwargs["visibility"] == file_events.VISIBILITY_TEAM
    assert emit.await_args.kwargs["actor"] == file.uploaded_by_user_id


async def test_the_partners_offer_sheet_never_reaches_the_timeline():
    db, intake, payload, _ = _upload_fixture(source_detail=mp.OFFER_SOURCE_DETAIL)
    emit = await _complete(db, intake, payload, expect_emit=False)
    emit.assert_not_awaited()
    db.commit.assert_awaited_once()


async def test_a_submitted_pfs_is_received_at_the_client_tier():
    req = SimpleNamespace(id=uuid.uuid4(), category="Personal Financials", status="requested")
    intake = SimpleNamespace(id=uuid.uuid4(), bucket=SimpleNamespace(requested_documents=[req]))
    stored = SimpleNamespace(id=uuid.uuid4(), uploaded_by_user_id=None)
    payload = SimpleNamespace(acknowledgment=True, owner_full_name="Ana Lopez", statement_date="2026-09-01", assets=[], liabilities=[])
    with (
        patch("app.services.dealer_forms_pdf.render_pfs_pdf", lambda **kw: b"%PDF"),
        patch.object(intake_router, "_store_drafted_form_pdf", AsyncMock(return_value=stored)),
        patch.object(intake_router, "_persist_pfs_statement", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        result = await intake_router._submit_pfs_form(_db(), intake, payload, SimpleNamespace(), actor_name="Ana Lopez", actor_email="a@x.com")
    assert result is stored
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["intake_id"] == intake.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("document.received", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Personal financial statement was submitted"
    assert kwargs["target_type"] == "file" and kwargs["target_id"] == stored.id


async def test_a_submitted_debt_schedule_is_received_at_the_client_tier():
    req = SimpleNamespace(id=uuid.uuid4(), category="Debts", status="requested")
    intake = SimpleNamespace(id=uuid.uuid4(), bucket=SimpleNamespace(requested_documents=[req]))
    stored = SimpleNamespace(id=uuid.uuid4(), uploaded_by_user_id=None)
    payload = SimpleNamespace(acknowledgment=True, business_name="Acme LLC", debts=[])
    with (
        patch("app.services.dealer_forms_pdf.render_debt_schedule_pdf", lambda **kw: b"%PDF"),
        patch.object(intake_router, "_store_drafted_form_pdf", AsyncMock(return_value=stored)),
        patch.object(intake_router, "_persist_client_debt_rows", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        result = await intake_router._submit_debt_schedule_form(_db(), intake, payload, SimpleNamespace(), actor_name="Ana Lopez", actor_email="a@x.com")
    assert result is stored
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert (kwargs["kind"], kwargs["visibility"]) == ("document.received", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Debt schedule was submitted"


async def _outcome(previous: str, new: str):
    intake = SimpleNamespace(id=uuid.uuid4(), bucket_id=uuid.uuid4(), outcome_status=previous)
    user = SimpleNamespace(id=uuid.uuid4(), name="Desk", email="desk@example.com")
    db = _db()
    with (
        patch.object(intake_router, "_require_super_admin", lambda u: None),
        patch.object(intake_router, "_load_admin_dealer_lead", AsyncMock(return_value=intake)),
        patch.object(intake_router, "_log", AsyncMock()),
        patch.object(intake_router, "_response", AsyncMock(return_value="ok")),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited() if previous != new else None
        out = await intake_router.update_lead_outcome_status(
            intake.id, SimpleNamespace(outcome_status=new), SimpleNamespace(), user, db
        )
    assert out == "ok" and intake.outcome_status == new
    return emit, user, intake


async def test_closing_denying_and_reopening_a_file_are_told_to_the_client_in_plain_words():
    for previous, new, title in (("submitted", "closed", "File closed"), ("submitted", "denied", "File denied"), ("denied", "submitted", "File reopened")):
        emit, user, intake = await _outcome(previous, new)
        emit.assert_awaited_once()
        kwargs = emit.await_args.kwargs
        assert kwargs["intake_id"] == intake.id
        assert (kwargs["kind"], kwargs["visibility"]) == ("status.changed", file_events.VISIBILITY_CLIENT)
        assert kwargs["title"] == title and kwargs["actor"] is user
        assert kwargs["meta"] == {"from": previous, "to": new}


async def test_setting_the_same_outcome_again_adds_nothing_to_the_timeline():
    emit, _, _ = await _outcome("closed", "closed")
    emit.assert_not_awaited()


# ── behaviour: bucket_ai ────────────────────────────────────────────────────


async def _human(audience: str, **kw):
    bucket = SimpleNamespace(id=uuid.uuid4())
    with (
        patch.object(ai, "log_bucket_ai_activity", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        row = await ai.create_human_message(_db(), bucket=bucket, audience=audience, message="the body", actor_name=kw.pop("actor_name", "Ana Lopez"), **kw)
    return emit, bucket, row


async def test_a_clients_message_in_their_thread_is_a_message_from_them():
    emit, bucket, row = await _human("uploader", sender_kind="client")
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["bucket_id"] == bucket.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("message.sent", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Message from Ana Lopez"
    assert kwargs["actor"] is None and kwargs["actor_label"] == "Ana Lopez"
    assert kwargs["target_type"] == "bucket_ai_message" and kwargs["target_id"] == row.id
    assert "the body" not in str(kwargs)


async def test_an_operators_reply_in_the_clients_thread_is_a_reply_from_the_desk():
    user = SimpleNamespace(id=uuid.uuid4(), name="Jane Doe", email="jane@example.com")
    emit, _, _ = await _human("uploader", actor_name="Underwriter — Jane Doe", user=user)
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["title"] == "Reply from the desk" and kwargs["actor"] is user
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT


async def test_the_private_admin_thread_is_not_the_clients_timeline():
    emit, _, _ = await _human("admin", user=SimpleNamespace(id=uuid.uuid4(), name="Jane", email="j@x.com"))
    emit.assert_not_awaited()
