"""The communications audit page's backend.

Two ledgers read as one list, an SES event finds its row, and the ownership
rule holds: everyone sees their own, a super admin sees everything, and a
message belonging to nobody is shown to super admins alone rather than
defaulting to everybody.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.message_send import MessageSend
from app.services.messaging import audit_feed, outbox, sns


def _user(role=Role.LOAN_EXEC, uid=None):
    return SimpleNamespace(id=uid or uuid4(), role=role, name="Dana", email="d@example.com")


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        # A list is what .scalars() wants; a single-row lookup gets None.
        return None if isinstance(self._value, list) else self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value if isinstance(self._value, list) else [])

    def scalar(self):
        return self._value


def _db(value=None):
    db = SimpleNamespace()
    db.execute = AsyncMock(return_value=_Result(value if value is not None else []))
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


def _send(**over):
    row = MessageSend(
        id=uuid4(), channel="email", direction="outbound", context="invite",
        to_email="client@example.com", subject="Action needed", status="sent", detail="",
        provider="ses", provider_message_id="SES-1", actor_label="user",
        secrets_masked=False, encryption_provider="fernet",
    )
    row.created_at = datetime.now(UTC)
    for k, v in over.items():
        setattr(row, k, v)
    return row


# --- who may read what --------------------------------------------------------------


def test_a_super_admin_is_recognised_as_seeing_everything():
    assert audit_feed.is_super_admin(_user(Role.SUPER_ADMIN)) is True
    assert audit_feed.is_super_admin(_user(Role.LOAN_EXEC)) is False


@pytest.mark.asyncio
async def test_an_operator_cannot_open_a_message_that_is_not_theirs():
    mine, theirs = _user(), _user()
    row = _send(owner_user_id=theirs.id, actor_user_id=theirs.id)
    db = _db()
    db.get = AsyncMock(return_value=row)
    assert await audit_feed.message_detail(db, mine, f"send:{row.id}") is None


@pytest.mark.asyncio
async def test_an_operator_can_open_what_they_sent():
    me = _user()
    row = _send(owner_user_id=me.id, actor_user_id=me.id)
    db = _db()
    db.get = AsyncMock(return_value=row)
    with patch.object(audit_feed, "_actor_names", AsyncMock(return_value={})):
        out = await audit_feed.message_detail(db, me, f"send:{row.id}")
    assert out is not None and out["subject"] == "Action needed"


@pytest.mark.asyncio
async def test_a_message_owned_by_nobody_is_super_admin_only():
    """A cron send about no particular file. Defaulting it to everybody would
    be a leak, so it defaults to nobody."""
    row = _send(owner_user_id=None, actor_user_id=None, actor_label="cron", job="job_digest")
    db = _db()
    db.get = AsyncMock(return_value=row)

    assert await audit_feed.message_detail(db, _user(Role.LOAN_EXEC), f"send:{row.id}") is None
    with patch.object(audit_feed, "_actor_names", AsyncMock(return_value={})):
        assert await audit_feed.message_detail(db, _user(Role.SUPER_ADMIN), f"send:{row.id}") is not None


@pytest.mark.asyncio
async def test_a_missing_message_and_a_forbidden_one_answer_the_same():
    """So the route cannot be used to discover that a message exists."""
    db = _db()
    db.get = AsyncMock(return_value=None)
    assert await audit_feed.message_detail(db, _user(), f"send:{uuid4()}") is None
    assert await audit_feed.message_detail(db, _user(), "send:not-a-uuid") is None


@pytest.mark.asyncio
async def test_a_client_cannot_reach_the_page_at_all():
    from app.routers.communications_audit import _require_operator

    for role in (Role.CLIENT, Role.DEALER_PARTNER, Role.LENDER, Role.FIELD_REP):
        with pytest.raises(HTTPException) as err:
            _require_operator(_user(role))
        assert err.value.status_code == 403


# --- two ledgers, one list -----------------------------------------------------------


def test_an_sms_row_normalizes_without_claiming_an_actor_it_never_had():
    """The SMS ledger predates actor attribution. Saying "system" would be a
    claim; it says nothing instead."""
    sms = SimpleNamespace(
        id=uuid4(), direction="outbound", phone_e164="+19735550148", body="hi",
        provider="twilio", provider_message_id="SM1", status="delivered", detail="",
        context="booking_reminder", client_id=None,
        created_at=datetime.now(UTC), delivered_at=datetime.now(UTC),
    )
    row = audit_feed._from_sms(sms)
    assert row.source == "sms_messages" and row.channel == "sms"
    assert row.actor_name is None and row.actor_label == "unknown"
    assert row.has_body is True


def test_an_email_row_reports_whether_a_body_was_kept():
    plain = audit_feed._from_email(_send(), {})
    assert plain.has_body is False
    kept = audit_feed._from_email(_send(body_text_enc="cipher", secrets_masked=True), {})
    assert kept.has_body is True and kept.secrets_masked is True


@pytest.mark.asyncio
async def test_both_ledgers_are_merged_newest_first():
    now = datetime.now(UTC)
    older = _send(subject="older")
    older.created_at = now - timedelta(hours=2)
    newer = _send(subject="newer")
    newer.created_at = now
    sms = SimpleNamespace(
        id=uuid4(), direction="outbound", phone_e164="+1973", body="x", provider="twilio",
        provider_message_id="SM1", status="sent", detail="", context="manual", client_id=None,
        created_at=now - timedelta(hours=1), delivered_at=None,
    )
    db = _db()
    calls = {"n": 0}

    async def execute(_stmt):
        calls["n"] += 1
        return _Result([sms] if calls["n"] == 1 else [newer, older])

    db.execute = execute
    with patch.object(audit_feed, "_actor_names", AsyncMock(return_value={})):
        rows, total = await audit_feed.list_messages(db, _user(Role.SUPER_ADMIN))
    assert total == 3
    assert [r.subject for r in rows] == ["newer", None, "older"]


# --- SES events ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delivery_event_advances_the_row():
    row = _send(status="sent")
    db = _db(row)
    assert await outbox.mark_delivery(db, provider_message_id="SES-1", event="Delivery")
    assert row.status == "delivered" and row.delivered_at is not None


@pytest.mark.asyncio
async def test_a_late_delivery_never_undoes_a_bounce():
    row = _send(status="bounced")
    db = _db(row)
    await outbox.mark_delivery(db, provider_message_id="SES-1", event="Delivery")
    assert row.status == "bounced"


@pytest.mark.asyncio
async def test_a_bounce_is_terminal_and_keeps_its_reason():
    row = _send(status="delivered")
    db = _db(row)
    await outbox.mark_delivery(
        db, provider_message_id="SES-1", event="Bounce", detail="Permanent/NoEmail"
    )
    assert row.status == "bounced" and "Permanent" in row.detail


@pytest.mark.asyncio
async def test_an_open_dates_the_row_without_becoming_its_status():
    """Image-blocking makes an unopened message indistinguishable from an
    unloaded pixel, so an open is a hint, never a state."""
    row = _send(status="delivered")
    db = _db(row)
    await outbox.mark_delivery(db, provider_message_id="SES-1", event="Open")
    assert row.status == "delivered" and row.opened_at is not None


@pytest.mark.asyncio
async def test_an_unknown_event_or_id_changes_nothing():
    assert await outbox.mark_delivery(_db(_send()), provider_message_id="X", event="Send") is False
    assert await outbox.mark_delivery(_db(None), provider_message_id="X", event="Bounce") is False


# --- the webhook's front door ---------------------------------------------------------


def test_only_amazon_may_supply_the_signing_certificate():
    """The certificate URL arrives inside the payload we are trying to
    authenticate, so this check is what stops an attacker signing their own."""
    assert sns._cert_url_is_amazon("https://sns.us-east-1.amazonaws.com/c.pem") is True
    assert sns._cert_url_is_amazon("http://sns.us-east-1.amazonaws.com/c.pem") is False
    assert sns._cert_url_is_amazon("https://evil.example.com/c.pem") is False
    assert sns._cert_url_is_amazon("https://amazonaws.com.evil.example.com/c.pem") is False
    assert sns._cert_url_is_amazon("") is False


@pytest.mark.asyncio
async def test_an_unverified_message_never_reaches_the_ledger():
    assert await sns.verify({"Type": "Notification", "SigningCertURL": "https://evil.com/c.pem"}) is False


def test_the_canonical_string_is_the_signed_field_order():
    out = sns._canonical(
        {"Type": "Notification", "MessageId": "m", "Message": "b", "Timestamp": "t", "TopicArn": "a"}
    )
    assert out == b"Message\nb\nMessageId\nm\nTimestamp\nt\nTopicArn\na\nType\nNotification\n"


def test_the_webhook_is_registered():
    from app.routers.webhooks import router

    paths = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/webhooks/ses", "POST") in paths


def test_the_audit_routes_are_registered():
    from app.routers.communications_audit import router

    paths = {r.path for r in router.routes}
    assert "/admin/communications/messages" in paths
    assert "/admin/communications/activity" in paths
