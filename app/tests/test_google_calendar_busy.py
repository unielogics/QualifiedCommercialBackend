from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.google import calendar_sync
from app.services.google.google_oauth_client import GoogleNotConnected


class _Request:
    def __init__(self, response: dict) -> None:
        self.response = response

    def execute(self) -> dict:
        return self.response


class _FreeBusy:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.body: dict | None = None

    def query(self, *, body: dict) -> _Request:
        self.body = body
        return _Request(self.response)


class _Service:
    def __init__(self, response: dict) -> None:
        self.resource = _FreeBusy(response)

    def freebusy(self) -> _FreeBusy:
        return self.resource


@pytest.mark.asyncio
async def test_busy_periods_reads_live_primary_google_calendar(monkeypatch) -> None:
    async def credentials(*_args, **_kwargs):
        return object()

    service = _Service(
        {
            "calendars": {
                "primary": {
                    "busy": [
                        {
                            "start": "2026-09-02T14:00:00Z",
                            "end": "2026-09-02T14:45:00Z",
                        }
                    ]
                }
            }
        }
    )
    monkeypatch.setattr(calendar_sync.google_oauth_client, "credentials_for_user", credentials)
    monkeypatch.setattr(calendar_sync, "_service", lambda _creds: service)

    result = await calendar_sync.busy_periods(
        object(),
        uuid4(),
        time_min=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
        time_max=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )

    assert result.status == "connected"
    assert result.intervals == [
        (
            datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
            datetime(2026, 9, 2, 14, 45, tzinfo=UTC),
        )
    ]
    assert service.resource.body == {
        "timeMin": "2026-09-02T00:00:00+00:00",
        "timeMax": "2026-09-03T00:00:00+00:00",
        "items": [{"id": "primary"}],
    }


@pytest.mark.asyncio
async def test_busy_periods_reports_disconnected_without_exposing_provider_error(monkeypatch) -> None:
    async def credentials(*_args, **_kwargs):
        raise GoogleNotConnected("sensitive provider detail")

    monkeypatch.setattr(calendar_sync.google_oauth_client, "credentials_for_user", credentials)

    result = await calendar_sync.busy_periods(
        object(),
        uuid4(),
        time_min=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
        time_max=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )

    assert result.status == "disconnected"
    assert result.intervals == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_metric"),
    [
        (TimeoutError("provider stalled"), "booking.provider.timeout"),
        (RuntimeError("provider rejected request"), "booking.provider.error"),
    ],
)
async def test_busy_periods_classifies_provider_failures(
    monkeypatch, failure: Exception, expected_metric: str
) -> None:
    async def credentials(*_args, **_kwargs):
        return object()

    class FailingRequest:
        def execute(self):
            raise failure

    class FailingFreeBusy:
        def query(self, **_kwargs):
            return FailingRequest()

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(calendar_sync.google_oauth_client, "credentials_for_user", credentials)
    monkeypatch.setattr(
        calendar_sync,
        "_service",
        lambda _credentials: SimpleNamespace(freebusy=lambda: FailingFreeBusy()),
    )
    monkeypatch.setattr(
        calendar_sync.booking_metrics,
        "emit",
        lambda metric, *_args, **dimensions: emitted.append((metric, dimensions)),
    )

    result = await calendar_sync.busy_periods(
        object(),
        uuid4(),
        time_min=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
        time_max=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
    )

    assert result.status == "unavailable"
    assert emitted == [
        (expected_metric, {"provider": "google", "operation": "freebusy"})
    ]


@pytest.mark.asyncio
async def test_calendar_write_timeout_emits_bounded_provider_metric(monkeypatch) -> None:
    async def credentials(*_args, **_kwargs):
        return object()

    class FailingRequest:
        def execute(self):
            raise TimeoutError("write stalled")

    class FailingEvents:
        def insert(self, **_kwargs):
            return FailingRequest()

    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(calendar_sync.google_oauth_client, "credentials_for_user", credentials)
    monkeypatch.setattr(
        calendar_sync,
        "_service",
        lambda _credentials: SimpleNamespace(events=lambda: FailingEvents()),
    )
    monkeypatch.setattr(
        calendar_sync.booking_metrics,
        "emit",
        lambda metric, *_args, **dimensions: emitted.append((metric, dimensions)),
    )
    event = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        google_calendar_id=None,
        google_event_id=None,
        title="Dealer appointment",
        description=None,
        starts_at=datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
        duration_min=20,
        status="pending",
        external_ref_kind="dealer_rep_appointment",
        external_ref_id=str(uuid4()),
    )

    result = await calendar_sync.push_event(AsyncMock(), event)

    assert result is None
    assert emitted == [
        (
            "booking.provider.timeout",
            {"provider": "google", "operation": "calendar_write"},
        )
    ]
