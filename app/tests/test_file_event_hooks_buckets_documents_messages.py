"""File-timeline hooks in the bucket, document, message, workspace-chat and
unified-communications routers.

Runs without a database or a model call. Source pins keep every hook where
it was placed, at the tier it was given, before the site's commit, and with
no body in what it records. The behavioural tests run the handlers against
fakes and check what `file_events.emit` was asked to record — what happened,
never what was said — and which seats the legacy notice at the same site had
already reached, so nobody is told twice.
"""

from __future__ import annotations

import inspect
import re
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.enums import DealChatMode, DocStatus, MessageFrom, Role
from app.routers import buckets as buckets_router
from app.routers import communications as comms_router
from app.routers import documents as documents_router
from app.routers import loan_workspace as workspace_router
from app.routers import messages as messages_router
from app.schemas.bucket import BucketUploadComplete
from app.schemas.communication import UnifiedCommunicationCompose
from app.schemas.document import DocumentRequest, DocumentUploadComplete
from app.schemas.loan_workspace import ChatSendRequest
from app.schemas.message import MessageCreate
from app.services import file_events
from app.services import merchant_processing as mp

BODY = "The lease renews in March; the cousin's guarantee is attached."


def _user(role=Role.LOAN_EXEC, name="Desk"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name, email=f"{name.lower().replace(' ', '.')}@example.com")


def _said(emit: AsyncMock) -> str:
    """Everything the timeline rows would carry: title, body, label and meta."""
    return " | ".join(
        f"{c.kwargs['title']} {c.kwargs.get('body')} {c.kwargs.get('actor_label')} {c.kwargs.get('meta')}" for c in emit.await_args_list
    )


def _ordered_emit(db: SimpleNamespace) -> AsyncMock:
    """An emit fake whose commit partner records the order, so a test can
    show the line lands on the timeline before the request commits."""
    order: list[str] = []
    emit = AsyncMock(side_effect=lambda *a, **k: order.append("emit"))
    db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
    emit.order = order  # type: ignore[attr-defined]
    return emit


# ── source pins ─────────────────────────────────────────────────────────────


def _emit_calls(src: str) -> list[str]:
    """The text of each `file_events.emit(...)` call in a function's source."""
    return re.findall(r"file_events\.emit\((.*?)\n\s*\)\n", src, flags=re.DOTALL)


@pytest.mark.parametrize(
    ("handler", "count", "kind", "visibilities"),
    [
        (buckets_router.request_upload_complete, 1, "document.received", ["VISIBILITY_CLIENT"]),
        (buckets_router.admin_upload_complete, 1, "document.received", ["VISIBILITY_TEAM"]),
        (documents_router.request_document, 1, "document.requested", ["VISIBILITY_CLIENT"]),
        (documents_router.upload_complete, 1, "document.received", ["VISIBILITY_CLIENT"]),
        (workspace_router.send_chat, 2, "message.sent", ["VISIBILITY_CLIENT", "VISIBILITY_TEAM"]),
        (messages_router.send_message, 1, "message.sent", ["VISIBILITY_CLIENT"]),
        (comms_router.reply_unified_communication_thread, 3, "message.sent", ["VISIBILITY_CLIENT", "VISIBILITY_TEAM", "VISIBILITY_DESK"]),
    ],
)
def test_each_hook_is_pinned_in_source(handler, count, kind, visibilities):
    src = inspect.getsource(handler)
    calls = _emit_calls(src)
    assert "file_events.emit(" in src and len(calls) == count, handler.__name__
    joined = "\n".join(calls)
    for call in calls:
        assert f'kind="{kind}"' in call, handler.__name__
        # What happened, never what was said.
        for leak in ("body=", "payload.body", "content=", "BODY"):
            assert leak not in call, (handler.__name__, leak)
    for visibility in visibilities:
        assert f"file_events.{visibility}" in joined, (handler.__name__, visibility)
    for other in {"VISIBILITY_CLIENT", "VISIBILITY_TEAM", "VISIBILITY_DESK"} - set(visibilities):
        assert f"file_events.{other}" not in joined, (handler.__name__, other)


