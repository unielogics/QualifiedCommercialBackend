"""File-timeline hooks in the Capital OS / Field Desk router.

Runs without a database, a provider or a model call. Source pins keep every
hook where it was placed, at the tier it was given; the behavioural tests run
the hooked functions against fakes and check what `file_events.emit` was
asked to record — what happened, never what was said.
"""

from __future__ import annotations

import inspect
import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.dealer_os import router
from app.services import file_events
from app.services import merchant_processing as mp

# ── source pins ─────────────────────────────────────────────────────────────


def _emit_calls(src: str) -> list[str]:
    """The text of each `file_events.emit(...)` call in a function's source."""
    return re.findall(r"file_events\.emit\((.*?)\n\s*\)\n", src, flags=re.DOTALL)


@pytest.mark.parametrize(
    ("handler", "kind", "visibilities"),
    [
        (router.create_doc_request, "document.requested", ["VISIBILITY_CLIENT"]),
        (router._auto_fulfill_doc_request, "document.received", ["VISIBILITY_CLIENT"]),
        (router.create_message, "message.sent", ["VISIBILITY_CLIENT", "VISIBILITY_TEAM", "VISIBILITY_DESK"]),
        (router.create_rep_inbox_thread, "message.sent", ["VISIBILITY_CLIENT"]),
        (router._send_rep_inbox_message, "message.sent", ["VISIBILITY_CLIENT"]),
        (router._append_rep_inbox_message, "message.sent", ["VISIBILITY_CLIENT"]),
        (router.patch_submission_human_review, "status.changed", ["VISIBILITY_TEAM"]),
        (router.patch_application_finalization, "status.changed", ["VISIBILITY_TEAM"]),
    ],
)
def test_each_hook_is_pinned_in_source(handler, kind, visibilities):
    src = inspect.getsource(handler)
    calls = _emit_calls(src)
    assert "file_events.emit(" in src and len(calls) == 1
    assert f'kind="{kind}"' in calls[0]
    for visibility in visibilities:
        assert f"file_events.{visibility}" in calls[0]
    # Only the tiers named for the site, nothing else.
    for other in {"VISIBILITY_CLIENT", "VISIBILITY_TEAM", "VISIBILITY_DESK"} - set(visibilities):
        assert f"file_events.{other}" not in calls[0]


def test_the_received_hook_sits_on_the_fulfilled_request_and_skips_the_offer_sheet():
    src = inspect.getsource(router._auto_fulfill_doc_request)
    assert "if not merchant_processing.is_offer_document(doc):" in src
    assert src.index('match.status = "fulfilled"') < src.index("file_events.emit(") < src.index("return match")
    # The ingest sites hook nothing themselves: the archive loop, the single
    # upload and the approval all reach the timeline through the one helper,
    # so an archive of forty statements is forty fulfilments at most, never
    # forty lines for one request.
    callers = [
        fn
        for fn in vars(router).values()
        if inspect.iscoroutinefunction(fn)
        and getattr(fn, "__module__", None) == router.__name__
        and fn is not router._auto_fulfill_doc_request
        and "_auto_fulfill_doc_request(" in inspect.getsource(fn)
    ]
    assert len(callers) == 3
    for fn in callers:
        assert "file_events.emit(" not in inspect.getsource(fn)


def test_the_request_hook_sits_after_the_room_mirror_and_before_the_commit():
    src = inspect.getsource(router.create_doc_request)
    assert src.index("client_room.request_document(") < src.index("file_events.emit(") < src.index("await db.commit()")
    (call,) = _emit_calls(src)
    assert 'title=f"We asked for {req.title}"' in call and "req.note" not in call


def test_the_message_hook_names_the_author_and_never_the_body():
    src = inspect.getsource(router.create_message)
    (call,) = _emit_calls(src)
    assert "payload.body" not in call and "message.body" not in call
    assert "already_notified={dealer.owner_user_id} if is_audit_client(user) else ()" in call
    assert src.index("_mirror_file_message_to_rep_inbox(") < src.index("file_events.emit(") < src.index("await db.commit()")
    for title in (
        'f"Message from {author_name}"',
        'f"Reply to the client from {author_name}"',
        'f"Desk message from {author_name}"',
        'f"Internal note from {author_name}"',
    ):
        assert title in src


