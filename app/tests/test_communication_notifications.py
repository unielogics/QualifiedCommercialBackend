from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.dealer_os import router as dealer_router
from app.dealer_os.models import DealerRepContact, DealerRepInboxThread
from app.routers import notifications as notification_router
from app.services import notifications
from app.services.email.user_inbox_sync import _thread_subject_key


class _FakeSession:
    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        for row in self.added:
            if hasattr(row, "id") and row.id is None:
                row.id = uuid4()


def test_read_all_route_precedes_uuid_notification_route() -> None:
    paths = [route.path for route in notification_router.router.routes]
    assert paths.index("/notifications/read-all") < paths.index(
        "/notifications/{notification_id}/read"
    )


@pytest.mark.asyncio
async def test_inbound_communication_notification_is_individual_and_deep_linked(
    monkeypatch,
) -> None:
    captured = {}

    async def fake_notify_users(_db, **kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(id=uuid4())]

    monkeypatch.setattr(notifications, "notify_users", fake_notify_users)
    recipient_id = uuid4()
    rows = await notifications.notify_inbound_communication(
        object(),
        recipient_ids={recipient_id},
        channel="sms",
        sender_label="Client Name",
        thread_id="sms:phone:+12015550100",
        message_id="message-1",
    )

    assert rows
    assert captured["recipient_ids"] == {recipient_id}
    assert captured["event_type"] == "sms_received"
    assert captured["target_type"] == "communication_thread"
    assert captured["target_id"] == "sms:phone:+12015550100"
    assert captured["deep_link"] == "/inbox?thread=sms%3Aphone%3A%2B12015550100"
    assert captured.get("batch_key") is None
    assert captured["email"] is False


@pytest.mark.asyncio
async def test_rep_inbox_append_creates_notification_for_inbound_only(monkeypatch) -> None:
    captured = []

    async def fake_notification(_db, **kwargs):
        captured.append(kwargs)
        return []

    monkeypatch.setattr(dealer_router, "notify_inbound_communication", fake_notification)
    owner_id = uuid4()
    thread = DealerRepInboxThread(
        id=uuid4(),
        owner_user_id=owner_id,
        subject="Client reply",
        channel="email",
        source="file_message",
        unread_count=0,
    )
    contact = DealerRepContact(
        id=uuid4(),
        owner_user_id=owner_id,
        full_name="Client Name",
        source="test",
    )
    db = _FakeSession()

    await dealer_router._append_rep_inbox_message(
        db,
        thread=thread,
        contact=contact,
        direction="inbound",
        channel="email",
        body="Please call me.",
        subject="Client reply",
        sender="client@example.com",
    )

    assert thread.unread_count == 1
    assert len(captured) == 1
    assert captured[0]["recipient_ids"] == {owner_id}
    assert captured[0]["thread_id"] == f"rep:{thread.id}"
    assert captured[0]["sender_label"] == "Client Name"


def test_file_email_reply_subjects_match_reply_and_forward_prefixes() -> None:
    expected = "qualified commercial | qc-2026-00008"
    assert _thread_subject_key("Qualified Commercial | QC-2026-00008") == expected
    assert _thread_subject_key("Re: Qualified Commercial | QC-2026-00008") == expected
    assert _thread_subject_key("Fwd: Re: Qualified   Commercial | QC-2026-00008") == expected