def test_the_bucket_upload_hooks_skip_the_offer_sheet_and_sit_before_the_commit():
    for handler in (buckets_router.request_upload_complete, buckets_router.admin_upload_complete):
        src = inspect.getsource(handler)
        assert "if not is_offer_document(file):" in src, handler.__name__
        assert src.index("file_events.emit(") < src.index("await db.commit()\n    await db.refresh(file)"), handler.__name__
        assert "already_notified=await _bucket_upload_notice_reached(" in src, handler.__name__
    # The room upload has no signed-in actor; the desk upload has one.
    assert "actor=None" in _emit_calls(inspect.getsource(buckets_router.request_upload_complete))[0]
    assert "actor=user" in _emit_calls(inspect.getsource(buckets_router.admin_upload_complete))[0]


def test_the_document_hooks_follow_the_legacy_notice_and_fire_once_per_receipt():
    src = inspect.getsource(documents_router.upload_complete)
    assert src.index("notify_document_uploaded(") < src.index("file_events.emit(")
    # Re-posting an already received document is a no-op for the timeline too.
    assert "if not already_received:\n        # File timeline" in src
    assert "already_notified=await _document_upload_notice_reached(" in src
    assert 'title=f"We asked for {doc.name}"' in inspect.getsource(documents_router.request_document)


def test_the_message_hook_follows_the_legacy_notice_and_the_ai_reply_is_never_hooked():
    src = inspect.getsource(messages_router.send_message)
    assert src.index("notify_message_sent(") < src.index("file_events.emit(") < src.index("channel.broadcast(")
    assert "already_notified=await _message_notice_reached(" in src
    assert "file_events.emit(" not in inspect.getsource(workspace_router._generate_ai_reply)
    assert "file_events.emit(" not in inspect.getsource(workspace_router._generate_ai_followup)


def test_the_reply_hooks_leave_the_intake_client_branch_and_the_outside_channels_alone():
    src = inspect.getsource(comms_router.reply_unified_communication_thread)
    # The client branch hands off to create_human_message, which emits itself.
    client_branch = src[src.index('if channel == "client":') : src.index("else:\n                await create_chat_reply(")]
    assert "create_human_message(" in client_branch and "file_events.emit(" not in client_branch
    for branch in ('elif parts[0] == "rep":', 'elif parts[0] == "sms":', 'elif parts[0] == "email":'):
        tail = src[src.index(branch) :]
        next_branch = re.search(r"\n    elif parts\[0\] == |\n    return await get_unified_communication_thread", tail[1:])
        assert "file_events.emit(" not in tail[: next_branch.start() + 1], branch
    assert "file_events.emit(" not in inspect.getsource(comms_router.compose_new_message)
    for call in _emit_calls(src):
        assert src.index(call) < src.index("await db.commit()", src.index(call))


# ── buckets: the PIN room upload and the desk-side upload ───────────────────


def _bucket_upload(*, source_detail=None, upload_link_id=None):
    bucket = SimpleNamespace(id=uuid.uuid4(), name="Acme LLC", created_by_id=uuid.uuid4())
    file = SimpleNamespace(
        id=uuid.uuid4(), bucket_id=bucket.id, upload_link_id=upload_link_id, deleted_at=None, status="pending",
        requested_document_id=None, file_name="Bank statement.pdf", uploaded_by_name="Ana Lopez",
        uploaded_by_email="ana@example.com", source_detail=source_detail, source_kind="client_upload",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=file), add=lambda row: None, refresh=AsyncMock())
    emit = _ordered_emit(db)
    admin = SimpleNamespace(id=uuid.uuid4())
    return bucket, file, db, emit, admin


