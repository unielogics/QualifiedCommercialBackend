from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.enums import Role
from app.models.activity import Activity
from app.routers import me
from app.routers.me import BookingInviteShareRequest, _booking_invite_body


def test_booking_invite_body_adds_canonical_link_once() -> None:
    url = "https://app.qualifiedcommercial.com/book/dana-moreno"

    rendered = _booking_invite_body("Please choose a convenient time.", url)

    assert rendered.endswith(url)
    assert rendered.count(url) == 1


def test_booking_invite_body_preserves_existing_link() -> None:
    url = "https://app.qualifiedcommercial.com/book/dana-moreno"
    body = f"Please choose a convenient time:\n{url}"

    assert _booking_invite_body(body, url) == body


def test_booking_invite_request_rejects_invalid_or_empty_recipients() -> None:
    with pytest.raises(ValidationError):
        BookingInviteShareRequest(to_emails=[], subject="Book a meeting", body="Choose a time")

    with pytest.raises(ValidationError):
        BookingInviteShareRequest(
            to_emails=["not-an-email"],
            subject="Book a meeting",
            body="Choose a time",
        )


@pytest.mark.asyncio
async def test_share_booking_link_sends_canonical_url_and_writes_audit(monkeypatch) -> None:
    booking_url = "https://app.qualifiedcommercial.com/book/dana-moreno"
    user = SimpleNamespace(
        id=uuid4(),
        email="dana@qualifiedcommercial.com",
        role=Role.SUPER_ADMIN,
    )

    async def fake_settings(db, current_user):
        assert current_user is user
        return SimpleNamespace(enabled=True, slug="dana-moreno")

    sent: dict[str, object] = {}

    async def fake_send(db, user_id, *, to_emails, subject, body_text):
        sent.update(
            user_id=user_id,
            to_emails=to_emails,
            subject=subject,
            body_text=body_text,
        )
        return SimpleNamespace(ok=True, detail="sent", message_id="message-123")

    class FakeDb:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.committed = False

        def add(self, row) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            self.committed = True

    monkeypatch.setattr(me, "_get_or_create_booking_settings", fake_settings)
    monkeypatch.setattr(me, "_booking_public_url", lambda row: booking_url)
    monkeypatch.setattr("app.services.email.user_mailer.send_as_user", fake_send)

    db = FakeDb()
    response = await me.share_booking_link(
        BookingInviteShareRequest(
            to_emails=["Client@Example.com", "client@example.com"],
            subject=" Book a meeting ",
            body="Choose a convenient time.",
        ),
        user,
        db,
    )

    assert response.ok is True
    assert response.booking_url == booking_url
    assert sent == {
        "user_id": user.id,
        "to_emails": ["client@example.com"],
        "subject": "Book a meeting",
        "body_text": f"Choose a convenient time.\n\nChoose a time that works for you:\n{booking_url}",
    }
    assert db.committed is True
    assert len(db.added) == 1
    audit = db.added[0]
    assert isinstance(audit, Activity)
    assert audit.kind == "calendar.booking_invite_sent"
    assert audit.payload["recipients"] == ["client@example.com"]
