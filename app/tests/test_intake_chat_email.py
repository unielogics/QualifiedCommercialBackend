"""AI-requested outstanding-items email from the client chat (contract item 6)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.bucket import BucketAIMessage
from app.services import bucket_ai, file_events, intake_chat_actions, intake_chat_email


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


REQUEST = {"kind": "outstanding_items", "reason": "The client asked for the list by email."}
ANSWER = "Here is where the file stands.\n\nFor Personal financial statement, will you upload your own document?"


def _message(*, audience="uploader", role="assistant", requested=REQUEST, content=ANSWER):
    return SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        upload_link_id=uuid4(),
        audience=audience,
        role=role,
        content=content,
        metadata_json={"raw": {"answer": content, "requested_email": requested}},
    )


def _profile(*, intake_id=None):
    return SimpleNamespace(
        id=uuid4(),
        intake_id=intake_id,
        client_id=None,
        primary_bucket_id=uuid4(),
        program_selection_mode="auto",
    )


def _requirement(key, *, client_visible=True, required_level="required", status="missing", coverage_complete=False):
    return SimpleNamespace(
        requirement_key=key,
        client_visible=client_visible,
        required_level=required_level,
        status=status,
        coverage_complete=coverage_complete,
    )


READINESS = SimpleNamespace(
    requirements=[
        _requirement("business_tax_returns_2_years"),
        _requirement("owner_personal_financial_statement", status="requested"),
        _requirement("business_debt_schedule", status="verified"),
        _requirement("ytd_p_and_l_balance_sheet", required_level="recommended"),
        _requirement("internal_only_item", client_visible=False),
        _requirement("business_bank_statements_6_months", status="received_unverified", coverage_complete=True),
    ]
)
OUTSTANDING = ["business_tax_returns_2_years", "owner_personal_financial_statement"]


def _db(*, recent=None, intake=None):
    return SimpleNamespace(
        execute=AsyncMock(return_value=_Result(scalar=recent)),
        get=AsyncMock(return_value=intake),
        flush=AsyncMock(),
        add=Mock(),
    )


def _delivery(*, status="sent", recipient="client@example.com", detail="Accepted"):
    return SimpleNamespace(id=uuid4(), status=status, recipient_email=recipient, detail=detail)


def _run(message, *, db, profile, send, readiness=READINESS, upload_link=None, intake_id=None):
    with patch.object(
        intake_chat_email, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ) as scope, patch.object(
        intake_chat_email, "get_program_readiness", AsyncMock(return_value=readiness)
    ), patch.object(
        intake_chat_email.missing_item_automation, "send_requirements_email", send
    ):
        receipt = asyncio.run(
            intake_chat_email.fulfil_requested_email(
                db, message=message, upload_link=upload_link, intake_id=intake_id
            )
        )
    return receipt, scope


# --- when the email is (not) attempted ---------------------------------------


def test_nothing_requested_sends_nothing_and_leaves_the_message_alone() -> None:
    message = _message(requested=None)
    send = AsyncMock()

    receipt, scope = _run(message, db=_db(), profile=_profile(), send=send)

    assert receipt is None
    send.assert_not_awaited()
    scope.assert_not_awaited()
    assert message.content == ANSWER
    assert "email" not in message.metadata_json


@pytest.mark.parametrize(
    "requested",
    [
        {"kind": "template", "reason": "wants the PFS template"},
        {"kind": "room_link"},
        "outstanding_items",
        {"reason": "no kind at all"},
    ],
)
def test_only_the_outstanding_items_kind_is_fulfilled(requested) -> None:
    message = _message(requested=requested)
    send = AsyncMock()

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send)

    assert receipt is None
    send.assert_not_awaited()
    assert message.content == ANSWER


@pytest.mark.parametrize("audience", ["admin", "share", "vendor", "client"])
def test_non_uploader_audiences_never_email_even_when_the_model_asks(audience) -> None:
    message = _message(audience=audience)
    send = AsyncMock(return_value=_delivery())

    receipt, scope = _run(message, db=_db(), profile=_profile(), send=send)

    assert receipt is None
    send.assert_not_awaited()
    scope.assert_not_awaited()
    assert message.content == ANSWER


def test_a_user_turn_carrying_the_field_is_ignored() -> None:
    message = _message(role="user")
    send = AsyncMock(return_value=_delivery())

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send)

    assert receipt is None
    send.assert_not_awaited()


def test_missing_profile_records_a_refusal_and_does_not_send() -> None:
    message = _message()
    send = AsyncMock(return_value=_delivery())

    receipt, _ = _run(message, db=_db(), profile=None, send=send)

    assert receipt == message.metadata_json["email"]
    assert receipt["ok"] is False
    assert "No application file" in receipt["detail"]
    send.assert_not_awaited()
    assert message.content == ANSWER


def test_nothing_outstanding_means_no_email() -> None:
    message = _message()
    send = AsyncMock(return_value=_delivery())
    complete = SimpleNamespace(
        requirements=[
            _requirement("business_tax_returns_2_years", status="verified"),
            _requirement("owner_personal_financial_statement", status="waived"),
        ]
    )

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send, readiness=complete)

    assert receipt["ok"] is False
    assert "Nothing is outstanding" in receipt["detail"]
    send.assert_not_awaited()
    assert message.content == ANSWER


# --- the send itself ----------------------------------------------------------


def test_outstanding_items_email_goes_through_the_requirements_sender_with_ai_chat_source() -> None:
    message = _message()
    profile = _profile()
    send = AsyncMock(return_value=_delivery())
    upload_link = SimpleNamespace(id=uuid4())
    intake_id = uuid4()

    receipt, scope = _run(
        message, db=_db(), profile=profile, send=send, upload_link=upload_link, intake_id=intake_id
    )

    scope.assert_awaited_once()
    assert scope.await_args.kwargs == {
        "bucket_id": message.bucket_id,
        "intake_id": intake_id,
        "upload_link_id": upload_link.id,
    }
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["profile"] is profile
    # Only client-visible, required items the client still has to act on:
    # not the verified one, not the recommended one, not the internal one, and
    # not the upload that is complete and waiting on staff review.
    assert kwargs["requirement_keys"] == OUTSTANDING
    assert kwargs["user"] is None
    assert kwargs["initiation_source"] == "ai_chat"
    assert receipt["ok"] is True
    assert receipt["requirement_keys"] == OUTSTANDING
    assert receipt["recipient_masked"] == "cl***@example.com"
    assert receipt["reason"] == REQUEST["reason"]
    assert receipt["initiation_source"] == "ai_chat"


def test_success_appends_the_english_confirmation_once_and_stores_the_receipt() -> None:
    message = _message()
    delivery = _delivery()
    send = AsyncMock(return_value=delivery)

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send)

    assert message.content == f"{ANSWER}\n\nI've emailed this to cl***@example.com."
    assert message.metadata_json["email"] == receipt
    assert message.metadata_json["email"]["delivery_id"] == str(delivery.id)
    # The model's raw answer survives next to the receipt.
    assert message.metadata_json["raw"]["requested_email"] == REQUEST
    assert message.content.count("I've emailed this to") == 1


def test_spanish_conversation_gets_the_spanish_confirmation() -> None:
    profile = _profile(intake_id=uuid4())
    intake = SimpleNamespace(id=profile.intake_id, preferred_language="es")
    message = _message()
    db = _db(intake=intake)
    send = AsyncMock(return_value=_delivery(recipient="maria.lopez@example.com"))

    receipt, _ = _run(message, db=db, profile=profile, send=send)

    assert receipt["ok"] is True
    assert message.content.endswith("\n\nSe lo envié por email a ma***@example.com.")
    assert "I've emailed this" not in message.content


def test_english_is_the_default_when_the_intake_language_is_not_spanish() -> None:
    profile = _profile(intake_id=uuid4())
    intake = SimpleNamespace(id=profile.intake_id, preferred_language="en")
    message = _message()
    send = AsyncMock(return_value=_delivery())

    _run(message, db=_db(intake=intake), profile=profile, send=send)

    assert message.content.endswith("\n\nI've emailed this to cl***@example.com.")


def test_failed_provider_delivery_records_the_receipt_without_a_confirmation_line() -> None:
    message = _message()
    send = AsyncMock(return_value=_delivery(status="failed", detail="Mailbox unavailable"))

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send)

    assert receipt["ok"] is False
    assert receipt["detail"] == "Mailbox unavailable"
    assert message.content == ANSWER
    assert message.metadata_json["email"]["ok"] is False


# --- guards -------------------------------------------------------------------


def test_http_exception_from_the_sender_is_swallowed_into_the_receipt() -> None:
    message = _message()
    send = AsyncMock(side_effect=HTTPException(409, "Client opted out of automated email"))

    receipt, _ = _run(message, db=_db(), profile=_profile(), send=send)

    send.assert_awaited_once()
    assert receipt == message.metadata_json["email"]
    assert receipt["ok"] is False
    assert receipt["detail"] == "Client opted out of automated email"
    assert message.content == ANSWER


def test_second_ai_email_inside_24_hours_is_refused_before_the_sender_runs() -> None:
    message = _message()
    profile = _profile()
    recent = SimpleNamespace(id=uuid4(), status="sent")
    db = _db(recent=recent)
    send = AsyncMock(return_value=_delivery())
    fixed_now = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)

    with patch.object(intake_chat_email, "now", return_value=fixed_now):
        receipt, _ = _run(message, db=db, profile=profile, send=send)

    send.assert_not_awaited()
    assert receipt["ok"] is False
    assert receipt["skipped"] == "rate_limited"
    assert receipt["delivery_id"] == str(recent.id)
    assert message.content == ANSWER
    # The guard is keyed on the delivery rows this module writes: same profile,
    # initiation_source "ai_chat", created inside the trailing 24 hours.
    db.execute.assert_awaited_once()
    statement = db.execute.await_args.args[0]
    compiled = statement.compile()
    sql = str(compiled)
    assert "application_room_deliveries.profile_id = " in sql
    assert "application_room_deliveries.initiation_source = " in sql
    assert "application_room_deliveries.created_at >= " in sql
    params = compiled.params
    assert params["profile_id_1"] == profile.id
    assert params["initiation_source_1"] == "ai_chat"
    assert params["created_at_1"] == fixed_now - timedelta(hours=24)


def test_a_failed_ai_email_inside_the_window_also_blocks_a_retry() -> None:
    # A rejected send is still an attempt; the client is not re-emailed every
    # turn until the provider happens to accept.
    message = _message()
    db = _db(recent=SimpleNamespace(id=uuid4(), status="failed"))
    send = AsyncMock(return_value=_delivery())

    receipt, _ = _run(message, db=db, profile=_profile(), send=send)

    send.assert_not_awaited()
    assert receipt["skipped"] == "rate_limited"


def test_window_constant_is_24_hours() -> None:
    assert intake_chat_email.AI_EMAIL_WINDOW == timedelta(hours=24)
    assert intake_chat_email.INITIATION_SOURCE == "ai_chat"


def test_mask_email_keeps_two_characters_and_the_domain() -> None:
    assert intake_chat_email.mask_email("franco@example.com") == "fr***@example.com"
    assert intake_chat_email.mask_email("a@example.com") == "a***@example.com"
    assert intake_chat_email.mask_email(None) == "***"
    assert intake_chat_email.mask_email("not-an-email") == "***"


# --- wiring from create_chat_reply --------------------------------------------


def _model_response(parsed: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(parsed))],
        model="anthropic.claude-haiku",
    )


def _bucket() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name="Room",
        client_name="Fixture Client",
        purpose="Underwriting",
        bucket_type="underwriting",
        ai_context={"review_type": "dealer_gatekeeper_v1"},
        files=[],
        requested_documents=[],
        notes=[],
    )


def _reply(*, audience: str, parsed: dict, fulfil: AsyncMock, actions: AsyncMock):
    db = SimpleNamespace(add=Mock(), flush=AsyncMock(), execute=AsyncMock())
    bucket = _bucket()
    upload_link = SimpleNamespace(id=uuid4(), recipient_name="Client") if audience == "uploader" else None
    intake_id = uuid4()
    with patch.object(bucket_ai, "_chat_context", AsyncMock(return_value={"audience": audience})), patch.object(
        bucket_ai, "_recent_thread_messages", AsyncMock(return_value=[])
    ), patch.object(
        bucket_ai, "tracked_messages_create", AsyncMock(return_value=_model_response(parsed))
    ), patch.object(bucket_ai, "get_client", Mock(return_value=None)), patch.object(
        bucket_ai, "model_light", Mock(return_value="model-light")
    ), patch.object(bucket_ai, "_create_proposals", AsyncMock(return_value=[])), patch.object(
        bucket_ai, "log_bucket_ai_activity", AsyncMock()
    ), patch.object(file_events, "emit", AsyncMock()), patch.object(
        intake_chat_actions, "author_actions_for_message", actions
    ), patch.object(intake_chat_email, "fulfil_requested_email", fulfil):
        messages, proposals, widget = asyncio.run(
            bucket_ai.create_chat_reply(
                db,
                bucket=bucket,
                audience=audience,
                message="Can you email me the list? My bookkeeper will finish it.",
                actor_name="Client",
                upload_link=upload_link,
                intake_id=intake_id,
                sender_kind="client",
            )
        )
    return messages, upload_link, intake_id


def test_create_chat_reply_fulfils_the_requested_email_right_after_the_actions() -> None:
    order: list[str] = []
    actions = AsyncMock(side_effect=lambda *a, **k: order.append("actions") or [])
    fulfil = AsyncMock(side_effect=lambda *a, **k: order.append("email") or {"ok": True})
    parsed = {
        "answer": "I can do that.",
        "proposed_context_patch": None,
        "proposed_action_items": [],
        "proposed_borrower_facts": None,
        "requested_email": REQUEST,
    }

    messages, upload_link, intake_id = _reply(audience="uploader", parsed=parsed, fulfil=fulfil, actions=actions)

    assert order == ["actions", "email"]
    assistant = messages[1]
    assert isinstance(assistant, BucketAIMessage)
    assert assistant.role == "assistant" and assistant.audience == "uploader"
    assert assistant.metadata_json["raw"]["requested_email"] == REQUEST
    fulfil.assert_awaited_once()
    kwargs = fulfil.await_args.kwargs
    assert kwargs["message"] is assistant
    assert kwargs["upload_link"] is upload_link
    assert kwargs["intake_id"] == intake_id
    assert actions.await_args.kwargs["message"] is assistant


def test_create_chat_reply_never_emails_from_the_admin_thread() -> None:
    actions = AsyncMock(return_value=[])
    fulfil = AsyncMock(return_value={"ok": True})
    parsed = {"answer": "Internal note.", "requested_email": REQUEST}

    messages, _, _ = _reply(audience="admin", parsed=parsed, fulfil=fulfil, actions=actions)

    fulfil.assert_not_awaited()
    actions.assert_not_awaited()
    assert messages[1].content == "Internal note."


def test_create_chat_reply_keeps_the_answer_when_the_email_step_blows_up() -> None:
    actions = AsyncMock(return_value=[])
    fulfil = AsyncMock(side_effect=RuntimeError("provider exploded"))
    parsed = {"answer": "Here is the status.", "requested_email": REQUEST}

    messages, _, _ = _reply(audience="uploader", parsed=parsed, fulfil=fulfil, actions=actions)

    fulfil.assert_awaited_once()
    assert messages[1].content == "Here is the status."
    assert "AI is unavailable" not in messages[1].content