def _bucket_patches(stack: ExitStack, admin, emit: AsyncMock) -> None:
    """The fakes both upload-complete handlers share: the audit log, the
    legacy upload notice and its recipient lookup, the post-upload work, and
    the timeline writer itself."""
    for patcher in (
        patch.object(buckets_router, "_log", AsyncMock()),
        patch("app.services.notifications.notify_bucket_file_uploaded", AsyncMock()),
        patch("app.services.notifications.users_with_roles", AsyncMock(return_value=[admin])),
        patch("app.services.bucket_evidence.reconcile_uploaded_file", AsyncMock()),
        patch("app.services.bucket_ai.enqueue_file_analysis", AsyncMock()),
        patch.object(file_events, "emit", emit),
    ):
        stack.enter_context(patcher)


async def _room_upload(source_detail=None):
    bucket, file, db, emit, admin = _bucket_upload(source_detail=source_detail, upload_link_id=uuid.uuid4())
    link = SimpleNamespace(
        id=file.upload_link_id, bucket_id=bucket.id, bucket=bucket, recipient_name="Ana", recipient_email="ana@example.com",
        allow_notes=False, completed_at=None,
    )
    background = SimpleNamespace(add_task=lambda *a, **k: None)
    with ExitStack() as stack:
        stack.enter_context(patch.object(buckets_router, "_load_upload_link_or_404", AsyncMock(return_value=link)))
        _bucket_patches(stack, admin, emit)
        out = await buckets_router.request_upload_complete("tok", BucketUploadComplete(file_id=file.id), background, SimpleNamespace(), db)
    return out, bucket, file, emit, admin


async def test_a_room_upload_is_received_at_the_client_tier_with_no_actor_before_the_commit():
    out, bucket, file, emit, admin = await _room_upload()
    assert out is file and file.status == "uploaded"
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["bucket_id"] == bucket.id and kwargs["kind"] == "document.received"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT
    assert kwargs["title"] == "Bank statement.pdf was received"
    assert kwargs["actor"] is None and kwargs["actor_label"] == "Ana Lopez"
    assert kwargs["target_type"] == "bucket_file" and kwargs["target_id"] == file.id
    # The upload notice already reached the bucket's creator and every super admin.
    assert kwargs["already_notified"] == {bucket.created_by_id, admin.id}
    assert emit.order == ["emit", "commit"]


async def test_the_partners_offer_sheet_never_reaches_the_timeline_from_the_room():
    out, _bucket, file, emit, _admin = await _room_upload(source_detail=mp.OFFER_SOURCE_DETAIL)
    assert out is file and file.status == "uploaded"
    emit.assert_not_awaited()
    assert emit.order == ["commit"]


async def test_a_desk_side_upload_is_received_at_the_team_tier_with_the_uploader_as_actor():
    bucket, file, db, emit, admin = _bucket_upload()
    user = _user(Role.SUPER_ADMIN)
    background = SimpleNamespace(add_task=lambda *a, **k: None)
    with ExitStack() as stack:
        stack.enter_context(patch.object(buckets_router, "_load_bucket_or_404", AsyncMock(return_value=bucket)))
        _bucket_patches(stack, admin, emit)
        out = await buckets_router.admin_upload_complete(bucket.id, BucketUploadComplete(file_id=file.id), background, SimpleNamespace(), user, db)
    assert out is file and file.status == "uploaded"
    kwargs = emit.await_args.kwargs
    assert kwargs["visibility"] == file_events.VISIBILITY_TEAM and kwargs["actor"] is user
    assert kwargs["bucket_id"] == bucket.id and kwargs["title"] == "Bank statement.pdf was received"
    assert kwargs["already_notified"] == {bucket.created_by_id, admin.id}
    assert emit.order == ["emit", "commit"]


