"""The soft pull is part of the process, not a second button.

Franco: "people are doing the soft pull initially in the process and we are
pulling the report into our platform" — and "if not done we do it for the
clients during the chat leading to it."
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import credit_pull_core
from app.services.collection_tasks import _render_request_message


def _pull(fico=701, notes="isoftpull_id=abc; fico_source=full_feed", response=None):
    return SimpleNamespace(
        id=uuid4(), fico=fico, notes=notes, bureau_response=response or {},
        pulled_at=datetime(2026, 11, 3, 15, 33, tzinfo=UTC), expires_at=datetime(2027, 2, 1, tzinfo=UTC),
    )


def _intake(variant="main_street_v1", state=None):
    return SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), client_id=uuid4(), variant=variant, intake_state=dict(state or {}),
        business_name="Body Good Studio LLC", full_name="Linda Louis",
    )


APPLICANT = {"legal_first_name": "Linda", "legal_last_name": "Louis", "dob": "1980-05-30", "street": "1 Main", "city": "Miami", "state": "FL", "zip": "33101"}


def test_the_score_source_and_the_stand_in_are_read_from_the_notes() -> None:
    assert credit_pull_core.fico_source_of(_pull()) == "full_feed"
    assert credit_pull_core.is_proxy_score(_pull(notes="isoftpull_id=x; fico_source=intelligence-proxy")) is True
    assert credit_pull_core.fico_source_of(_pull(notes=None)) is None


def test_status_extras_say_when_the_number_is_not_a_bureau_score() -> None:
    real = credit_pull_core.credit_status_extras(_pull(response={"report_s3_key": "k"}))
    assert real["score_verified"] is True and real["report_available"] is True and real["attention"] is None
    proxy = credit_pull_core.credit_status_extras(_pull(fico=720, notes="fico_source=intelligence-proxy"))
    assert proxy["score_verified"] is False and "stand-in" in proxy["attention"]
    none = credit_pull_core.credit_status_extras(_pull(fico=None, notes="isoftpull_id=x"))
    assert none["score_verified"] is False and "not been imported" in none["attention"]
    assert credit_pull_core.credit_status_extras(None)["report_available"] is False


@pytest.mark.asyncio
async def test_signing_runs_the_pull_and_records_it_on_the_intake() -> None:
    intake = _intake()
    pull = _pull()
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    with (
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(return_value=pull)) as run,
        patch.object(credit_pull_core, "stamp_primary_owner", AsyncMock(return_value=True)) as stamp,
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)

    assert record["status"] == "completed" and record["fico"] == 701 and record["fico_source"] == "full_feed"
    assert intake.intake_state["credit_pull"]["pull_id"] == str(pull.id)
    applicant = run.await_args.kwargs["applicant"]
    assert applicant.dob == date(1980, 5, 30) and applicant.ssn is None
    stamp.assert_awaited_once()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_no_hit_asks_the_client_for_the_ssn_instead_of_failing() -> None:
    intake = _intake()
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    denied = credit_pull_core.SoftPullDenied("no_hit_provide_ssn", "no match")
    with (
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(side_effect=denied)),
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)

    assert record["status"] == "needs_ssn"
    assert intake.intake_state["credit_pull"]["status"] == "needs_ssn"
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bureau_failure_or_a_scoreless_pull_goes_to_the_desk() -> None:
    intake = _intake()
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    with (
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(side_effect=credit_pull_core.SoftPullUnavailable("down"))),
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)
    assert record["status"] == "failed" and record["error_code"] == "SoftPullUnavailable"
    alert.assert_awaited_once()

    intake = _intake()
    with (
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(return_value=_pull(fico=None, notes="isoftpull_id=x"))),
        patch.object(credit_pull_core, "stamp_primary_owner", AsyncMock(return_value=False)),
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)
    assert record["status"] == "completed" and record["fico"] is None
    assert "no score" in alert.await_args.kwargs["title"]


@pytest.mark.asyncio
async def test_a_manual_review_mca_file_defers_to_the_desk_and_a_done_pull_is_not_repeated() -> None:
    intake = _intake(variant="mca_refi_v1", state={"mca_qualification": {"active_mca_count": 3, "requested_amount": 50_000}})
    db = SimpleNamespace(get=AsyncMock())
    with (
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock()) as run,
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)
    assert record["status"] == "deferred" and record["reason"] == "manual_review"
    run.assert_not_awaited()
    alert.assert_awaited_once()

    done = _intake(state={"credit_pull": {"pull_id": "abc", "fico": 700}})
    with patch.object(credit_pull_core, "run_soft_pull", AsyncMock()) as run:
        record = await credit_pull_core.auto_pull_after_signature(db, done, applicant_data=APPLICANT)
    assert record["pull_id"] == "abc"
    run.assert_not_awaited()


def test_the_request_template_shows_the_label_not_the_braces() -> None:
    assert _render_request_message("Please provide {label}.", label="Current advance terms or payoff letters") == "Please provide Current advance terms or payoff letters."
    assert _render_request_message(None, label="x") is None
    assert _render_request_message("Collect the statements.", label=None) == "Collect the statements."
