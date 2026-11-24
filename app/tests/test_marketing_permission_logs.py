import logging

import pytest

from app.services.marketing_permission_logs import protect_permission_logs


@pytest.mark.parametrize("action", ["", "/confirm", "/decline"])
def test_permission_capability_redacted_from_both_http_log_formats(action):
    protect_permission_logs()
    protect_permission_logs()
    prefix = "/api/v1/public/email-marketing/permission/"
    path = prefix + "example-signed-capability" + action
    for name, msg, args in (
        ("uvicorn.access", '%s - "%s %s HTTP/%s" %d', ("local", "POST", path + "?ignored=private", "1.1", 200)),
        ("qc", "%s %s: transaction aborted (SQLSTATE %s) after retries; answering 503", ("POST", path, "40001")),
    ):
        record = logging.getLogger(name).makeRecord(name, logging.INFO, "", 0, msg, args, None)
        assert logging.getLogger(name).filter(record)
        rendered = record.getMessage()
        assert prefix + "[redacted]" + action in rendered
        assert "example-signed-capability" not in rendered and "ignored=private" not in rendered


def test_ordinary_http_route_keeps_existing_log_format():
    protect_permission_logs()
    logger = logging.getLogger("uvicorn.access")
    record = logger.makeRecord(logger.name, logging.INFO, "", 0, "%s", ("/api/v1/ready",), None)
    assert logger.filter(record) and record.getMessage() == "/api/v1/ready"