async def test_the_bucket_notice_recipients_never_fail_the_upload():
    bucket = SimpleNamespace(id=uuid.uuid4(), created_by_id=uuid.uuid4())
    with patch("app.services.notifications.users_with_roles", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await buckets_router._bucket_upload_notice_reached(SimpleNamespace(), bucket) == set()


# ── documents: the request and the receipt ──────────────────────────────────


def _loan(**kw):
    base = dict(id=uuid.uuid4(), deal_id="L-1001", client_id=uuid.uuid4(), source_deal_id=None, assigned_owner_id=None, broker_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _refresh_with_id(row):
    if getattr(row, "id", None) is None:
        row.id = uuid.uuid4()


async def test_requesting_a_document_tells_the_client_what_was_asked_for():
    loan = _loan()
    user = _user(Role.LOAN_EXEC)
    db = SimpleNamespace(get=AsyncMock(return_value=loan), add=lambda row: None, flush=AsyncMock(), refresh=AsyncMock(side_effect=_refresh_with_id))
    emit = AsyncMock()
    with patch.object(documents_router, "_can_access_loan", AsyncMock(return_value=True)), \
         patch.object(documents_router, "vector_log", AsyncMock()), \
         patch.object(documents_router.calendar_emitter, "emit_for_document_request", AsyncMock()), \
         patch.object(documents_router, "mark_loan_dirty", AsyncMock()), \
         patch.object(documents_router, "DocumentRead", SimpleNamespace(model_validate=lambda row: row)), \
         patch.object(file_events, "emit", emit):
        doc = await documents_router.request_document(DocumentRequest(loan_id=loan.id, name="Bank statements (2 mo)", category="financials"), user, db)
    assert doc.status == DocStatus.REQUESTED
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["loan_id"] == loan.id and kwargs["kind"] == "document.requested"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT
    assert kwargs["title"] == "We asked for Bank statements (2 mo)"
    assert kwargs["actor"] is user and kwargs["target_type"] == "document" and kwargs["target_id"] == doc.id


async def _receive(doc_status, *, source_deal_id):
    loan = _loan(source_deal_id=source_deal_id)
    doc = SimpleNamespace(
        id=uuid.uuid4(), loan_id=loan.id, status=doc_status, received_on=None, s3_key="k", checklist_key=None,
        is_other=False, name="Bank statements", scan_dirty=False, ai_scan_status=None,
    )
    user = _user(Role.CLIENT, "Ana Lopez")
    agent, desk = uuid.uuid4(), uuid.uuid4()
    db = SimpleNamespace(get=AsyncMock(side_effect=[doc, loan]), add=lambda row: None, flush=AsyncMock(), refresh=AsyncMock())
    emit = AsyncMock()
    with patch.object(documents_router, "_can_access_loan", AsyncMock(return_value=True)), \
         patch.object(documents_router, "mark_loan_dirty", AsyncMock()), \
         patch.object(documents_router, "DocumentRead", SimpleNamespace(model_validate=lambda row: row)), \
         patch("app.services.notifications.notify_document_uploaded", AsyncMock()), \
         patch("app.services.notifications.loan_agent_user_ids", AsyncMock(return_value={agent})), \
         patch("app.services.notifications.users_with_roles", AsyncMock(return_value=[SimpleNamespace(id=desk)])), \
         patch.object(file_events, "emit", emit):
        out = await documents_router.upload_complete(DocumentUploadComplete(document_id=doc.id), user, db)
    assert out is doc and doc.status == DocStatus.RECEIVED
    return emit, loan, doc, user, agent, desk


async def test_a_received_document_is_told_to_the_client_skipping_the_seats_the_upload_notice_reached():
    emit, loan, doc, user, agent, desk = await _receive(DocStatus.REQUESTED, source_deal_id=uuid.uuid4())
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["loan_id"] == loan.id and kwargs["kind"] == "document.received"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT and kwargs["title"] == "Bank statements was received"
    assert kwargs["actor"] is user and kwargs["target_type"] == "document" and kwargs["target_id"] == doc.id
    assert kwargs["already_notified"] == {agent, desk}


async def test_a_loan_without_a_deal_keeps_the_desk_out_of_the_upload_notice_and_a_repeat_is_quiet():
    emit, _loan_, _doc, _user_, agent, _desk = await _receive(DocStatus.REQUESTED, source_deal_id=None)
    assert emit.await_args.kwargs["already_notified"] == {agent}
    emit, *_ = await _receive(DocStatus.RECEIVED, source_deal_id=uuid.uuid4())
    emit.assert_not_awaited()


# ── messages: the loan thread ───────────────────────────────────────────────


async def _send_message(user, from_role, *, client_user_id):
    loan = _loan()
    client = SimpleNamespace(id=loan.client_id, user_id=client_user_id, name="Acme LLC")
    agent, desk = uuid.uuid4(), uuid.uuid4()

    def get(model, key):
        return loan if model is messages_router.Loan else client

    def refresh(row):
        _refresh_with_id(row)
        row.sent_at = datetime.now(UTC)

    db = SimpleNamespace(get=AsyncMock(side_effect=get), execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: loan.id)), add=lambda row: None, flush=AsyncMock(), refresh=AsyncMock(side_effect=refresh))
    emit = AsyncMock()
    read = SimpleNamespace(model_validate=lambda row: SimpleNamespace(row=row, model_dump=lambda mode=None: {"id": str(row.id)}))
    with patch.object(messages_router, "scope_loan_query", lambda user, stmt: stmt), \
         patch.object(messages_router, "MessageRead", read), \
         patch.object(messages_router.channel, "broadcast", AsyncMock()) as broadcast, \
         patch("app.services.notifications.notify_message_sent", AsyncMock()), \
         patch("app.services.notifications.loan_agent_user_ids", AsyncMock(return_value={agent})), \
         patch("app.services.notifications.users_with_roles", AsyncMock(return_value=[SimpleNamespace(id=desk)])), \
         patch.object(file_events, "emit", emit):
        out = await messages_router.send_message(MessageCreate(loan_id=loan.id, body=BODY, from_role=from_role), user, db)
    broadcast.assert_awaited_once()
    return emit, loan, out.row, agent, desk