def test_the_inbox_hooks_fire_once_per_conversation_and_only_on_a_file():
    src = inspect.getsource(router.create_rep_inbox_thread)
    assert src.index("for channel in channels:") < src.index("if dealer is not None:") < src.index("file_events.emit(") < src.index("await db.commit()")
    src = inspect.getsource(router._send_rep_inbox_message)
    assert src.index("if dealer is not None:") < src.index("file_events.emit(") < src.index("await db.commit()")
    src = inspect.getsource(router._append_rep_inbox_message)
    assert 'if direction == "inbound" and thread.dealer_id is not None and provider != "file_message":' in src
    assert src.index("notify_inbound_communication(") < src.index("file_events.emit(")
    (call,) = _emit_calls(src)
    assert "already_notified={thread.owner_user_id}" in call and "body" not in call


def test_the_status_hooks_fire_only_on_a_change_and_name_the_state_in_plain_words():
    src = inspect.getsource(router.patch_submission_human_review)
    assert 'if row.human_review_status != before["status"]:' in src
    assert "human_review_note" not in _emit_calls(src)[0]
    src = inspect.getsource(router.patch_application_finalization)
    assert 'if dealer.status != before["status"]:' in src
    assert router._HUMAN_REVIEW_EVENT_TITLES["fundable"] == "Desk review: approved"
    assert router._FINALIZATION_EVENT_TITLES == {
        "active": "Active",
        "decision_ready": "Decision ready",
        "forms_out": "Forms out",
        "signed": "Signed",
        "complete": "Complete",
        "declined": "Declined",
    }


# ── behaviour ───────────────────────────────────────────────────────────────


