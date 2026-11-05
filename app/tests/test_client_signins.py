"""The client-access directory counts the sign-ins that never had a login account.

Most clients enter their secure room with the PIN on the upload link, and
dealer leads sign in with an emailed one-time code. Both were already
recorded; the directory used to ignore them and call every such person
"No login". A room can hold links for more than one person, so an entry is
the subject's own only when the link carried their email (or no email).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.enums import ProductAccountType
from app.routers.client_access import (
    AccessHistoryItem,
    ClientAccessDirectoryRow,
    _login_state,
    _matches_filters,
    combined_history,
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


def _room(count: int, last_at: datetime) -> SigninSummary:
    return SigninSummary(count=count, last_at=last_at, room_count=count)


def _code(count: int, last_at: datetime) -> SigninSummary:
    return SigninSummary(count=count, last_at=last_at, code_count=count)


# --- the summary arithmetic --------------------------------------------------


def test_merging_summaries_adds_counts_and_keeps_the_latest_time() -> None:
    merged = client_signins.merge([_room(2, EARLIER), None, _code(1, NOW), SigninSummary()])
    assert merged == SigninSummary(count=3, last_at=NOW, room_count=2, code_count=1)


def test_summary_for_folds_every_room_and_code_of_one_subject_once() -> None:
    bucket_a, bucket_b, intake = uuid4(), uuid4(), uuid4()
    room = {
        bucket_a: [("owner@example.com", _room(3, EARLIER))],
        bucket_b: [("owner@example.com", _room(1, NOW))],
    }
    code = {intake: _code(2, EARLIER)}
    summary = client_signins.summary_for(
        # The same bucket reached through two profiles still counts once.
        bucket_ids=[bucket_a, bucket_a, bucket_b, None],
        intake_ids=[intake, None],
        emails=["Owner@Example.com"],
        room=room,
        code=code,
    )
    assert summary == SigninSummary(count=6, last_at=NOW, room_count=4, code_count=2)


def test_another_persons_link_on_the_same_room_is_not_the_clients_sign_in() -> None:
    bucket = uuid4()
    room = {
        bucket: [
            ("john@graceauto.example", _room(1, EARLIER)),
            ("bobwake@example.com", _room(10, NOW)),  # the partner's own link
        ]
    }
    summary = client_signins.summary_for(
        bucket_ids=[bucket], intake_ids=[], emails=["john@graceauto.example"], room=room, code={}
    )
    assert summary == SigninSummary(count=1, last_at=EARLIER, room_count=1)


def test_a_link_issued_without_an_email_stays_with_the_rooms_subject() -> None:
    bucket = uuid4()
    room = {bucket: [(None, _room(2, NOW))]}
    summary = client_signins.summary_for(
        bucket_ids=[bucket], intake_ids=[], emails=["client@example.com"], room=room, code={}
    )
    assert summary.count == 2


def test_a_subject_with_no_recorded_entries_is_empty() -> None:
    assert client_signins.summary_for(
        bucket_ids=[uuid4()], intake_ids=[uuid4()], emails=[None], room={}, code={}
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
    assert _login_state(None, _room(1, NOW)) == "passcode"
    assert _login_state(None, _code(1, NOW)) == "passcode"


def test_an_account_keeps_its_own_state_even_when_the_pin_was_also_used() -> None:
    assert _login_state(_user(), _room(4, NOW)) == "active"
    assert _login_state(_user(clerk_id=None), _room(4, NOW)) == "invited"
    assert _login_state(_user(account_status="suspended"), _room(4, NOW)) == "suspended"


def test_the_login_filter_can_pick_out_passcode_clients() -> None:
    row = ClientAccessDirectoryRow(
        subject_kind="client",
        subject_id=uuid4(),
        client_name="Bryson Mummert",
        origin="mca_refinance",
        login_state="passcode",
        account_types=[],
        file_count=1,
        passcode_signin_count=1,
        room_pin_signin_count=1,
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
    assert row.room_pin_signin_count == 0
    assert row.email_code_signin_count == 0
    assert row.last_passcode_signin_at is None


def test_room_entries_never_push_the_account_audit_trail_out_of_the_history() -> None:
    suspended = AccessHistoryItem(
        id=uuid4(), action="client_access.suspended", created_at=NOW - timedelta(days=30)
    )
    signins = [
        AccessHistoryItem(
            id=uuid4(), action="room_pin_signin", created_at=NOW - timedelta(minutes=i)
        )
        for i in range(100)
    ]
    history = combined_history([suspended], signins)
    assert len(history) == 101
    assert history[-1] is suspended
    assert history[0].created_at == NOW


# --- reading the recorded entries -------------------------------------------


@pytest.mark.asyncio
async def test_room_sign_ins_are_grouped_per_bucket_and_link_holder() -> None:
    bucket = uuid4()
    db = _Session(_Result([(bucket, "owner@example.com", 3, NOW), (bucket, None, 1, EARLIER)]))
    groups = await client_signins.room_signins_by_bucket(db, [bucket, None])
    assert groups == {
        bucket: [
            ("owner@example.com", _room(3, NOW)),
            (None, _room(1, EARLIER)),
        ]
    }
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_no_buckets_means_no_query() -> None:
    db = _Session()
    assert await client_signins.room_signins_by_bucket(db, []) == {}
    assert await client_signins.code_signins_by_intake(db, [None]) == {}
    assert db.statements == []


@pytest.mark.asyncio
async def test_sign_in_history_names_who_entered_and_mixes_in_code_sign_ins() -> None:
    bucket, intake = uuid4(), uuid4()
    room_rows = [
        SimpleNamespace(
            id=uuid4(), bucket_id=bucket, action="upload_link_accessed", target_id="link-1",
            actor_name="John Grace", actor_email="John@GraceAuto.example",
            created_at=EARLIER, ip_address="203.0.113.9", user_agent="Safari",
        ),
        SimpleNamespace(
            id=uuid4(), bucket_id=bucket, action="upload_link_accessed", target_id="link-2",
            actor_name="Robert Wake", actor_email="bobwake@example.com",
            created_at=EARLIER - timedelta(hours=1), ip_address="198.51.100.7", user_agent="Chrome",
        ),
        SimpleNamespace(
            id=uuid4(), bucket_id=bucket, action="upload_passcode_failed", target_id="link-1",
            actor_name="John Grace", actor_email=None,
            created_at=EARLIER - timedelta(hours=2), ip_address="203.0.113.9", user_agent="Safari",
        ),
    ]
    code_rows = [
        SimpleNamespace(
            id=uuid4(), intake_id=intake, used_at=NOW, ip_address="198.51.100.4", user_agent="Chrome",
        )
    ]
    db = _Session(_Result(room_rows), _Result(code_rows))
    events = await client_signins.signin_events(
        db, bucket_ids=[bucket], intake_ids=[intake], emails=["john@graceauto.example"]
    )
    assert [(event.action, event.own) for event in events] == [
        ("email_code_signin", True),
        ("room_pin_signin", True),
        ("room_pin_signin", False),
        ("room_pin_failed", True),
    ]
    assert events[1].reason == "Entered the secure room with the room PIN as John Grace"
    assert events[1].request_metadata["upload_link_id"] == "link-1"
    assert events[1].request_metadata["ip_address"] == "203.0.113.9"
    assert events[2].reason == "Robert Wake entered the secure room with their own PIN link"
    assert events[2].request_metadata["actor_email"] == "bobwake@example.com"
    assert events[3].reason == "Wrong room PIN was entered"
    assert events[0].request_metadata["intake_id"] == str(intake)


@pytest.mark.asyncio
async def test_sign_in_history_without_subjects_asks_nothing() -> None:
    db = _Session()
    assert await client_signins.signin_events(db, bucket_ids=[None], intake_ids=[], emails=[]) == []
    assert db.statements == []