async def test_a_clients_message_is_told_as_a_message_from_them_and_skips_the_agents_and_the_desk():
    user = _user(Role.CLIENT, "Ana Lopez")
    emit, loan, msg, agent, desk = await _send_message(user, MessageFrom.BROKER, client_user_id=user.id)
    assert msg.from_role == MessageFrom.CLIENT
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["loan_id"] == loan.id and kwargs["kind"] == "message.sent"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT and kwargs["title"] == "Message from Ana Lopez"
    assert kwargs["actor"] is user and kwargs["target_type"] == "message" and kwargs["target_id"] == msg.id
    assert kwargs["already_notified"] == {agent, desk}
    assert "lease" not in _said(emit) and "cousin" not in _said(emit)


async def test_an_agents_message_is_told_as_from_the_agent_and_skips_the_client_the_notice_reached():
    client_user = uuid.uuid4()
    emit, _loan_, _msg, _agent, _desk = await _send_message(_user(Role.BROKER, "Ben"), MessageFrom.BROKER, client_user_id=client_user)
    assert emit.await_args.kwargs["title"] == "Message from your agent"
    assert emit.await_args.kwargs["already_notified"] == {client_user}
    emit, *_ = await _send_message(_user(Role.LOAN_EXEC, "Desk"), MessageFrom.BROKER, client_user_id=None)
    assert emit.await_args.kwargs["title"] == "Message from the desk"
    assert emit.await_args.kwargs["already_notified"] == set()


