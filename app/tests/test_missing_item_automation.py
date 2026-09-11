from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.services import missing_item_automation


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


def test_client_request_excludes_complete_uploads_waiting_for_staff_review() -> None:
    waiting_for_staff = SimpleNamespace(status="received_unverified", coverage_complete=True)
    partial_upload = SimpleNamespace(status="received_unverified", coverage_complete=False)

    assert missing_item_automation._client_submission_needed(waiting_for_staff) is False
    assert missing_item_automation._client_submission_needed(partial_upload) is True


def test_combined_requirement_email_sends_one_message_and_updates_every_item() -> None:
    profile = SimpleNamespace(
        id=uuid4(),
        client_id=None,
        loan_id=None,
        primary_bucket_id=uuid4(),
        missing_item_email_attempts=0,
        missing_item_email_last_sent_at=None,
        missing_item_email_next_send_at=None,
        missing_item_email_requirement_key=None,
    )
    first = SimpleNamespace(
        requirement_key="business_tax_returns_2_years",
        label="Last 2 years business tax returns",
        requested_document_id=uuid4(),
        first_requested_at=None,
        last_requested_at=None,
        status="missing",
    )
    second = SimpleNamespace(
        requirement_key="business_debt_schedule",
        label="Business debt schedule",
        requested_document_id=uuid4(),
        first_requested_at=None,
        last_requested_at=None,
        status="requested",
    )
    readiness = SimpleNamespace(
        requirements=[
            SimpleNamespace(requirement_key=first.requirement_key, client_visible=True, status="missing", coverage_complete=False),
            SimpleNamespace(requirement_key=second.requirement_key, client_visible=True, status="requested", coverage_complete=False),
        ],
        programs=[SimpleNamespace(blocking_requirement_keys=[first.requirement_key, second.requirement_key])],
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(rows=[first, second]), _Result(scalar=None)]),
        flush=AsyncMock(),
        add=Mock(),
    )
    sender = SimpleNamespace(id=uuid4())
    send = AsyncMock(return_value=SimpleNamespace(ok=True, message_id="message-1", detail="Accepted"))

    with patch.object(missing_item_automation, "get_program_readiness", AsyncMock(return_value=readiness)), patch.object(
        missing_item_automation,
        "_active_room_link",
        AsyncMock(return_value=SimpleNamespace(token="room-token")),
    ), patch.object(
        missing_item_automation,
        "_recipient",
        AsyncMock(return_value="client@example.com"),
    ), patch.object(
        missing_item_automation,
        "_compose_combined_intro",
        AsyncMock(return_value=("Hello,\n\nPlease provide the consolidated items.", True)),
    ), patch.object(missing_item_automation, "template_for_requirement", return_value=None), patch.object(
        missing_item_automation,
        "send_as_user",
        send,
    ), patch.object(missing_item_automation.profiles, "log_profile_action", AsyncMock()):
        delivery = asyncio.run(
            missing_item_automation.send_requirements_email(
                db,
                profile=profile,
                requirement_keys=[first.requirement_key, second.requirement_key],
                user=sender,
                initiation_source="staff_requirement_batch_request",
            )
        )

    send.assert_awaited_once()
    outbound = send.await_args.kwargs
    assert outbound["to_emails"] == ["client@example.com"]
    assert outbound["body_text"].count("/buckets/request/room-token") == 1
    assert first.label in outbound["body_text"]
    assert second.label in outbound["body_text"]
    assert delivery.provider_result["requirement_keys"] == [first.requirement_key, second.requirement_key]
    assert delivery.provider_result["ai_composed"] is True
    assert first.status == "requested"
    assert first.last_requested_at is not None
    assert second.last_requested_at is not None
