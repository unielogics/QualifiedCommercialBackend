"""The desk can email a client the link that fills in a form, in one click.

A file with no debt schedule had two options on the desk: copy the form's link
and paste it into an email by hand, or put the form on the checklist and wait
for the AI's reminder, which carries the secure room's address rather than the
form. This pins the one-click path: the right link, to the verified address,
once, with nothing secret in the email and a record of who sent it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.application_profile import ApplicationRoomDelivery
from app.services import form_link_email as fle


class _Result:
    def __init__(self, value): self._value = value
    def scalars(self): return self
    def first(self): return self._value
    def scalar_one_or_none(self): return self._value


class _DB:
    """Enough session for the service: two kinds of lookup, adds and a flush."""

    def __init__(self, *, intake=None, client=None, recent=None, state=None):
        self.intake, self.client, self.recent, self.state = intake, client, recent, state
        self.added: list = []
        self.flushed = 0

    async def get(self, model, _id):
        return self.intake if model.__name__ == "PublicUnderwritingIntake" else self.client

    async def execute(self, stmt):
        text = str(stmt)
        if "application_room_deliveries" in text:
            return _Result(self.recent)
        if "application_requirement_states" in text:
            return _Result(self.state)
        raise AssertionError(f"unexpected query: {text[:80]}")

    def add(self, row): self.added.append(row)
    async def flush(self): self.flushed += 1


def _profile(**extra):
    return SimpleNamespace(id=uuid.uuid4(), intake_id=uuid.uuid4(), client_id=None, primary_bucket_id=uuid.uuid4(), **extra)


def _user(role=Role.SUPER_ADMIN, name="Franco Rivera"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name)


@pytest.fixture
def wired(monkeypatch):
    """The collaborators the service reaches for, replaced by recorders."""
    sent, minted, logged, events = [], [], [], []
    expires = datetime(2026, 12, 5, 15, 0, tzinfo=UTC)

    async def active_link(_db, _bucket):
        return SimpleNamespace(token="roomtoken", recipient_email=None)

    async def mint_link(_db, profile, **kwargs):
        minted.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), expires_at=expires), "form-token-123"

    async def send_as_user(_db, user_id, **kwargs):
        sent.append({"user_id": user_id, **kwargs})
        return SimpleNamespace(ok=True, detail="Email accepted", message_id="msg-1")

    async def not_suppressed(_db, _client_id): return False

    async def log_profile_action(_db, profile, user, action, summary, **kwargs):
        logged.append((action, summary, kwargs))

    async def emit(_db, **kwargs):
        events.append(kwargs)

    monkeypatch.setattr(fle.client_room, "active_link", active_link)
    monkeypatch.setattr(fle.financial_statements, "mint_link", mint_link)
    monkeypatch.setattr(fle, "send_as_user", send_as_user)
    monkeypatch.setattr(fle, "email_is_suppressed", not_suppressed)
    monkeypatch.setattr(fle.profiles, "log_profile_action", log_profile_action)
    monkeypatch.setattr(fle.file_events, "emit", emit)
    monkeypatch.setattr(fle, "get_settings", lambda: SimpleNamespace(frontend_app_url="https://app.example.test/"))
    return SimpleNamespace(sent=sent, minted=minted, logged=logged, events=events, expires=expires)


def _intake(email="owner@example.com"):
    return SimpleNamespace(email=email)


# --- the email itself --------------------------------------------------------


def test_the_email_carries_the_form_link_and_the_room_as_the_alternative_and_no_pin():
    subject, body = fle.compose_form_link_email(
        label="Business debt schedule",
        url="https://app.example.test/forms/debt_schedule/abc",
        expires_on="December 5, 2026",
        room_url="https://app.example.test/buckets/request/room?tab=todo",
        sender="Franco Rivera",
    )
    assert subject == "Action needed: Business debt schedule"
    assert "we need your business debt schedule" in body
    assert "https://app.example.test/forms/debt_schedule/abc" in body
    assert "works until December 5, 2026" in body
    assert "Upload it in your secure application room instead" in body
    assert "https://app.example.test/buckets/request/room?tab=todo" in body
    assert "your room PIN is not included in this email" in body
    assert body.rstrip().endswith("Thank you,\nFranco Rivera")


def test_the_email_leaves_out_what_it_does_not_have():
    _, body = fle.compose_form_link_email(label="Personal financial statement", url="https://x.test/f", expires_on=None, room_url=None, sender=None)
    assert "works until" not in body
    assert "secure application room" not in body
    assert "Thank you" not in body
    assert "https://x.test/f" in body


# --- sending -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_mints_a_link_and_emails_it_once_and_records_who_sent_it(wired):
    profile, user = _profile(), _user()
    slot = SimpleNamespace(id=uuid.uuid4())
    state = SimpleNamespace(last_requested_at=None, first_requested_at=None, status="missing")
    db = _DB(intake=_intake(), state=state)

    delivery = await fle.email_form_link(db, profile=profile, kind="debt_schedule", label="Business debt schedule", slot=slot, user=user)

    assert wired.minted == [{
        "statement_id": None, "label": "Emailed to the client", "invitee_email": "owner@example.com", "created_by": user.id,
        "kind": "debt_schedule",
    }]
    (mail,) = wired.sent
    assert mail["user_id"] == user.id and mail["to_emails"] == ["owner@example.com"]
    assert mail["subject"] == "Action needed: Business debt schedule"
    assert "https://app.example.test/forms/debt_schedule/form-token-123" in mail["body_text"]
    assert f"https://app.example.test/buckets/request/roomtoken?tab=todo&request={slot.id}" in mail["body_text"]
    assert "December 5, 2026" in mail["body_text"]
    assert isinstance(delivery, ApplicationRoomDelivery)
    assert (delivery.action_kind, delivery.channel, delivery.status) == ("form_link_email", "email", "sent")
    assert delivery.initiation_source == "staff_form_link" and delivery.created_by_user_id == user.id
    assert delivery.requested_document_id == slot.id
    assert "form-token-123" not in str(delivery.provider_result) and "form-token-123" not in (delivery.detail or "")
    assert db.added == [delivery]
    # the row now says it was asked
    assert state.status == "requested" and state.last_requested_at is not None and state.first_requested_at is not None
    assert wired.logged[0][0] == "financial_form.link_emailed"
    assert wired.events[0]["kind"] == "document.link_emailed" and wired.events[0]["visibility"] == "team"


@pytest.mark.asyncio
async def test_a_personal_statement_link_fills_a_statement_so_one_is_started_when_there_is_none(wired, monkeypatch):
    created = []

    async def latest_for_profile(_db, _profile_id): return None
    async def save_statement(_db, profile, *, body, actor_user_id):
        created.append((body, actor_user_id))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(fle.financial_statements, "latest_for_profile", latest_for_profile)
    monkeypatch.setattr(fle.financial_statements, "save_statement", save_statement)
    user = _user()
    await fle.email_form_link(_DB(intake=_intake()), profile=_profile(), kind="pfs", label="Personal financial statement", slot=None, user=user)
    assert len(created) == 1 and created[0][1] == user.id
    assert wired.minted[0]["statement_id"] is not None


@pytest.mark.asyncio
async def test_the_debt_schedule_needs_no_placeholder_statement(wired):
    await fle.email_form_link(_DB(intake=_intake()), profile=_profile(), kind="debt_schedule", label="Business debt schedule", slot=None, user=_user())
    assert wired.minted[0]["statement_id"] is None


@pytest.mark.asyncio
async def test_a_second_click_inside_two_minutes_returns_the_first_send_and_mails_nobody(wired):
    first = SimpleNamespace(id=uuid.uuid4(), status="sent")
    db = _DB(intake=_intake(), recent=first)
    again = await fle.email_form_link(db, profile=_profile(), kind="debt_schedule", label="Business debt schedule", slot=SimpleNamespace(id=uuid.uuid4()), user=_user())
    assert again is first
    assert wired.sent == [] and wired.minted == [] and db.added == []


@pytest.mark.asyncio
async def test_with_nobody_to_email_it_says_so_instead_of_minting_a_link(wired):
    with pytest.raises(HTTPException) as caught:
        await fle.email_form_link(_DB(intake=SimpleNamespace(email=None)), profile=_profile(), kind="debt_schedule", label="Business debt schedule", slot=None, user=_user())
    assert caught.value.status_code == 409 and "No verified client email" in caught.value.detail
    assert wired.minted == [] and wired.sent == []


@pytest.mark.asyncio
async def test_an_address_that_opted_out_is_respected(wired, monkeypatch):
    async def suppressed(_db, _client_id): return True
    monkeypatch.setattr(fle, "email_is_suppressed", suppressed)
    profile = _profile()
    profile.client_id = uuid.uuid4()
    with pytest.raises(HTTPException) as caught:
        await fle.email_form_link(_DB(intake=_intake()), profile=profile, kind="debt_schedule", label="Business debt schedule", slot=None, user=_user())
    assert caught.value.status_code == 409 and "opted out" in caught.value.detail
    assert wired.sent == []


@pytest.mark.asyncio
async def test_a_failed_send_is_recorded_as_failed_and_does_not_mark_the_row_as_asked(wired, monkeypatch):
    async def refused(_db, _user_id, **_kwargs):
        return SimpleNamespace(ok=False, detail="Mailbox rejected the message", message_id=None)

    monkeypatch.setattr(fle, "send_as_user", refused)
    state = SimpleNamespace(last_requested_at=None, first_requested_at=None, status="missing")
    delivery = await fle.email_form_link(_DB(intake=_intake(), state=state), profile=_profile(), kind="debt_schedule", label="Business debt schedule", slot=None, user=_user())
    assert delivery.status == "failed" and delivery.detail == "Mailbox rejected the message"
    assert state.status == "missing" and state.last_requested_at is None
    assert wired.logged[0][0] == "financial_form.link_email_failed"
    assert wired.events == []


@pytest.mark.asyncio
async def test_the_client_record_is_the_fallback_when_the_intake_has_no_email(wired):
    db = _DB(intake=SimpleNamespace(email=None), client=SimpleNamespace(email="Client@Example.com"))
    profile = _profile()
    profile.client_id = uuid.uuid4()
    delivery = await fle.email_form_link(db, profile=profile, kind="debt_schedule", label="Business debt schedule", slot=None, user=_user())
    assert delivery.recipient_email == "client@example.com"


# --- the route ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_underwriting_desk_can_email_a_client_a_form_link():
    from app.routers import application_profiles as route

    with pytest.raises(HTTPException) as caught:
        await route.email_financial_form_link(uuid.uuid4(), "debt_schedule", _user(role=Role.BROKER), db=None)
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_form_is_a_404_before_anything_is_touched():
    from app.routers import application_profiles as route

    with pytest.raises(HTTPException) as caught:
        await route.email_financial_form_link(uuid.uuid4(), "tax_returns", _user(), db=None)
    assert caught.value.status_code == 404


def test_every_form_kind_the_route_accepts_has_a_requirement_to_mark_as_asked():
    from app.routers import application_profiles as route

    assert set(fle.REQUIREMENT_KEYS) == set(route._FORM_SLOT_CATEGORY)
    assert fle.DUPLICATE_WINDOW == timedelta(minutes=2)