async def test_the_message_notice_recipients_never_fail_the_send():
    with patch("app.services.notifications.loan_agent_user_ids", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await messages_router._message_notice_reached(SimpleNamespace(), _loan(), "client") == set()


# ── the workspace chat ──────────────────────────────────────────────────────


async def _send_chat(user, mode, *, paused=False, client_user_id=None):
    loan = _loan(ai_paused_until=None)
    client = SimpleNamespace(id=loan.client_id, user_id=client_user_id)
    db = SimpleNamespace(
        add=lambda row: None, flush=AsyncMock(), refresh=AsyncMock(side_effect=_refresh_with_id),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: client)),
    )
    emit = AsyncMock()
    paused_until = datetime(2026, 9, 10, 15, tzinfo=UTC)
    with patch.object(workspace_router, "_load_loan", AsyncMock(return_value=loan)), \
         patch.object(workspace_router.engagement, "is_paused", lambda loan: paused), \
         patch.object(workspace_router.engagement, "pause", lambda loan: paused_until), \
         patch.object(workspace_router, "_notify_client", lambda **kw: None) as push, \
         patch.object(workspace_router, "_schedule_resume_followup", lambda **kw: None), \
         patch.object(workspace_router, "_generate_ai_reply", AsyncMock(return_value=None)) as ai, \
         patch.object(workspace_router, "serialize_chat_one", AsyncMock(return_value="row")), \
         patch.object(workspace_router, "ChatSendResponse", lambda **kw: SimpleNamespace(**kw)), \
         patch.object(file_events, "emit", emit):
        out = await workspace_router.send_chat(loan.id, ChatSendRequest(body=BODY, mode=mode), user, db)
    assert out.kind == "message"
    return emit, loan, ai, push


async def test_a_clients_chat_message_is_a_message_from_them_at_the_client_tier():
    user = _user(Role.CLIENT, "Ana Lopez")
    emit, loan, ai, _push = await _send_chat(user, DealChatMode.CHAT, paused=True)
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["loan_id"] == loan.id and kwargs["kind"] == "message.sent"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT and kwargs["title"] == "Message from Ana Lopez"
    assert kwargs["actor"] is user and kwargs["target_type"] == "loan_chat_message" and kwargs["target_id"] is not None
    assert "already_notified" not in kwargs
    ai.assert_not_awaited()
    assert "lease" not in _said(emit)


async def test_an_agents_question_to_the_desk_is_a_team_line_and_the_ai_reply_adds_nothing():
    user = _user(Role.BROKER, "Ben")
    emit, _loan_, ai, _push = await _send_chat(user, DealChatMode.BROKER_QUESTION)
    ai.assert_awaited_once()
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["visibility"] == file_events.VISIBILITY_TEAM and kwargs["title"] == "Agent question to the desk"
    assert kwargs["actor"] is user


async def test_a_takeover_reply_is_told_to_the_client_once_the_push_already_reached_them():
    client_user = uuid.uuid4()
    emit, _loan_, ai, _push = await _send_chat(_user(Role.SUPER_ADMIN, "Desk"), DealChatMode.CHAT, client_user_id=client_user)
    ai.assert_not_awaited()
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT and kwargs["title"] == "Reply from the desk"
    assert kwargs["already_notified"] == {client_user}
    emit, *_ = await _send_chat(_user(Role.BROKER, "Ben"), DealChatMode.LIVE_CHAT, client_user_id=None)
    assert emit.await_args.kwargs["title"] == "Reply from your agent"
    assert emit.await_args.kwargs["already_notified"] == {None}
    assert "lease" not in _said(emit)


# ── the unified inbox reply ─────────────────────────────────────────────────


async def _reply(thread_id, user, *, audit_client=False):
    intake = SimpleNamespace(id=uuid.uuid4(), bucket_id=uuid.uuid4(), bucket_upload_link_id=None, last_message_at=None, preferred_language=None)
    bucket = SimpleNamespace(id=intake.bucket_id)

    def get(model, key):
        return intake if model is comms_router.PublicUnderwritingIntake else bucket

    db = SimpleNamespace(add=lambda row: None, get=AsyncMock(side_effect=get))
    emit = _ordered_emit(db)
    with patch.object(comms_router, "_thread_summary", AsyncMock(return_value=SimpleNamespace(can_reply=True))), \
         patch.object(comms_router, "get_unified_communication_thread", AsyncMock(return_value="detail")), \
         patch.object(comms_router, "is_audit_client", lambda user: audit_client), \
         patch("app.services.bucket_ai.create_human_message", AsyncMock()) as human, \
         patch("app.services.bucket_ai.create_chat_reply", AsyncMock()), \
         patch.object(file_events, "emit", emit):
        out = await comms_router.reply_unified_communication_thread(thread_id, UnifiedCommunicationCompose(body=BODY), SimpleNamespace(), user, db)
    assert out == "detail"
    return emit, intake, human


