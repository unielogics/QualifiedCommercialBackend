import json

import pytest

from app.request_context import RequestContextMiddleware
from app.services import file_loading_metrics

FILE = "123e4567-e89b-12d3-a456-426614174000"


@pytest.mark.parametrize(("path", "label"), [
    (f"/api/v1/admin/ai-underwriter-leads/{FILE}", "lead.detail"),
    (f"/api/v1/application-profiles/{FILE}/financial-cockpit", "profile.financial_cockpit"),
    (f"/api/v1/application-profiles/{FILE}/underwriting", "profile.underwriting"),
    (f"/api/v1/loans/{FILE}/documents", "loan.documents"),
    (f"/api/v1/application-profiles/{FILE}/evidence/customer-name", None),
    ("/api/v1/admin/ai-underwriter-leads/customer@example.com", None),
    ("/api/v1/auth/me", None),
])
def test_route_labels_never_contain_customer_identifiers(path, label):
    assert file_loading_metrics.route_name(path, "GET") == label
    assert file_loading_metrics.route_name(path, "POST") is None


@pytest.mark.asyncio
async def test_file_timing_preserves_body_headers_and_records_only_bounded_fields(monkeypatch):
    events = []
    monkeypatch.setattr(file_loading_metrics, "emit", lambda **fields: events.append(fields))
    ticks = iter([1.0, 1.125, 1.25])
    monkeypatch.setattr("app.request_context.perf_counter", lambda: next(ticks))

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"cache-control", b"private, no-store"), (b"server-timing", b"db;dur=100")]})
        await send({"type": "http.response.body", "body": b'{"ok":', "more_body": True})
        await send({"type": "http.response.body", "body": b'true}'})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    await RequestContextMiddleware(downstream)({"type": "http", "method": "GET",
        "path": f"/api/v1/admin/ai-underwriter-leads/{FILE}",
        "query_string": b"private-client-note=do-not-log", "headers": []}, receive, send)
    assert b"".join(message.get("body", b"") for message in sent) == b'{"ok":true}'
    assert (b"cache-control", b"private, no-store") in sent[0]["headers"]
    assert (b"server-timing", b"db;dur=100") in sent[0]["headers"]
    assert any(key == b"server-timing" and b"125.00" in value for key, value in sent[0]["headers"])
    assert events == [{"route": "lead.detail", "method": "GET", "status_code": 200,
                       "duration_ms": 250.0, "ttfb_ms": 125.0, "response_bytes": 11}]
    assert FILE not in str(events) and "do-not-log" not in str(events)


def test_metric_log_has_no_actor_raw_path_or_payload(caplog):
    caplog.set_level("INFO", logger="qc.file.metrics")
    file_loading_metrics.emit(route="lead.detail", method="GET", status_code=200,
        duration_ms=15.5, ttfb_ms=14.0, response_bytes=1024)
    event = json.loads(caplog.records[-1].message)
    assert event["event"] == "file_loading_metric"
    assert event["value"] == 15.5
    assert set(event) == {"event", "metric", "unit", "route", "method", "status_code", "value", "ttfb_ms", "response_bytes"}
