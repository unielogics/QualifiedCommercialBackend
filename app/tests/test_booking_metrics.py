from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from app import request_context
from app.dealer_os import router as dealer_router
from app.services import booking_metrics
from app.services.email import ses_client


def test_booking_metric_route_names_are_low_cardinality() -> None:
    prospect_id = "123e4567-e89b-12d3-a456-426614174000"
    assert booking_metrics.route_name(
        f"/api/v1/dealer-os/prospects/{prospect_id}/appointments"
    ) == "/api/v1/dealer-os/prospects/:id/appointments"
    assert booking_metrics.route_name(
        "/api/v1/public/book/jonathan-franco/availability"
    ) == "/api/v1/public/book/:slug/availability"
    assert booking_metrics.route_name(
        "/api/v1/public/booking/franco"
    ) == "/api/v1/public/booking/:slug"
    assert booking_metrics.route_name("/api/v1/dealer-os/dealers") is None


def test_booking_metric_envelope_carries_request_correlation(caplog) -> None:
    caplog.set_level("INFO", logger="qc.booking.metrics")
    with request_context.bind(request_id="request-123", actor_label="user"):
        booking_metrics.emit(
            "booking.provider.timeout",
            provider="google",
            operation="calendar_write",
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "booking_metric"
    assert payload["request_id"] == "request-123"
    assert payload["provider"] == "google"


@pytest.mark.asyncio
async def test_request_context_emits_booking_http_error_metric(monkeypatch) -> None:
    emitted: list[tuple[str, float | int, dict]] = []

    def capture(metric: str, value: float | int = 1, **dimensions) -> None:
        emitted.append((metric, value, dimensions))

    monkeypatch.setattr(booking_metrics, "emit", capture)

    async def downstream(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 504, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = request_context.RequestContextMiddleware(downstream)
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/dealer-os/appointments",
            "headers": [(b"x-request-id", b"booking-504")],
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 504
    assert [item[0] for item in emitted] == [
        "booking.http.duration",
        "booking.http.error",
    ]
    assert emitted[-1][2]["status_code"] == 504


@pytest.mark.parametrize(
    ("failure", "expected_metric"),
    [
        (TimeoutError("SES read timeout"), "booking.provider.timeout"),
        (RuntimeError("SES rejected request"), "booking.provider.error"),
    ],
)
def test_ses_send_classifies_timeout_and_other_provider_errors(
    monkeypatch, failure: Exception, expected_metric: str
) -> None:
    settings = SimpleNamespace(
        ses_from_address="no-reply@qualifiedcommercial.com",
        ses_region="us-east-1",
        ses_configuration_set="",
    )
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr(ses_client, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ses_client,
        "_ses_client",
        lambda _region: SimpleNamespace(send_email=Mock(side_effect=failure)),
    )
    monkeypatch.setattr(
        ses_client.booking_metrics,
        "emit",
        lambda metric, *_args, **dimensions: emitted.append((metric, dimensions)),
    )

    result = ses_client.send_email(
        to_email="dealer@example.com",
        subject="Appointment",
        body_text="Body",
    )

    assert result.ok is False
    assert emitted == [
        (expected_metric, {"provider": "ses", "operation": "send_email"})
    ]


def test_availability_metrics_report_slots_date_coverage_and_fail_closed(
    monkeypatch,
) -> None:
    emitted: list[tuple[str, float | int, dict]] = []
    monkeypatch.setattr(
        dealer_router.booking_metrics,
        "emit",
        lambda metric, value=1, **dimensions: emitted.append(
            (metric, value, dimensions)
        ),
    )
    page = SimpleNamespace(
        page_start_date=date(2026, 9, 23),
        page_end_date=date(2026, 9, 25),
    )
    slots = [
        SimpleNamespace(starts_at=datetime(2026, 9, 23, 14, 0, tzinfo=UTC)),
        SimpleNamespace(starts_at=datetime(2026, 9, 23, 15, 0, tzinfo=UTC)),
        SimpleNamespace(starts_at=datetime(2026, 9, 25, 14, 0, tzinfo=UTC)),
    ]

    dealer_router._emit_booking_availability_metrics(
        page=page,
        slots=slots,
        calendar_state="connected",
        zone=ZoneInfo("America/New_York"),
    )

    assert [item[:2] for item in emitted] == [
        ("booking.availability.slots", 3),
        ("booking.availability.date_coverage", 2),
        ("booking.availability.fail_closed", 0),
    ]
    assert all(item[2]["requested_dates"] == 3 for item in emitted[:2])
    assert all(item[2]["calendar_state"] == "connected" for item in emitted)

    emitted.clear()
    dealer_router._emit_booking_availability_metrics(
        page=page,
        slots=[],
        calendar_state="unavailable",
        zone=ZoneInfo("America/New_York"),
    )
    assert emitted[-1] == (
        "booking.availability.fail_closed",
        1,
        {"calendar_state": "unavailable"},
    )