async def test_a_reply_on_the_loan_thread_is_a_client_line_before_the_commit():
    loan_id = uuid.uuid4()
    emit, _intake, _human = await _reply(f"loan:{loan_id}", _user(Role.BROKER, "Ben"))
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["loan_id"] == loan_id and kwargs["kind"] == "message.sent"
    assert kwargs["visibility"] == file_events.VISIBILITY_CLIENT and kwargs["title"] == "Reply from your agent"
    assert kwargs["target_type"] == "communication_thread" and kwargs["target_id"] == f"loan:{loan_id}"
    assert emit.order == ["emit", "commit"]
    emit, *_ = await _reply(f"loan:{loan_id}", _user(Role.CLIENT, "Ana Lopez"))
    assert emit.await_args.kwargs["title"] == "Message from Ana Lopez"
    emit, *_ = await _reply(f"loan:{loan_id}", _user(Role.LOAN_EXEC, "Desk"))
    assert emit.await_args.kwargs["title"] == "Reply from the desk"
    assert "lease" not in _said(emit)


async def test_a_partner_note_is_the_teams_and_an_internal_note_stays_with_the_desk():
    user = _user(Role.LOAN_EXEC, "Desk")
    emit, intake, _human = await _reply(f"intake:{uuid.uuid4()}:partner", user)
    kwargs = emit.await_args.kwargs
    assert kwargs["intake_id"] == intake.id and kwargs["visibility"] == file_events.VISIBILITY_TEAM
    assert kwargs["title"] == "Note to the partner channel" and kwargs["actor"] is user
    assert intake.last_message_at is not None and emit.order == ["emit", "commit"]
    emit, _intake, _human = await _reply(f"intake:{uuid.uuid4()}:internal", user)
    assert emit.await_args.kwargs["visibility"] == file_events.VISIBILITY_DESK
    assert emit.await_args.kwargs["title"] == "Internal note"
    assert "cousin" not in _said(emit)


async def test_the_intake_client_branch_leaves_the_timeline_to_the_service():
    emit, _intake, human = await _reply(f"intake:{uuid.uuid4()}:client", _user(Role.LOAN_EXEC, "Desk"))
    human.assert_awaited_once()
    emit.assert_not_awaited()
    assert emit.order == ["commit"]


async def test_a_dealer_reply_is_a_client_line_on_the_client_channel_and_a_team_line_on_the_desk_channel():
    dealer_id = uuid.uuid4()
    emit, *_ = await _reply(f"dealer:{dealer_id}:client", _user(Role.LOAN_EXEC, "Desk"))
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer_id and kwargs["visibility"] == file_events.VISIBILITY_CLIENT
    assert kwargs["title"] == "Reply to the client from Desk" and emit.order == ["emit", "commit"]
    emit, *_ = await _reply(f"dealer:{dealer_id}:desk", _user(Role.BROKER, "Ben"))
    assert emit.await_args.kwargs["visibility"] == file_events.VISIBILITY_TEAM
    assert emit.await_args.kwargs["title"] == "Desk message from Ben"
    emit, *_ = await _reply(f"dealer:{dealer_id}:client", _user(Role.DEALER, "Acme Owner"), audit_client=True)
    assert emit.await_args.kwargs["title"] == "Message from Acme Owner"
    assert "lease" not in _said(emit)
