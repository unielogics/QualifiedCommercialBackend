"""A send that failed must not read as a send that landed.

Two places said otherwise. `notifications` stamped `emailed_at` next to a
fire-and-forget task whose result it threw away, so a refused or bounced email
was recorded as a delivered one on every one of its callers. And the SMS
ledger's `mark_delivery` — which is careful, and refuses to let a late "sent"
event undo a "delivered" one — had no callers at all, so every outbound text sat
at "sent" forever while the same webhook updated two other tables.

An audit page built on either of those would have published the lie rather than
ended it.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import notifications
from app.services.email.ses_client import SesSendResult
from app.services.sms import ledger


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _db(row=None):
    db = SimpleNamespace()
    db.execute = AsyncMock(return_value=_Result(row))
    db.flush = AsyncMock()
    return db


def _sms(**over):
    row = SimpleNamespace(
        id=uuid4(), direction="outbound", status="sent", detail="",
        delivered_at=None, provider_message_id="SM123",
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


# --- the notification email ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_send_reports_itself_rather_than_returning_none():
    """send_email never raises — it returns ok=False. The old best-effort
    wrapper only caught exceptions, so it could not see a refusal at all."""
    refused = SesSendResult(False, None, "send_failed: address suppressed")
    with patch.object(notifications, "send_email", return_value=refused):
        out = await notifications._send_notification_email(
            "dana@example.com", subject="Hi", body="Body"
        )
    assert out.ok is False
    assert "suppressed" in out.detail


@pytest.mark.asyncio
async def test_a_raising_transport_still_returns_a_result():
    with patch.object(notifications, "send_email", side_effect=RuntimeError("boom")):
        out = await notifications._send_notification_email("d@example.com", subject="s", body="b")
    assert out.ok is False and "boom" in out.detail


def test_emailed_at_is_stamped_only_on_a_successful_send():
    """The ordering is the whole defect: emailed_at used to be set
    unconditionally, immediately after scheduling a task nobody awaited."""
    source = inspect.getsource(notifications.notify_users)
    assert "_send_notification_email" in source
    assert "asyncio.create_task(\n                _send_email_best_effort" not in source
    # Stamped inside the success branch, and the reason kept on failure.
    assert "if sent.ok:" in source
    assert "row.emailed_at = now" in source
    assert "email_error" in source
    assert source.index("sent = await _send_notification_email") < source.index("if sent.ok:")


# --- the SMS ledger ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delivery_event_advances_the_row_and_dates_it():
    row = _sms()
    assert await ledger.mark_delivery(_db(row), provider_message_id="SM123", status="delivered")
    assert row.status == "delivered" and row.delivered_at is not None


@pytest.mark.asyncio
async def test_a_late_sent_event_never_undoes_a_delivery():
    row = _sms(status="delivered")
    await ledger.mark_delivery(_db(row), provider_message_id="SM123", status="sent")
    assert row.status == "delivered"


@pytest.mark.asyncio
async def test_a_carrier_failure_overwrites_a_delivery():
    """The one state that must win late: a rejection can only arrive after the
    fact, and it is the truer answer."""
    row = _sms(status="delivered")
    await ledger.mark_delivery(
        _db(row), provider_message_id="SM123", status="failed", detail="twilio 30006: unreachable"
    )
    assert row.status == "failed"
    assert "30006" in row.detail


@pytest.mark.asyncio
async def test_a_failure_never_regresses_to_sent():
    row = _sms(status="failed")
    await ledger.mark_delivery(_db(row), provider_message_id="SM123", status="sent")
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_an_event_for_an_unknown_id_is_not_an_error():
    assert await ledger.mark_delivery(_db(None), provider_message_id="SM404", status="delivered") is False
    assert await ledger.mark_delivery(_db(), provider_message_id="", status="delivered") is False


# --- the webhook that had no caller ------------------------------------------------


def test_the_twilio_status_webhook_now_advances_the_ledger():
    from app.routers import webhooks

    source = inspect.getsource(webhooks.twilio_sms_status)
    assert "sms_ledger.mark_delivery" in source, "the ledger is still not updated"
    # It updates the ledger as well as the two tables it already touched.
    assert "DealerRepInboxMessage" in source and "BookingNotificationReminder" in source


def test_every_twilio_terminal_state_maps_onto_a_ledger_state():
    from app.routers.webhooks import _LEDGER_STATUS

    assert _LEDGER_STATUS["delivered"] == "delivered"
    assert _LEDGER_STATUS["read"] == "delivered"
    assert {_LEDGER_STATUS[k] for k in ("failed", "undelivered", "canceled")} == {"failed"}
    # In-flight states are deliberately absent: the ledger already says "sent".
    assert "queued" not in _LEDGER_STATUS and "sending" not in _LEDGER_STATUS
