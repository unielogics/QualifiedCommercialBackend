from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.routers import webhooks
from app.services.communication_events import (
    _event_payload,
    broker,
    publish_communication_event,
    publish_ephemeral,
    sheet_audience,
    user_audience,
)
from app.services.email import inbound_poller, user_inbox_sync


@pytest.mark.asyncio
async def test_event_broker_delivers_only_to_target_user() -> None:
    first_id = uuid4()
    second_id = uuid4()
    event = _event_payload(
        event_type="message.created",
        audiences=[user_audience(first_id)],
        thread_id=uuid4(),
    )

    async with broker.subscribe([user_audience(first_id)]) as first:
        async with broker.subscribe([user_audience(second_id)]) as second:
            broker.dispatch(event)
            assert (await asyncio.wait_for(first.get(), timeout=0.1))["id"] == event["id"]
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(second.get(), timeout=0.01)


def test_event_payload_contains_ids_but_no_message_content() -> None:
    payload = _event_payload(
        event_type="message.created",
        audiences=[user_audience(uuid4())],
        dealer_id=uuid4(),
        thread_id=uuid4(),
        message_id=uuid4(),
        channel="sms",
        direction="inbound",
    )

    assert payload["type"] == "message.created"
    assert payload["message_id"]
    assert "body" not in payload
    assert "phone" not in payload
    assert "email" not in payload


@pytest.mark.asyncio
async def test_non_postgres_publish_uses_local_broker() -> None:
    user_id = uuid4()

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("SQLite fallback must not call pg_notify")

    async with broker.subscribe([user_audience(user_id)]) as queue:
        await publish_communication_event(
            FakeSession(),  # type: ignore[arg-type]
            recipient_user_ids={user_id},
            event_type="notification.created",
        )
        assert (await asyncio.wait_for(queue.get(), timeout=0.1))["type"] == "notification.created"


@pytest.mark.asyncio
async def test_gmail_push_refreshes_lender_and_client_inboxes(monkeypatch) -> None:
    calls: list[str] = []

    async def lender_refresh() -> None:
        calls.append("lender")

    async def client_refresh() -> dict[str, int]:
        calls.append("client")
        return {"ingested": 0}

    monkeypatch.setattr(inbound_poller, "run_inbound_poll", lender_refresh)
    monkeypatch.setattr(user_inbox_sync, "run_user_inbox_sync", client_refresh)

    await webhooks._triggered_poll()

    assert set(calls) == {"lender", "client"}


def test_sheet_audience_is_one_key_per_worksheet_and_sheet() -> None:
    worksheet_id = uuid4()

    assert sheet_audience(worksheet_id, "p_and_l") == f"sheet:{worksheet_id}:p_and_l"
    assert sheet_audience(str(worksheet_id), "pfs") == f"sheet:{worksheet_id}:pfs"
    # The PFS and the P&L of one worksheet are different audiences — a link
    # that opens one must not be able to hear the other.
    assert sheet_audience(worksheet_id, "p_and_l") != sheet_audience(worksheet_id, "pfs")
    assert not sheet_audience(worksheet_id, "pfs").startswith("user:")


@pytest.mark.asyncio
async def test_sheet_audiences_isolate_the_pfs_from_the_p_and_l() -> None:
    worksheet_id = uuid4()
    event = _event_payload(event_type="cell.changed", audiences=[sheet_audience(worksheet_id, "pfs")])

    async with broker.subscribe([sheet_audience(worksheet_id, "p_and_l")]) as p_and_l:
        async with broker.subscribe([sheet_audience(worksheet_id, "pfs")]) as pfs:
            broker.dispatch(event)
            assert (await asyncio.wait_for(pfs.get(), timeout=0.1))["id"] == event["id"]
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(p_and_l.get(), timeout=0.01)


@pytest.mark.asyncio
async def test_publish_ephemeral_dispatches_in_process_without_a_session() -> None:
    worksheet_id = uuid4()
    audience = sheet_audience(worksheet_id, "debt_schedule")
    cursor = {
        "id": str(uuid4()),
        "type": "presence.cursor",
        "audiences": [audience],
        "data": {"participant_id": "p_1", "sheet_kind": "debt_schedule", "editing": True},
    }

    async with broker.subscribe([audience]) as here:
        async with broker.subscribe([sheet_audience(worksheet_id, "pfs")]) as elsewhere:
            publish_ephemeral(cursor)
            got = await asyncio.wait_for(here.get(), timeout=0.1)
            assert got["type"] == "presence.cursor"
            assert got["data"]["participant_id"] == "p_1"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(elsewhere.get(), timeout=0.01)


def test_publish_ephemeral_is_documented_as_single_worker_only() -> None:
    # The failure at two workers is silent, so the warning has to live at the
    # definition, where the next person will read it.
    doc = publish_ephemeral.__doc__ or ""
    assert "--workers 1" in doc
    assert "Dockerfile" in doc
