from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.dealer_os import prospect_router
from app.dealer_os.prospect_schemas import ProspectCallAttemptCreate
from app.services import team_calendar


def test_call_attempt_contract_accepts_stable_retry_key() -> None:
    payload = ProspectCallAttemptCreate(
        method="google_voice",
        idempotency_key="call-attempt-0001",
    )
    assert payload.idempotency_key == "call-attempt-0001"
    assert ProspectCallAttemptCreate(method="device_dialer").idempotency_key is None
    with pytest.raises(ValidationError):
        ProspectCallAttemptCreate(method="google_voice", idempotency_key="short")


def test_call_attempt_is_deduplicated_before_activity_insert() -> None:
    source = inspect.getsource(prospect_router.record_prospect_call_attempt)
    lookup = source.index("DealerProspectActivity.metadata_json")
    insert = source.index("service.add_activity")
    assert lookup < insert
    assert '"idempotency_key": payload.idempotency_key' in source
    assert "idempotency_key_reused" in source


def test_shared_booking_settings_never_commits_inside_helper() -> None:
    source = inspect.getsource(team_calendar.team_booking_settings)
    assert "await db.flush()" in source
    assert "await db.commit()" not in source

