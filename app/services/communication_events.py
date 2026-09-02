"""Transaction-aware communication change events for Field Desk.

Message rows and notifications remain the source of truth. PostgreSQL NOTIFY
only tells connected browsers which scoped query became stale; payloads never
contain message bodies or contact details.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

log = logging.getLogger(__name__)

CHANNEL = "qc_communication_events"
HEARTBEAT_SECONDS = 25
MAX_QUEUE_SIZE = 100


def user_audience(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}"


def _event_payload(
    *,
    event_type: str,
    audiences: Iterable[str],
    dealer_id: uuid.UUID | str | None = None,
    thread_id: uuid.UUID | str | None = None,
    message_id: uuid.UUID | str | None = None,
    notification_id: uuid.UUID | str | None = None,
    channel: str | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "audiences": sorted(set(audiences)),
        "dealer_id": str(dealer_id) if dealer_id else None,
        "thread_id": str(thread_id) if thread_id else None,
        "message_id": str(message_id) if message_id else None,
        "notification_id": str(notification_id) if notification_id else None,
        "channel": channel,
        "direction": direction,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


class CommunicationEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._listener_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.last_event_at: datetime | None = None

    async def start(self) -> None:
        if self._listener_task and not self._listener_task.done():
            return
        self._stop.clear()
        self._listener_task = asyncio.create_task(
            self._listen_forever(), name="communication-event-listener"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
        self.connected = False

    @asynccontextmanager
    async def subscribe(self, audiences: Iterable[str]) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        keys = set(audiences)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        for key in keys:
            self._subscribers[key].add(queue)
        try:
            yield queue
        finally:
            for key in keys:
                subscribers = self._subscribers.get(key)
                if subscribers is None:
                    continue
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)

    def dispatch(self, event: dict[str, Any]) -> None:
        self.last_event_at = datetime.now(UTC)
        queues: set[asyncio.Queue[dict[str, Any]]] = set()
        for audience in event.get("audiences") or []:
            queues.update(self._subscribers.get(str(audience), set()))
        for queue in queues:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                replacement = {
                    "id": str(uuid.uuid4()),
                    "type": "sync.required",
                    "occurred_at": datetime.now(UTC).isoformat(),
                }
                queue.put_nowait(replacement)
            else:
                queue.put_nowait(event)

    def broadcast_sync_required(self) -> None:
        queues = {queue for subscribers in self._subscribers.values() for queue in subscribers}
        event = {
            "id": str(uuid.uuid4()),
            "type": "sync.required",
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        for queue in queues:
            if not queue.full():
                queue.put_nowait(event)

    async def _listen_forever(self) -> None:
        delay = 1
        while not self._stop.is_set():
            connection: asyncpg.Connection | None = None
            try:
                url = make_url(get_settings().database_url).set(drivername="postgresql")
                connection = await asyncpg.connect(url.render_as_string(hide_password=False))

                def receive(_connection, _pid, _channel, payload: str) -> None:
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("communication event listener received invalid JSON")
                        return
                    if isinstance(event, dict):
                        self.dispatch(event)

                await connection.add_listener(CHANNEL, receive)
                self.connected = True
                self.broadcast_sync_required()
                delay = 1
                while not self._stop.is_set() and not connection.is_closed():
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.connected = False
                log.exception("communication event listener disconnected")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self.connected = False
                if connection and not connection.is_closed():
                    try:
                        await connection.remove_listener(CHANNEL, receive)
                    except Exception:  # noqa: BLE001
                        pass
                    await connection.close()


broker = CommunicationEventBroker()


async def publish_communication_event(
    db: AsyncSession,
    *,
    recipient_user_ids: Iterable[uuid.UUID | str],
    event_type: str,
    dealer_id: uuid.UUID | str | None = None,
    thread_id: uuid.UUID | str | None = None,
    message_id: uuid.UUID | str | None = None,
    notification_id: uuid.UUID | str | None = None,
    channel: str | None = None,
    direction: str | None = None,
) -> None:
    audiences = [user_audience(value) for value in recipient_user_ids if value]
    if not audiences:
        return
    payload = _event_payload(
        event_type=event_type,
        audiences=audiences,
        dealer_id=dealer_id,
        thread_id=thread_id,
        message_id=message_id,
        notification_id=notification_id,
        channel=channel,
        direction=direction,
    )
    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        broker.dispatch(payload)
        return
    bind = get_bind()
    if bind.dialect.name != "postgresql":
        broker.dispatch(payload)
        return
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": json.dumps(payload, separators=(",", ":"))},
    )
