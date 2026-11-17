"""A soft pull younger than the reuse window is reused, not repeated."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import credit_links, credit_pull_core

NOW = datetime.now(UTC)
APPLICANT = {"legal_first_name": "Jordan", "legal_last_name": "Sample", "dob": "1980-01-15", "street": "100 Example St", "city": "Charlotte", "state": "NC", "zip": "28202"}


def _pull(days_ago: float, fico: int | None = 712):
    return SimpleNamespace(
        id=uuid4(), fico=fico, notes="isoftpull_id=abc; fico_source=full_feed", bureau_response={},
        pulled_at=NOW - timedelta(days=days_ago), expires_at=NOW + timedelta(days=60),
        legal_first_name="Jordan", legal_last_name="Sample", dob=date(1980, 1, 15), email=None,
        client_id=None, application_owner_id=None,
    )


def _applicant(ssn: str | None = None) -> credit_pull_core.SoftPullApplicant:
    return credit_pull_core.SoftPullApplicant(
        legal_first_name="Jordan", legal_last_name="Sample", dob=date(1980, 1, 15),
        street="100 Example St", city="Charlotte", state="NC", zip="28202", ssn=ssn,
    )


def test_pulled_within_reads_dates_and_strings_and_zero_means_never() -> None:
    assert credit_pull_core.pulled_within(NOW - timedelta(days=29), 30)
    assert not credit_pull_core.pulled_within(NOW - timedelta(days=31), 30)
    assert credit_pull_core.pulled_within((NOW - timedelta(days=2)).isoformat(), 30)
    assert not credit_pull_core.pulled_within(NOW - timedelta(days=2), 0)
    assert not credit_pull_core.pulled_within("not a date", 30)
    assert not credit_pull_core.pulled_within(None, 30)


def test_the_window_setting_defaults_to_thirty_and_tolerates_junk() -> None:
    assert credit_links._window({}) == 30
    assert credit_links._window({"reuse_window_days": 14}) == 14
    assert credit_links._window({"reuse_window_days": 0}) == 0
    for junk in (-1, 400, "30", True, None):
        assert credit_links._window({"reuse_window_days": junk}) == 30


async def test_the_newest_exact_match_inside_the_window_is_reused() -> None:
    older, newer, stranger = _pull(20), _pull(3), _pull(1)
    candidates = [
        credit_links.Candidate(pull=older, evidence={}, level=credit_links.LEVEL_EXACT, linked_here=False, linked_elsewhere=False),
        credit_links.Candidate(pull=newer, evidence={}, level=credit_links.LEVEL_EXACT, linked_here=False, linked_elsewhere=False),
        credit_links.Candidate(pull=stranger, evidence={}, level=credit_links.LEVEL_LIKELY, linked_here=False, linked_elsewhere=False),
    ]
    with patch.object(credit_links, "candidate_pulls", AsyncMock(return_value=candidates)):
        found = await credit_pull_core.reusable_pull(
            None, client_id=uuid4(), first_name="Jordan", last_name="Sample", dob=date(1980, 1, 15), within_days=30
        )
    assert found is newer


async def test_pull_or_reuse_calls_the_bureau_only_when_it_must() -> None:
    client = SimpleNamespace(id=uuid4())
    recent = _pull(5)
    fresh = _pull(0)
    with (
        patch.object(credit_links, "reuse_window_days", AsyncMock(return_value=30)),
        patch.object(credit_pull_core, "reusable_pull", AsyncMock(return_value=recent)),
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(return_value=fresh)) as run,
    ):
        assert await credit_pull_core.pull_or_reuse(None, client=client, applicant=_applicant()) == (recent, True)
        run.assert_not_awaited()
        # An SSN retry after a no-hit always goes to the bureau.
        assert await credit_pull_core.pull_or_reuse(None, client=client, applicant=_applicant(ssn="123456789")) == (fresh, False)
        run.assert_awaited_once()

    with (
        patch.object(credit_links, "reuse_window_days", AsyncMock(return_value=30)),
        patch.object(credit_pull_core, "reusable_pull", AsyncMock(return_value=None)),
        patch.object(credit_pull_core, "run_soft_pull", AsyncMock(return_value=fresh)),
    ):
        assert await credit_pull_core.pull_or_reuse(None, client=client, applicant=_applicant()) == (fresh, False)


def _intake(state=None):
    return SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), client_id=uuid4(), variant="real_estate_dscr_v1", intake_state=dict(state or {}),
        business_name=None, full_name="Jordan Sample", email="jordan@example.com",
    )


@pytest.mark.parametrize(("days", "pulls_again"), [(10, False), (45, True)])
async def test_a_file_already_counting_on_a_pull_repeats_it_only_after_the_window(days: int, pulls_again: bool) -> None:
    intake = _intake({"credit_pull": {"pull_id": "old", "fico": 700, "pulled_at": (NOW - timedelta(days=days)).isoformat()}})
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    with (
        patch.object(credit_links, "reuse_window_days", AsyncMock(return_value=30)),
        patch.object(credit_pull_core, "pull_or_reuse", AsyncMock(return_value=(_pull(0), False))) as pull,
        patch.object(credit_pull_core, "stamp_primary_owner", AsyncMock(return_value=True)),
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()),
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)
    assert pull.await_count == (1 if pulls_again else 0)
    assert (record["pull_id"] != "old") is pulls_again


async def test_a_signature_inside_the_window_links_the_recent_pull_and_says_so() -> None:
    intake = _intake()
    recent = _pull(6)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    with (
        patch.object(credit_links, "reuse_window_days", AsyncMock(return_value=30)),
        patch.object(credit_pull_core, "pull_or_reuse", AsyncMock(return_value=(recent, True))),
        patch.object(credit_pull_core, "stamp_primary_owner", AsyncMock(return_value=True)) as stamp,
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()) as alert,
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()) as activity,
        patch("app.services.file_events.emit", AsyncMock()) as emit,
    ):
        record = await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT)
    assert record["reused"] is True and record["pull_id"] == str(recent.id)
    assert stamp.await_args.kwargs["how"] == credit_links.HOW_REUSED
    assert activity.await_args.args[2] == "credit_pull_reused"
    assert emit.await_args.kwargs["title"].startswith("Using the soft pull from ")
    alert.assert_not_awaited()


async def test_a_window_override_from_the_requirement_is_honoured() -> None:
    intake = _intake({"credit_pull": {"pull_id": "old", "fico": 700, "pulled_at": (NOW - timedelta(days=10)).isoformat()}})
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=intake.client_id)))
    with (
        patch.object(credit_links, "reuse_window_days", AsyncMock(return_value=30)) as setting,
        patch.object(credit_pull_core, "pull_or_reuse", AsyncMock(return_value=(_pull(0), False))) as pull,
        patch.object(credit_pull_core, "stamp_primary_owner", AsyncMock(return_value=True)),
        patch.object(credit_pull_core, "_alert_desk", AsyncMock()),
        patch("app.services.bucket_ai.log_bucket_ai_activity", AsyncMock()),
    ):
        await credit_pull_core.auto_pull_after_signature(db, intake, applicant_data=APPLICANT, within_days=7)
    setting.assert_not_awaited()
    assert pull.await_args.kwargs["within_days"] == 7


def test_reuse_words_name_the_date() -> None:
    pull = _pull(0)
    pull.pulled_at = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    assert credit_pull_core.reuse_words(pull) == "Using the soft pull from Nov 3, 2026"
