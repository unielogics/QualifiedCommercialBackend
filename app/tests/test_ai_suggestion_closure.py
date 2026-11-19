"""AI suggestions follow their file.

A file marked lost or denied keeps its room open so its evidence stays
readable, and so nothing ever answered the suggestions on it: in production
116 of the 283 the desk's dashboard counted sat on denied or lost files. These
tests pin the rule (services/ai_suggestion_closure.py):

* what waits on a closed file (lost, denied, withdrawn, declined,
  disqualified) or an archived room closes, with a reason and a time;
* a live file, a funded file and a room no file uses are left alone;
* a file that comes back reopens exactly what the closing closed, never what
  a person answered;
* the desk's queue, its attention queue and its activity feed drop a closed
  file at once, before the sweep has run;
* an AI review of a closed file files its recommendations already closed;
* a suggestion left waiting and untouched for 90 days expires, in any room,
  and is never reopened by the system.

The real ORM runs against an on-disk SQLite copy of the schema
(app/tests/_sqlite_schema.py). Every name here is made up.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.application_profile import ApplicationProfile
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketAIActionItem,
    BucketRequestedDocument,
)
from app.models.file_event import FileEvent
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.bucket import BucketAIActionItemPatch
from app.services import ai_suggestion_closure as closure
from app.tests._sqlite_schema import sqlite_session, sqlite_template

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "ai_suggestion_closure")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        yield session


# ── builders ────────────────────────────────────────────────────────────────


async def _room(db, name="Example Freight LLC", *, archived=False) -> Bucket:
    room = Bucket(name=f"{name} room", archived_at=NOW - timedelta(days=1) if archived else None)
    db.add(room)
    await db.flush()
    return room


async def _file(
    db,
    *,
    name="Example Freight LLC",
    room=None,
    archived=False,
    status="reviewing",
    outcome="submitted",
    underwriting="in_underwriting",
    foreclosure=None,
    with_profile=True,
    created_at=None,
):
    room = room or await _room(db, name, archived=archived)
    intake = PublicUnderwritingIntake(
        bucket_id=room.id,
        token_hash=uuid.uuid4().hex,
        full_name="Avery Example",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        business_name=name,
        variant="dealer_gatekeeper_v1",
        status=status,
        outcome_status=outcome,
        foreclosure_rescue_status=foreclosure,
        intake_state={"messages": []},
    )
    if created_at is not None:
        intake.created_at = created_at
        intake.updated_at = created_at
    db.add(intake)
    await db.flush()
    profile = None
    if with_profile:
        profile = ApplicationProfile(
            intake_id=intake.id, primary_bucket_id=room.id, underwriting_status=underwriting
        )
        db.add(profile)
        await db.flush()
    return intake, room, profile


async def _lost(db, name="Example Freight LLC", **fields):
    return await _file(
        db, name=name, status="closed_lost", outcome="closed", underwriting="closed_lost", **fields
    )


async def _suggestion(db, room, title="Ask for the 2025 business return", *, status="proposed", age_days=10, **fields):
    created_at = NOW - timedelta(days=age_days)
    item = BucketAIActionItem(
        bucket_id=room.id,
        title=title,
        instructions="Ask the client to upload it to the room.",
        status=status,
        route="uploader",
        created_by="ai",
        created_at=created_at,
        # As in production: nothing edits a suggestion after the AI files it.
        updated_at=fields.pop("updated_at", created_at),
        **fields,
    )
    db.add(item)
    await db.flush()
    return item


async def _reload(db, item) -> BucketAIActionItem:
    return (
        await db.execute(
            select(BucketAIActionItem)
            .where(BucketAIActionItem.id == item.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _room_log(db, room, action):
    return list(
        (
            await db.execute(
                select(BucketActivityLog).where(
                    BucketActivityLog.bucket_id == room.id, BucketActivityLog.action == action
                )
            )
        ).scalars().all()
    )


# ── what closes ─────────────────────────────────────────────────────────────


async def test_a_lost_files_waiting_suggestions_close_and_answered_ones_stay(db) -> None:
    _intake, room, _profile = await _lost(db)
    waiting = await _suggestion(db, room)
    second = await _suggestion(db, room, "Ask for three months of statements")
    approved = await _suggestion(db, room, "Already approved", status="approved")
    rejected = await _suggestion(db, room, "Already dismissed", status="rejected")

    report = await closure.reconcile(db, now=NOW)

    assert report == {"rooms": 1, "closed": 2, "expired": 0, "reopened": 0, "relabelled": 0}
    for item in (waiting, second):
        item = await _reload(db, item)
        assert item.status == "closed"
        assert item.closed_reason == closure.FILE_CLOSED
        assert item.closed_at is not None
    assert (await _reload(db, approved)).status == "approved"
    assert (await _reload(db, rejected)).status == "rejected"
    (entry,) = await _room_log(db, room, closure.CLOSED_ACTION)
    assert entry.actor_role == "system"  # never counted as client activity or put in a digest
    assert entry.detail == "2 AI suggestions closed: the file was closed (lost, denied or withdrawn)"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "closed_lost", "outcome": "closed", "underwriting": "closed_lost"},
        {"status": "denied", "outcome": "denied", "underwriting": "denied"},
        # Older rows: the canonical status moved before the intake mirrored it.
        {"status": "reviewing", "outcome": "submitted", "underwriting": "closed_lost"},
        {"status": "completed", "outcome": "denied", "underwriting": "denied"},
        {"status": "closed", "outcome": "closed", "underwriting": "in_underwriting"},
        {"status": "disqualified", "outcome": "submitted", "underwriting": "submitted"},
        {"status": "collecting", "outcome": "submitted", "underwriting": "submitted", "foreclosure": "withdrawn_expired"},
        {"status": "collecting", "outcome": "submitted", "underwriting": "submitted", "foreclosure": "declined"},
    ],
)
async def test_every_way_a_file_closes_closes_its_suggestions(db, fields) -> None:
    _intake, room, _profile = await _file(db, **fields)
    item = await _suggestion(db, room)

    await closure.reconcile(db, now=NOW)

    item = await _reload(db, item)
    assert (item.status, item.closed_reason) == ("closed", closure.FILE_CLOSED)


async def test_an_archived_room_closes_its_suggestions_under_its_own_reason(db) -> None:
    _intake, room, _profile = await _file(db, archived=True)
    item = await _suggestion(db, room)

    await closure.reconcile(db, now=NOW)

    item = await _reload(db, item)
    assert (item.status, item.closed_reason) == ("closed", closure.ROOM_ARCHIVED)


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "reviewing", "outcome": "submitted", "underwriting": "in_underwriting"},
        {"status": "collecting", "outcome": "submitted", "underwriting": "collecting_docs"},
        {"status": "completed", "outcome": "closed", "underwriting": "closed_won"},
        # Funding is an affirmative fact: a stale denial left on an imported
        # intake does not close a funded file.
        {"status": "denied", "outcome": "denied", "underwriting": "closed_won"},
        {"status": "denied", "outcome": "denied", "underwriting": "submitted", "foreclosure": "funded"},
    ],
)
async def test_live_and_funded_files_keep_their_suggestions(db, fields) -> None:
    _intake, room, _profile = await _file(db, **fields)
    item = await _suggestion(db, room)

    report = await closure.reconcile(db, now=NOW)

    assert report["closed"] == 0
    assert (await _reload(db, item)).status == "proposed"


async def test_a_room_no_file_uses_is_left_alone(db) -> None:
    room = await _room(db, "Example shared documents")
    item = await _suggestion(db, room)

    await closure.reconcile(db, now=NOW)

    assert (await _reload(db, item)).status == "proposed"


async def test_a_room_shared_with_a_live_file_stays_open(db) -> None:
    _lost_intake, room, _p1 = await _lost(db)
    await _file(db, name="Example Freight LLC second file", room=room)
    item = await _suggestion(db, room)

    await closure.reconcile(db, now=NOW)

    assert (await _reload(db, item)).status == "proposed"


async def test_a_file_that_never_came_through_an_intake_follows_its_profile(db) -> None:
    lost_room = await _room(db, "Example Desk File")
    db.add(ApplicationProfile(primary_bucket_id=lost_room.id, underwriting_status="closed_lost"))
    live_room = await _room(db, "Example Live Desk File")
    db.add(ApplicationProfile(primary_bucket_id=live_room.id, underwriting_status="in_underwriting"))
    await db.flush()
    lost_item = await _suggestion(db, lost_room)
    live_item = await _suggestion(db, live_room)

    await closure.reconcile(db, now=NOW)

    assert (await _reload(db, lost_item)).closed_reason == closure.FILE_CLOSED
    assert (await _reload(db, live_item)).status == "proposed"


# ── what comes back ─────────────────────────────────────────────────────────


async def test_a_file_that_comes_back_gets_exactly_its_suggestions_back(db) -> None:
    intake, room, profile = await _lost(db)
    waiting = await _suggestion(db, room)
    dismissed = await _suggestion(db, room, "Dismissed by the desk", status="rejected")
    await closure.reconcile(db, now=NOW)

    # The desk reopens the file.
    profile.underwriting_status = "in_underwriting"
    intake.status = "reviewed"
    intake.outcome_status = "submitted"
    await db.flush()
    report = await closure.reconcile(db, now=NOW + timedelta(hours=1))

    assert report == {"rooms": 1, "closed": 0, "expired": 0, "reopened": 1, "relabelled": 0}
    waiting = await _reload(db, waiting)
    assert (waiting.status, waiting.closed_at, waiting.closed_reason) == ("proposed", None, None)
    assert (await _reload(db, dismissed)).status == "rejected"
    (entry,) = await _room_log(db, room, closure.REOPENED_ACTION)
    assert entry.detail == "1 AI suggestion waiting again: the file is active again"


async def test_a_restored_room_whose_file_is_still_lost_stays_closed(db) -> None:
    _intake, room, _profile = await _lost(db, archived=True)
    item = await _suggestion(db, room)
    await closure.reconcile(db, now=NOW)
    assert (await _reload(db, item)).closed_reason == closure.ROOM_ARCHIVED

    room.archived_at = None
    await db.flush()
    report = await closure.reconcile(db, now=NOW)

    assert report["relabelled"] == 1 and report["reopened"] == 0
    item = await _reload(db, item)
    assert (item.status, item.closed_reason) == ("closed", closure.FILE_CLOSED)


async def test_running_it_again_changes_nothing(db) -> None:
    _intake, room, _profile = await _lost(db)
    await _suggestion(db, room)
    await closure.reconcile(db, now=NOW)

    again = await closure.reconcile(db, now=NOW)

    assert again["closed"] == again["reopened"] == again["relabelled"] == 0
    assert len(await _room_log(db, room, closure.CLOSED_ACTION)) == 1


async def test_it_can_look_at_one_room(db) -> None:
    _i1, first_room, _p1 = await _lost(db, "Example One LLC")
    _i2, second_room, _p2 = await _lost(db, "Example Two LLC")
    first = await _suggestion(db, first_room)
    second = await _suggestion(db, second_room)

    await closure.reconcile(db, bucket_ids=[first_room.id], now=NOW)

    assert (await _reload(db, first)).status == "closed"
    assert (await _reload(db, second)).status == "proposed"
    assert (await closure.reconcile(db, bucket_ids=[], now=NOW))["rooms"] == 0


async def test_a_person_answering_a_closed_suggestion_keeps_their_answer(db) -> None:
    from app.routers import buckets

    intake, room, profile = await _lost(db)
    item = await _suggestion(db, room)
    await closure.reconcile(db, now=NOW)
    desk = SimpleNamespace(id=uuid.uuid4(), role="super_admin", name="Desk User", email="desk@example.com")

    answered = await buckets.patch_ai_action_item(
        room.id, item.id, BucketAIActionItemPatch(status="rejected"), None, desk, db
    )

    assert (answered.status, answered.closed_at, answered.closed_reason) == ("rejected", None, None)
    profile.underwriting_status = "in_underwriting"
    intake.status = "reviewed"
    intake.outcome_status = "submitted"
    await db.flush()
    await closure.reconcile(db, now=NOW)
    assert (await _reload(db, item)).status == "rejected"


# ── unanswered for 90 days ──────────────────────────────────────────────────


async def test_a_suggestion_left_unanswered_for_90_days_expires_for_good(db) -> None:
    _intake, live_room, _profile = await _file(db, name="Example Live LLC")
    shared_room = await _room(db, "Example shared documents")
    old = await _suggestion(db, live_room, "Asked 91 days ago", age_days=91)
    recent = await _suggestion(db, live_room, "Asked 89 days ago", age_days=89)
    forgotten = await _suggestion(db, shared_room, "Asked 120 days ago", age_days=120)
    answered = await _suggestion(db, live_room, "Approved long ago", status="approved", age_days=200)

    report = await closure.reconcile(db, now=NOW)

    assert report == {"rooms": 2, "closed": 0, "expired": 2, "reopened": 0, "relabelled": 0}
    for item in (old, forgotten):
        item = await _reload(db, item)
        assert (item.status, item.closed_reason) == ("closed", closure.EXPIRED)
        assert item.closed_at is not None
    assert (await _reload(db, recent)).status == "proposed"
    assert (await _reload(db, answered)).status == "approved"
    (entry,) = await _room_log(db, shared_room, closure.EXPIRED_ACTION)
    assert entry.actor_role == "system"
    assert entry.detail == "1 AI suggestion closed: unanswered for 90 days"

    # Expired is final: the next pass finds nothing to do, even on a live file.
    assert await closure.rooms_out_of_step(db, now=NOW) == {}
    assert (await closure.reconcile(db, now=NOW))["rooms"] == 0


async def test_the_clock_runs_from_the_last_time_anyone_touched_it(db) -> None:
    room = await _room(db, "Example shared documents")
    touched = await _suggestion(db, room, age_days=120, updated_at=NOW - timedelta(days=5))

    await closure.reconcile(db, now=NOW)

    assert (await _reload(db, touched)).status == "proposed"


async def test_a_file_that_comes_back_late_gets_its_old_suggestions_back_expired(db) -> None:
    intake, room, profile = await _lost(db)
    stale = await _suggestion(db, room, "Asked 120 days ago", age_days=120)
    fresh = await _suggestion(db, room, "Asked 10 days ago", age_days=10)
    await closure.reconcile(db, now=NOW)
    assert (await _reload(db, stale)).closed_reason == closure.FILE_CLOSED  # the file's reason comes first
    assert (await _reload(db, fresh)).closed_reason == closure.FILE_CLOSED

    profile.underwriting_status = "in_underwriting"
    intake.status = "reviewed"
    intake.outcome_status = "submitted"
    await db.flush()
    report = await closure.reconcile(db, now=NOW + timedelta(hours=1))

    assert report == {"rooms": 1, "closed": 0, "expired": 1, "reopened": 1, "relabelled": 0}
    stale = await _reload(db, stale)
    assert (stale.status, stale.closed_reason) == ("closed", closure.EXPIRED)
    assert (await _reload(db, fresh)).status == "proposed"
    assert len(await _room_log(db, room, closure.EXPIRED_ACTION)) == 1
    assert len(await _room_log(db, room, closure.REOPENED_ACTION)) == 1


async def test_a_review_files_a_suggestion_again_once_the_old_one_expired(db) -> None:
    from app.services import bucket_ai

    _intake, room, _profile = await _file(db, name="Example Live LLC")
    title = "Ask for the 2025 business return"
    await _suggestion(db, room, title, age_days=120)
    await closure.reconcile(db, now=NOW)
    review = SimpleNamespace(id=uuid.uuid4())
    result = {"recommended_next_document_requests": [{"title": title, "instructions": "Upload the full return."}]}

    (again,) = await bucket_ai._create_review_recommendation_actions(db, bucket=room, review=review, result=result)

    assert (again.status, again.closed_reason) == ("proposed", None)
    # While that one waits, the next review does not file it a second time.
    assert await bucket_ai._create_review_recommendation_actions(db, bucket=room, review=review, result=result) == []


# ── the desk sees it at once ────────────────────────────────────────────────


async def test_the_desk_queue_drops_a_closed_file_before_the_sweep_runs(db) -> None:
    from app.routers import buckets

    live, live_room, _p1 = await _file(db, name="Example Live LLC")
    _lost_intake, lost_room, _p2 = await _lost(db, "Example Lost LLC")
    _archived, archived_room, _p3 = await _file(db, name="Example Archived LLC", archived=True)
    shared_room = await _room(db, "Example shared documents")
    await _suggestion(db, live_room, "Live file suggestion")
    await _suggestion(db, lost_room, "Lost file suggestion")
    await _suggestion(db, archived_room, "Archived room suggestion")
    await _suggestion(db, shared_room, "Shared room suggestion")

    async def titles(bucket_id=None):
        rows = await buckets.list_ai_action_items_across_buckets(
            item_status="proposed", limit=500, bucket_id=bucket_id, _=SimpleNamespace(), db=db
        )
        return sorted(row.title for row in rows)

    assert await titles() == ["Live file suggestion", "Shared room suggestion"]
    assert await titles(lost_room.id) == []  # the lost file's own page waits on nothing
    assert await titles(live_room.id) == ["Live file suggestion"]
    assert (await buckets.list_ai_action_items_across_buckets(
        item_status="proposed", limit=500, bucket_id=live_room.id, _=SimpleNamespace(), db=db
    ))[0].intake_id == live.id


async def test_the_attention_queue_leaves_closed_files_out(db) -> None:
    from app.services import attention_queue

    old = NOW - timedelta(days=60)
    live, live_room, _p1 = await _file(db, name="Example Live LLC", status="collecting", underwriting="collecting_docs", created_at=old)
    # An older row: the profile says lost, the intake was never told.
    stale, stale_room, _p2 = await _file(db, name="Example Lost LLC", status="collecting", underwriting="closed_lost", created_at=old)
    archived, archived_room, _p3 = await _file(db, name="Example Archived LLC", status="collecting", underwriting="collecting_docs", archived=True, created_at=old)
    for room in (live_room, stale_room, archived_room):
        db.add(BucketRequestedDocument(bucket_id=room.id, name="Bank statements", status="requested", created_at=old))
        await _suggestion(db, room, age_days=30)
    await db.flush()

    items = await attention_queue.load_source_items(db, now=NOW)

    rooms = {item.bucket_id for item in items}
    assert live_room.id in rooms
    assert stale_room.id not in rooms
    assert archived_room.id not in rooms
    assert {item.kind for item in items if item.bucket_id == live_room.id} == {
        "intake_no_files", "no_client_entry", "request_overdue", "action_items_stale",
    }
    assert {item.intake_id for item in items if item.kind == "intake_no_files"} == {live.id}


async def test_the_activity_feed_leaves_closed_files_out(db) -> None:
    from app.services import file_clients

    _live, _r1, live_profile = await _file(db, name="Example Live LLC")
    _lost_intake, _r2, lost_profile = await _lost(db, "Example Lost LLC")
    _archived, _r3, archived_profile = await _file(db, name="Example Archived LLC", archived=True)
    for profile, title in ((live_profile, "Live file event"), (lost_profile, "Lost file event"), (archived_profile, "Archived file event")):
        db.add(FileEvent(profile_id=profile.id, kind="document.received", visibility="client", title=title, created_at=NOW))
    await db.flush()

    items = await file_clients.desk_activity(db)

    assert [item["title"] for item in items] == ["Live file event"]


# ── new suggestions on a closed file ────────────────────────────────────────


async def test_a_review_of_a_closed_file_files_its_recommendations_closed(db) -> None:
    from app.services import bucket_ai

    _lost_intake, lost_room, _p1 = await _lost(db, "Example Lost LLC")
    _live, live_room, _p2 = await _file(db, name="Example Live LLC")
    review = SimpleNamespace(id=uuid.uuid4())
    result = {"recommended_next_document_requests": [
        {"title": "Ask for the 2025 business return", "instructions": "Upload the full return."},
    ]}

    (on_lost,) = await bucket_ai._create_review_recommendation_actions(db, bucket=lost_room, review=review, result=result)
    (on_live,) = await bucket_ai._create_review_recommendation_actions(db, bucket=live_room, review=review, result=result)

    assert (on_lost.status, on_lost.closed_reason) == ("closed", closure.FILE_CLOSED)
    assert on_lost.closed_at is not None
    assert (on_live.status, on_live.closed_reason) == ("proposed", None)
    # The next review of the lost file does not file the same thing again.
    assert await bucket_ai._create_review_recommendation_actions(db, bucket=lost_room, review=review, result=result) == []
    assert await closure.closing_reason(db, lost_room.id) == closure.FILE_CLOSED
    assert await closure.closing_reason(db, live_room.id) is None


# ── the scheduled sweep ─────────────────────────────────────────────────────


async def _status(db, item_id) -> str:
    return (
        await db.execute(select(BucketAIActionItem.status).where(BucketAIActionItem.id == item_id))
    ).scalar_one()


async def test_the_sweep_commits_room_by_room_and_one_failure_spares_the_rest(db, monkeypatch) -> None:
    _i1, first_room, _p1 = await _lost(db, "Example One LLC")
    _i2, second_room, _p2 = await _lost(db, "Example Two LLC")
    _i3, live_room, _p3 = await _file(db, name="Example Live LLC")
    # Ids first: the sweep rolls back between rooms, which expires every row.
    first_room_id = first_room.id
    first = (await _suggestion(db, first_room)).id
    second = (await _suggestion(db, second_room)).id
    live = (await _suggestion(db, live_room)).id
    await db.commit()

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.db.SessionLocal", Sessions())
    real_reconcile = closure.reconcile

    async def one_room_fails(session, *, bucket_ids=None, now=None):
        if bucket_ids == [first_room_id]:
            raise RuntimeError("simulated failure in one room")
        return await real_reconcile(session, bucket_ids=bucket_ids, now=now)

    monkeypatch.setattr(closure, "reconcile", one_room_fails)

    report = await closure.run_scheduled_sweep()

    assert report == {"rooms": 1, "closed": 1, "expired": 0, "reopened": 0, "relabelled": 0, "failed": 1}
    assert await _status(db, first) == "proposed"  # rolled back, tried again next pass
    assert await _status(db, second) == "closed"
    assert await _status(db, live) == "proposed"

    monkeypatch.setattr(closure, "reconcile", real_reconcile)
    assert (await closure.run_scheduled_sweep())["closed"] == 1
    quiet = await closure.run_scheduled_sweep()
    assert quiet == {"rooms": 0, "closed": 0, "expired": 0, "reopened": 0, "relabelled": 0, "failed": 0}


async def test_only_rooms_out_of_step_are_visited(db) -> None:
    _i1, lost_room, _p1 = await _lost(db, "Example Lost LLC")
    _i2, live_room, _p2 = await _file(db, name="Example Live LLC")
    await _suggestion(db, lost_room)
    await _suggestion(db, live_room)

    assert await closure.rooms_out_of_step(db) == {lost_room.id: closure.FILE_CLOSED}
    await closure.reconcile(db, now=NOW)
    assert await closure.rooms_out_of_step(db) == {}


# ── wiring ──────────────────────────────────────────────────────────────────


def test_the_sweep_runs_on_the_scheduler() -> None:
    from app.services import scheduler

    source = inspect.getsource(scheduler.start_scheduler)
    assert 'id="ai_suggestion_closure"' in source
    assert "run_scheduled_sweep" in inspect.getsource(scheduler.job_ai_suggestion_closure)
    assert "ai_suggestion_closure" in inspect.getsource(scheduler.job_ai_suggestion_closure)


def test_the_room_rule_correlates_on_the_room_in_postgres() -> None:
    """Each subquery reads its own intakes and profiles against the outer room;
    none may fall back to a cross join of every room."""

    from app.models.bucket import Bucket as Room

    sql = str(
        select(BucketAIActionItem.id)
        .join(Room, Room.id == BucketAIActionItem.bucket_id)
        .where(closure.room_is_live())
        .compile(dialect=postgresql.dialect())
    )
    assert sql.count("FROM buckets") == 0
    assert "public_underwriting_intakes.bucket_id = buckets.id" in sql
    assert "application_profiles_1.primary_bucket_id = buckets.id" in sql