def _db(**extra):
    base = dict(add=lambda row: None, flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    base.update(extra)
    return SimpleNamespace(**base)


def _user(name="Ana Lopez", email="ana@example.com"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, email=email)


def _result(*, scalars_all=(), scalar_one=None, first=None):
    """A stand-in for an `execute` result answering the shapes the router asks for."""
    scalars = SimpleNamespace(all=lambda: list(scalars_all), first=lambda: first)
    return SimpleNamespace(scalars=lambda: scalars, scalar_one_or_none=lambda: scalar_one, first=lambda: first)


# document.received — _auto_fulfill_doc_request


def _doc(**overrides):
    base = dict(id=uuid.uuid4(), dealer_id=uuid.uuid4(), kind="statement", status="extracted", account_id=None, source_detail=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(title="March bank statement"):
    return SimpleNamespace(id=uuid.uuid4(), title=title, account_id=None, status="open", fulfilled_document_id=None)


async def _fulfill(doc, *, open_requests):
    db = _db(execute=AsyncMock(return_value=_result(scalars_all=open_requests)))
    user = _user()
    with (
        patch.object(router, "log_action", AsyncMock()) as log,
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        match = await router._auto_fulfill_doc_request(db, doc.dealer_id, doc, user)
    return match, emit, log, user


async def test_a_fulfilled_request_is_received_once_at_the_client_tier():
    doc, req = _doc(), _request()
    match, emit, log, user = await _fulfill(doc, open_requests=[req])
    assert match is req and req.status == "fulfilled" and req.fulfilled_document_id == doc.id
    log.assert_awaited_once()
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == doc.dealer_id
    assert (kwargs["kind"], kwargs["visibility"]) == ("document.received", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "March bank statement was received"
    assert kwargs["actor"] is user
    assert kwargs["target_type"] == "doc_request" and kwargs["target_id"] == req.id


async def test_a_document_that_fulfils_nothing_and_the_partners_offer_sheet_are_quiet():
    match, emit, _, _ = await _fulfill(_doc(), open_requests=[])
    assert match is None
    emit.assert_not_awaited()
    req = _request("Offer")
    match, emit, _, _ = await _fulfill(_doc(source_detail=mp.OFFER_SOURCE_DETAIL), open_requests=[req])
    assert match is req and req.status == "fulfilled"  # the write itself is untouched
    emit.assert_not_awaited()


# document.requested — create_doc_request


async def test_asking_for_a_document_tells_the_client_before_the_commit():
    dealer = SimpleNamespace(id=uuid.uuid4(), name="Acme LLC", email="owner@example.com", phone=None)
    user = _user()
    payload = SimpleNamespace(title=" March bank statement ", kind="statement", account_id=None, due_on=None, note="the note", notify="email")
    db = _db()
    with (
        patch.object(router, "require_team_or_rep", lambda u: None),
        patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)),
        patch.object(router, "_require_training_live_action", AsyncMock()),
        patch.object(router, "log_action", AsyncMock()),
        patch.object(router.client_room, "request_document", AsyncMock()),
        patch.object(router.client_room, "ensure_room", AsyncMock(return_value=SimpleNamespace(url="https://room.example"))),
        patch.object(router, "_notify_client_request", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once()
        req = await router.create_doc_request(dealer.id, payload, SimpleNamespace(), user, db)
    assert req.title == "March bank statement"
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("document.requested", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "We asked for March bank statement" and kwargs["actor"] is user
    assert kwargs["target_type"] == "doc_request"
    assert "the note" not in str(kwargs)


# message.sent — create_message


async def _post_message(*, client: bool, channel: str | None, owner_user_id=None):
    dealer = SimpleNamespace(id=uuid.uuid4(), owner_user_id=owner_user_id, name="Acme LLC")
    user = _user()
    payload = SimpleNamespace(body="the body", channel=channel, internal=None, image_ids=[])
    db = _db()
    read = SimpleNamespace(model_validate=lambda m: SimpleNamespace(model_dump=lambda **kw: {"id": "x"}))
    with (
        patch.object(router, "require_team_or_dealer_or_rep", lambda u: None),
        patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)),
        patch.object(router, "is_audit_client", lambda u: client),
        patch.object(router, "is_rep", lambda u: False),
        patch.object(router.inline_images, "attach", AsyncMock(return_value=[])),
        patch.object(router, "_mirror_file_message_to_rep_inbox", AsyncMock()) as mirror,
        patch.object(router, "MessageRead", read),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once()
        out = await router.create_message(dealer.id, payload, user, db)
    assert out["images"] == []
    mirror.assert_awaited_once()
    return emit.await_args.kwargs, user, dealer


async def test_a_clients_message_is_told_at_the_client_tier_and_the_owning_rep_is_not_told_twice():
    owner = uuid.uuid4()
    # A client may not pick a channel: whatever the payload says, it is the client thread.
    kwargs, user, dealer = await _post_message(client=True, channel="note", owner_user_id=owner)
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("message.sent", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Message from Ana Lopez" and kwargs["actor"] is user
    assert kwargs["already_notified"] == {owner}
    assert kwargs["target_type"] == "dealer_message"
    assert "the body" not in str(kwargs)


async def test_staff_messages_are_tiered_by_channel():
    for channel, visibility, title in (
        ("client", file_events.VISIBILITY_CLIENT, "Reply to the client from Ana Lopez"),
        ("desk", file_events.VISIBILITY_TEAM, "Desk message from Ana Lopez"),
        ("note", file_events.VISIBILITY_DESK, "Internal note from Ana Lopez"),
    ):
        kwargs, _, _ = await _post_message(client=False, channel=channel)
        assert (kwargs["visibility"], kwargs["title"]) == (visibility, title)
        assert kwargs["already_notified"] == ()
        assert "the body" not in str(kwargs)


# message.sent — the rep inbox, inbound


async def _append(*, direction, dealer_id, provider=None, contact=None):
    thread = SimpleNamespace(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), contact_id=None, dealer_id=dealer_id, unread_count=0, last_message_at=None)
    db = _db()
    with (
        patch.object(router, "notify_inbound_communication", AsyncMock()) as told,
        patch("app.services.communication_events.publish_communication_event", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        msg = await router._append_rep_inbox_message(
            db, thread=thread, contact=contact, direction=direction, channel="sms", body="the body",
            provider=provider, sender="+15550001111",
        )
    return msg, thread, emit, told


async def test_a_clients_inbound_message_on_a_file_is_received_and_the_owner_is_not_told_twice():
    dealer_id = uuid.uuid4()
    contact = SimpleNamespace(id=uuid.uuid4(), full_name="Ana Lopez", company="Acme LLC", last_activity_at=None)
    msg, thread, emit, told = await _append(direction="inbound", dealer_id=dealer_id, contact=contact)
    told.assert_awaited_once()
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer_id
    assert (kwargs["kind"], kwargs["visibility"]) == ("message.sent", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Message received from Ana Lopez"
    assert kwargs["actor"] is None and kwargs["actor_label"] == "Ana Lopez"
    assert kwargs["already_notified"] == {thread.owner_user_id}
    assert kwargs["target_type"] == "inbox_message" and kwargs["target_id"] == msg.id
    assert "the body" not in str(kwargs)


async def test_outbound_unbound_and_mirrored_inbox_rows_add_nothing_here():
    for kw in (
        dict(direction="outbound", dealer_id=uuid.uuid4()),
        dict(direction="inbound", dealer_id=None),
        dict(direction="inbound", dealer_id=uuid.uuid4(), provider="file_message"),
    ):
        _, _, emit, _ = await _append(**kw)
        emit.assert_not_awaited()


# message.sent — the rep inbox, outbound


def _sent_email(**kw):
    return SimpleNamespace(ok=True, message_id="m1", detail=None)


async def _reply(*, dealer, contact):
    thread = SimpleNamespace(id=uuid.uuid4(), dealer_id=dealer.id if dealer else None, channel="email", subject="Hello")
    user = _user()
    msg = SimpleNamespace(id=uuid.uuid4())
    db = _db()
    with (
        patch.object(router, "_require_training_live_action", AsyncMock()),
        patch.object(router.ses_client, "send_email", _sent_email),
        patch.object(router, "_append_rep_inbox_message", AsyncMock(return_value=msg)) as append,
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once() if dealer else None
        out = await router._send_rep_inbox_message(
            db, thread=thread, contact=contact, dealer=dealer,
            payload=SimpleNamespace(channel=None, body="the body"), request=SimpleNamespace(), user=user,
        )
    assert out is msg
    append.assert_awaited_once()
    return emit, user, msg


async def test_a_reply_from_the_inbox_is_a_message_sent_to_the_client():
    dealer = SimpleNamespace(id=uuid.uuid4(), name="Acme LLC")
    contact = SimpleNamespace(id=uuid.uuid4(), full_name="Ana Lopez", company="Acme LLC", email="ana@example.com", phone_e164=None)
    emit, user, msg = await _reply(dealer=dealer, contact=contact)
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("message.sent", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Message sent to Ana Lopez" and kwargs["actor"] is user
    assert kwargs["target_type"] == "inbox_message" and kwargs["target_id"] == msg.id
    assert "the body" not in str(kwargs)


async def test_a_reply_on_a_thread_with_no_file_stays_off_every_timeline():
    contact = SimpleNamespace(id=uuid.uuid4(), full_name="Ana Lopez", company=None, email="ana@example.com", phone_e164=None)
    emit, _, _ = await _reply(dealer=None, contact=contact)
    emit.assert_not_awaited()


async def _compose(*, bound: bool):
    dealer = SimpleNamespace(id=uuid.uuid4(), name="Acme LLC")
    user = _user()
    contact = SimpleNamespace(
        id=uuid.uuid4(), full_name="Ana Lopez", company="Acme LLC",
        sms_opted_out_at=None, sms_transactional_consented_at="2026-09-01", sms_marketing_consented_at=None,
    )
    payload = SimpleNamespace(
        dealer_id=dealer.id if bound else None, recipient_name="Ana Lopez", company=None,
        recipient_email="ana@example.com", recipient_phone="+15550001111", channels=["email", "sms"],
        subject="Hello", body="the body", transactional_sms_consent=True, marketing_sms_consent=False,
        consent_method="rep_attested",
    )
    thread = SimpleNamespace(id=uuid.uuid4())
    sms = SimpleNamespace(ok=True, sender="+15550009999", provider="sns", provider_message_id="s1", detail=None)
    db = _db()
    with (
        patch.object(router, "require_team_or_rep", lambda u: None),
        patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)),
        patch.object(router, "_require_training_live_action", AsyncMock()),
        patch.object(router.consent_delivery, "normalize_phone", lambda p: p),
        patch.object(router.consent_delivery, "send_sms_guarded", AsyncMock(return_value=sms)),
        patch.object(router, "_ensure_rep_contact", AsyncMock(return_value=contact)),
        patch.object(router, "_capture_rep_contact_sms_consent", AsyncMock()),
        patch.object(router, "_ensure_rep_thread", AsyncMock(return_value=thread)),
        patch.object(router.ses_client, "send_email", _sent_email),
        patch.object(router, "_append_rep_inbox_message", AsyncMock(side_effect=lambda db, **kw: SimpleNamespace(id=uuid.uuid4()))),
        patch.object(router, "_thread_read", lambda t, c: t),
        patch.object(router, "log_action", AsyncMock()),
        patch.object(router, "RepInboxComposeResult", lambda **kw: kw),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once() if bound else None
        out = await router.create_rep_inbox_thread(payload, SimpleNamespace(), user, db)
    assert len(out["messages"]) == 2
    return emit, user, dealer, thread


async def test_starting_a_conversation_from_the_inbox_is_one_line_however_many_channels():
    emit, user, dealer, thread = await _compose(bound=True)
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("message.sent", file_events.VISIBILITY_CLIENT)
    assert kwargs["title"] == "Message sent to Ana Lopez" and kwargs["actor"] is user
    assert kwargs["target_type"] == "inbox_thread" and kwargs["target_id"] == thread.id
    assert kwargs["meta"] == {"channels": ["email", "sms"]}
    assert "the body" not in str(kwargs)


async def test_a_conversation_with_no_file_behind_it_stays_off_every_timeline():
    emit, _, _, _ = await _compose(bound=False)
    emit.assert_not_awaited()


# status.changed — the desk's decisions


async def _review(*, current: str, new: str):
    dealer = SimpleNamespace(id=uuid.uuid4(), name="Acme LLC")
    user = _user()
    row = SimpleNamespace(id=uuid.uuid4(), human_review_status=current, human_review_note=None, human_reviewed_at=None, human_reviewed_by_user_id=None)
    payload = SimpleNamespace(status=new, note="the note")
    db = _db(execute=AsyncMock(side_effect=[_result(scalar_one=row), _result(first=None)]))
    with (
        patch.object(router, "require_super_admin", lambda u: None),
        patch.object(router, "resolve_dealer_scope", AsyncMock(return_value=dealer)),
        patch.object(router, "_current_qc_context", AsyncMock(return_value=(None, {}))),
        patch.object(router.qc_master_application, "build_readiness", lambda ctx: {"package_ready": True, "items": []}),
        patch.object(router, "SubmissionReadinessRead", lambda **kw: "ok"),
        patch.object(router, "log_action", AsyncMock()),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once() if new != current else None
        out = await router.patch_submission_human_review(dealer.id, payload, user, db)
    assert out == "ok" and row.human_review_status == new
    return emit, row, user, dealer


async def test_approving_a_file_at_the_desk_is_told_to_the_team_without_the_note():
    emit, row, user, dealer = await _review(current="pending", new="fundable")
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("status.changed", file_events.VISIBILITY_TEAM)
    assert kwargs["title"] == "Desk review: approved" and kwargs["actor"] is user
    assert kwargs["target_type"] == "application_profile" and kwargs["target_id"] == row.id
    assert kwargs["meta"] == {"from": "pending", "to": "fundable"}
    assert "the note" not in str(kwargs)


async def test_repeating_the_same_decision_adds_nothing_to_the_timeline():
    emit, _, _, _ = await _review(current="fundable", new="fundable")
    emit.assert_not_awaited()


async def _finalize(*, current: str, new: str | None, funded_amount=None, contract=None):
    dealer = SimpleNamespace(id=uuid.uuid4(), status=current, funded_amount=None, name="Acme LLC")
    user = _user()
    payload = SimpleNamespace(status=new, funded_amount=funded_amount)
    db = _db(execute=AsyncMock(side_effect=[_result(first=contract), _result(first=None)]))
    with (
        patch.object(router, "require_super_admin", lambda u: None),
        patch.object(router, "_load_visible_dealer", AsyncMock(return_value=dealer)),
        patch.object(router, "log_action", AsyncMock()),
        patch.object(router, "_dealer_read", AsyncMock(return_value="ok")),
        patch.object(file_events, "emit", AsyncMock(return_value=None)) as emit,
    ):
        db.commit.side_effect = lambda: emit.assert_awaited_once() if new not in (None, current) else None
        out = await router.patch_application_finalization(dealer.id, payload, user, db)
    assert out == "ok"
    return emit, dealer, user


async def test_declining_a_file_is_told_to_the_team_in_plain_words():
    emit, dealer, user = await _finalize(current="active", new="declined")
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["dealer_id"] == dealer.id
    assert (kwargs["kind"], kwargs["visibility"]) == ("status.changed", file_events.VISIBILITY_TEAM)
    assert kwargs["title"] == "Declined" and kwargs["actor"] is user
    assert kwargs["target_type"] == "dealer" and kwargs["target_id"] == dealer.id
    assert kwargs["meta"] == {"from": "active", "to": "declined"}


async def test_signing_is_told_once_the_contract_is_executed():
    emit, _, _ = await _finalize(current="forms_out", new="signed", contract=SimpleNamespace(status="executed"))
    assert emit.await_args.kwargs["title"] == "Signed"


async def test_recording_only_the_funded_amount_adds_nothing_to_the_timeline():
    emit, dealer, _ = await _finalize(current="complete", new=None, funded_amount=25000.0, contract=SimpleNamespace(status="executed"))
    assert dealer.funded_amount == 25000.0
    emit.assert_not_awaited()
