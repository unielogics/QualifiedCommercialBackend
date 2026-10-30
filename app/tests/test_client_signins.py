"""The client-access directory counts the sign-ins that never had a login account.

Most clients enter their secure room with the PIN on the upload link, and
dealer leads sign in with an emailed one-time code. Both were already
recorded; the directory used to ignore them and call every such person
"No login".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.enums import ProductAccountType
from app.routers.client_access import (
    ClientAccessDirectoryRow,
    _login_state,
    _matches_filters,
)
from app.services import client_signins
from app.services.client_signins import SigninSummary

NOW = datetime(2026, 10, 28, 20, 57, tzinfo=UTC)
EARLIER = NOW - timedelta(days=2)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, *results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self._results:
            raise AssertionError("more queries than the test expected")
        return self._results.pop(0)


# --- the summary arithmetic --------------------------------------------------


def test_merging_summaries_adds_counts_and_keeps_the_latest_time() -> None:
    merged = client_signins.merge(
        [SigninSummary(2, EARLIER), None, SigninSummary(1, NOW), SigninSummary(0, None)]
    )
    assert merged == SigninSummary(3, NOW)


def test_summary_for_folds_every_room_and_code_of_one_subject_once() -> None:
    bucket_a, bucket_b, intake = uuid4(), uuid4(), uuid4()
    room = {bucket_a: SigninSummary(3, EARLIER), bucket_b: SigninSummary(1, NOW)}
    code = {intake: SigninSummary(2, EARLIER)}
    summary = client_signins.summary_for(
        # The same bucket reached through two profiles still counts once.
        bucket_ids=[bucket_a, bucket_a, bucket_b, None],
        intake_ids=[intake, None],
        room=room,
        code=code,
    )
    assert summary == SigninSummary(6, NOW)


def test_a_subject_with_no_recorded_entries_is_empty() -> None:
    assert client_signins.summary_for(
        bucket_ids=[uuid4()], intake_ids=[uuid4()], room={}, code={}
    ) == client_signins.EMPTY


def test_latest_ignores_missing_values() -> None:
    assert client_signins.latest(None, EARLIER, NOW, None) == NOW
    assert client_signins.latest(None, None) is None


# --- how the directory reads them -------------------------------------------


def _user(*, clerk_id="clerk_1", account_status="active", last_invite_status=None):
    return SimpleNamespace(
        clerk_id=clerk_id, account_status=account_status, last_invite_status=last_invite_status
    )


def test_a_person_without_an_account_who_used_the_pin_is_not_no_login() -> None:
    assert _login_state(None) == "no_login"
    assert _login_state(None, SigninSummary(1, NOW)) == "passcode"


def test_an_account_keeps_its_own_state_even_when_the_pin_was_also_used() -> None:
    assert _login_state(_user(), SigninSummary(4, NOW)) == "active"
    assert _login_state(_user(clerk_id=None), SigninSummary(4, NOW)) == "invited"
    assert _login_state(_user(account_status="suspended"), SigninSummary(4, NOW)) == "suspended"


def test_the_login_filter_can_pick_out_passcode_clients() -> None:
    row = ClientAccessDirectoryRow(
        subject_kind="client",
        subject_id=uuid4(),
        client_name="Bryson Mummert",
        origin="mca_refinance",
        login_state="passcode",
        account_types=[],
        file_count=1,
        last_login_at=NOW,
        passcode_signin_count=1,
        last_passcode_signin_at=NOW,
        status="passcode_access",
    )
    assert _matches_filters(row, source=None, login_state="passcode", account_type=None)
    assert not _matches_filters(row, source=None, login_state="no_login", account_type=None)
    assert _matches_filters(row, source=None, login_state=None, account_type="none")


def test_rows_default_to_no_passcode_sign_ins() -> None:
    row = ClientAccessDirectoryRow(
        subject_kind="intake",
        subject_id=uuid4(),
        client_name="Lead",
        origin="public_intake",
        login_state="no_login",
        account_types=[ProductAccountType.FUNDING],
        status="new",
    )
    assert row.passcode_signin_count == 0
    assert row.last_passcode_signin_at is None
    assert row.last_login_at is None


# --- reading the recorded entries -------------------------------------------


@pytest.mark.asyncio
async def test_room_sign_ins_are_grouped_per_bucket() -> None:
    bucket = uuid4()
    db = _Session(_Result([(bucket, 3, NOW)]))
    assert await client_signins.room_signins_by_bucket(db, [bucket, None]) == {
        bucket: SigninSummary(3, NOW)
    }
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_no_buckets_means_no_query() -> None:
    db = _Session()
    assert await client_signins.room_signins_by_bucket(db, []) == {}
    assert await client_signins.code_signins_by_intake(db, [None]) == {}
    assert db.statements == []


@pytest.mark.asyncio
async def test_sign_in_history_mixes_room_and_code_entries_newest_first() -> None:
    bucket, intake = uuid4(), uuid4()
    room_rows = [
        SimpleNamespace(
            id=uuid4(), bucket_id=bucket, action="upload_link_accessed",
            created_at=EARLIER, ip_address="203.0.113.9", user_agent="Safari",
        ),
        SimpleNamespace(
            id=uuid4(), bucket_id=bucket, action="upload_passcode_failed",
            created_at=EARLIER - timedelta(minutes=1), ip_address="203.0.113.9", user_agent="Safari",
        ),
    ]
    code_rows = [
        SimpleNamespace(
            id=uuid4(), intake_id=intake, used_at=NOW, ip_address="198.51.100.4", user_agent="Chrome",
        )
    ]
    db = _Session(_Result(room_rows), _Result(code_rows))
    events = await client_signins.signin_events(db, bucket_ids=[bucket], intake_ids=[intake])
    assert [event.action for event in events] == [
        "email_code_signin",
        "room_pin_signin",
        "room_pin_failed",
    ]
    assert events[0].request_metadata["intake_id"] == str(intake)
    assert events[1].request_metadata["ip_address"] == "203.0.113.9"
    assert events[1].reason == "Entered the secure room with the room PIN"


@pytest.mark.asyncio
async def test_sign_in_history_without_subjects_asks_nothing() -> None:
    db = _Session()
    assert await client_signins.signin_events(db, bucket_ids=[None], intake_ids=[]) == []
    assert db.statements == []
