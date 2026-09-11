"""Holding a form's PDF back until the typing stops.

Every save used to render a PDF and put it to S3 on the spot. On the live
worksheet, where a save is a cell, that is one document per keystroke — and the
owner's instruction was to wait: *"we will wait 120 seconds before we generate
PDF with updates. This way we prevent excessive mistakes from happening and
documents being updated for no reason."*

The rules pinned here, in the order they matter:

* a save queues rather than renders, 120 seconds out;
* **a second save pushes the deadline out rather than adding a row.** That is
  the difference between a debounce and a rate limit: a rate limit fires on the
  first edit of a burst and drops the rest, so the last figure typed — the one
  that mattered — never reaches the PDF;
* the drain draws only what is due, and drops the row once it has;
* a render that fails defers its row instead of deleting it or spinning on it
  in front of everything behind it;
* **a submit still files immediately** and clears the pending row, so the job
  cannot put an identical render over a just-filed document a minute later;
* the four worksheet write routes and the stacked-form draft saves queue.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.form_pdf_refresh import FormPdfRefresh
from app.routers import application_profiles as router
from app.routers import buckets as buckets_router
from app.routers import dealer_ai_intake as intake_router
from app.routers import worksheets as worksheets_router
from app.services import business_statement_schema as bss
from app.services import drafted_forms, scheduler, sheets


def _run(coro):
    return asyncio.run(coro)


def _profile(bucket=True):
    return SimpleNamespace(id=uuid.uuid4(), primary_bucket_id=uuid.uuid4() if bucket else None)


# ── a tiny standing queue, so the debounce can be watched rather than mocked ──


class _Queue:
    """Just enough session for the queue: an upsert that really upserts on
    (profile, kind), a due-at select, a delete and an update.

    A dict rather than a table because what the tests are about is the
    behaviour the unique constraint buys — one row per form, whose deadline
    moves — and that has to be visible, not stubbed.
    """

    def __init__(self):
        self.rows: dict[tuple[uuid.UUID, str], dict] = {}
        self.commits = 0
        self.rollbacks = 0
        self.profiles: dict[uuid.UUID, object] = {}
        self.executed: list = []

    async def execute(self, statement):
        self.executed.append(statement)
        compiled = str(statement).split("\n")[0].strip().upper()
        if compiled.startswith("INSERT"):
            values = {
                key: bind.value for key, bind in statement.compile().binds.items()
            }
            key = (values["profile_id"], values["kind"])
            existing = self.rows.get(key)
            if existing is None:
                self.rows[key] = dict(values)
            else:
                existing["due_at"] = values["due_at"]
                existing["actor_name"] = values["actor_name"] or existing["actor_name"]
                existing["actor_email"] = values["actor_email"] or existing["actor_email"]
            return SimpleNamespace()
        if compiled.startswith("DELETE"):
            for key, row in list(self.rows.items()):
                if row["id"] in self._ids(statement) or key[1] in self._kinds(statement):
                    self.rows.pop(key, None)
            return SimpleNamespace()
        if compiled.startswith("UPDATE"):
            wanted = self._ids(statement)
            for row in self.rows.values():
                if row["id"] in wanted:
                    row["due_at"] = self._deferred(statement)
            return SimpleNamespace()
        # The due-at select.
        now = self._now(statement)
        due = [row for row in self.rows.values() if row["due_at"] <= now]
        due.sort(key=lambda row: row["due_at"])
        rows = [
            (row["id"], row["profile_id"], row["kind"], row["actor_name"], row["actor_email"])
            for row in due
        ]
        return SimpleNamespace(all=lambda: rows)

    @staticmethod
    def _binds(statement):
        return [bind.value for bind in statement.compile().binds.values()]

    def _ids(self, statement):
        return {v for v in self._binds(statement) if isinstance(v, uuid.UUID)}

    def _kinds(self, statement):
        return {v for v in self._binds(statement) if isinstance(v, str)}

    def _now(self, statement):
        stamps = [v for v in self._binds(statement) if isinstance(v, datetime)]
        return stamps[0] if stamps else datetime.now(UTC)

    def _deferred(self, statement):
        stamps = [v for v in self._binds(statement) if isinstance(v, datetime)]
        return max(stamps)

    async def get(self, _model, key):
        return self.profiles.get(key)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _enqueue(db, profile, kind="p_and_l", **kw):
    return _run(drafted_forms.enqueue_form_refresh(db, profile, kind, **kw))


# ── the wait ────────────────────────────────────────────────────────────────


def test_a_save_queues_the_redraw_for_two_minutes_time_rather_than_drawing_it():
    db, profile = _Queue(), _profile()
    before = datetime.now(UTC)
    due_at = _enqueue(db, profile, actor_name="Jane Desk", actor_email="jane@example.com")
    after = datetime.now(UTC)

    assert due_at is not None
    # The owner's 120 seconds, measured from the save.
    assert before + timedelta(seconds=120) <= due_at <= after + timedelta(seconds=120)
    assert len(db.rows) == 1
    row = next(iter(db.rows.values()))
    assert row["kind"] == "p_and_l" and row["profile_id"] == profile.id
    assert row["actor_name"] == "Jane Desk"
    # Its own write, after the caller's commit — the same shape the render it
    # replaces used.
    assert db.commits == 1


def test_the_delay_is_the_owners_number_and_says_why():
    assert drafted_forms.REFRESH_DELAY_SECONDS == 120
    reason = inspect.getsource(drafted_forms).split("REFRESH_DELAY_SECONDS = 120")[0]
    assert "keystroke" in reason and "two minutes behind" in reason


def test_a_second_save_pushes_the_deadline_out_and_does_not_add_a_row():
    """The whole mechanism. A rate limit would fire on the first edit of a
    burst and drop the rest, so the last figure typed would never reach the
    PDF. Pushing the deadline forward settles on the last edit instead."""
    db, profile = _Queue(), _profile()
    first = _enqueue(db, profile, actor_name="Jane Desk")
    # Thirty seconds later, still typing.
    second = _enqueue(db, profile, delay_seconds=150)

    assert len(db.rows) == 1                       # the unique row holds
    row = next(iter(db.rows.values()))
    assert row["due_at"] == second > first         # and its deadline moved out
    # A later save with no name does not erase the name the earlier one had.
    assert row["actor_name"] == "Jane Desk"


def test_the_upsert_names_the_unique_constraint_that_is_the_debounce():
    db, profile = _Queue(), _profile()
    _enqueue(db, profile)
    insert = db.executed[0]
    assert "uq_form_pdf_refresh_queue_profile_kind" in str(insert)
    assert "ON CONFLICT" in str(insert).upper()
    # The row is a deadline, not a payload: nothing about what was typed is
    # stored, so a row that waits through six more edits still draws today's
    # figures when it finally runs.
    assert "body" not in inspect.signature(drafted_forms.enqueue_form_refresh).parameters


def test_two_forms_on_one_file_are_two_rows():
    db, profile = _Queue(), _profile()
    _enqueue(db, profile, "p_and_l")
    _enqueue(db, profile, "balance_sheet")
    assert {row["kind"] for row in db.rows.values()} == {"p_and_l", "balance_sheet"}


def test_an_unknown_form_and_a_file_with_no_document_room_queue_nothing():
    db = _Queue()
    assert _enqueue(db, _profile(), "worksheet") is None
    assert _enqueue(db, _profile(bucket=False)) is None
    assert _enqueue(db, None) is None
    assert db.rows == {} and db.commits == 0


def test_a_file_row_a_rollback_expired_is_a_lost_redraw_not_a_five_hundred():
    """The caller has already committed by the time this runs, and something
    between may have rolled back — the per-kind loop in `refresh_touched_pdfs`
    does exactly that when one kind fails. Reading an attribute off an expired
    ORM row is then a lazy load with no greenlet under it. Losing the redraw
    costs a stale PDF until the next save; raising would cost a 500 on a save
    that is already durable."""

    class _Expired:
        @property
        def id(self):
            raise RuntimeError("greenlet_spawn has not been called")

    db = _Queue()
    assert _enqueue(db, _Expired()) is None
    assert db.rows == {} and db.commits == 0


def test_a_queue_write_that_fails_never_fails_the_save():
    """The save is already committed by the time this runs. The cost of
    swallowing is a PDF that stays behind until the next save — exactly what
    the immediate render cost when WeasyPrint threw."""
    db = _Queue()
    db.execute = AsyncMock(side_effect=RuntimeError("pg gone"))
    assert _enqueue(db, _profile()) is None
    assert db.rollbacks == 1 and db.commits == 0


# ── the drain ───────────────────────────────────────────────────────────────


def _due(db, profile, kind="p_and_l", *, seconds_ago=1):
    """A row whose deadline has already passed."""
    db.profiles[profile.id] = profile
    db.rows[(profile.id, kind)] = {
        "id": uuid.uuid4(),
        "profile_id": profile.id,
        "kind": kind,
        "due_at": datetime.now(UTC) - timedelta(seconds=seconds_ago),
        "actor_name": "Jane Desk",
        "actor_email": "jane@example.com",
    }
    return db.rows[(profile.id, kind)]


def test_the_drain_redraws_only_what_is_due_and_then_drops_the_row():
    db = _Queue()
    ready, waiting = _profile(), _profile()
    _due(db, ready)
    db.profiles[waiting.id] = waiting
    db.rows[(waiting.id, "pfs")] = {
        "id": uuid.uuid4(), "profile_id": waiting.id, "kind": "pfs",
        "due_at": datetime.now(UTC) + timedelta(seconds=90),
        "actor_name": None, "actor_email": None,
    }

    with patch.object(drafted_forms, "refresh_saved_form", AsyncMock()) as drew:
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 1

    drew.assert_awaited_once()
    assert drew.await_args.args[1] is ready
    assert drew.await_args.args[2] == "p_and_l"
    # Attributed to whoever last typed, not to the cron actor that drew it.
    assert drew.await_args.kwargs["actor_name"] == "Jane Desk"
    # The done row is gone; the one still settling is untouched.
    assert list(db.rows) == [(waiting.id, "pfs")]


def test_an_empty_queue_is_a_tick_that_touches_nothing():
    db = _Queue()
    with patch.object(drafted_forms, "refresh_saved_form", AsyncMock()) as drew:
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 0
    drew.assert_not_awaited()
    assert db.commits == 0


def test_a_render_that_fails_defers_its_row_instead_of_deleting_or_wedging():
    """A failure must not take the redraw off the queue, and must not be
    retried immediately and forever in front of everything behind it."""
    db = _Queue()
    profile = _profile()
    row = _due(db, profile)
    was_due = row["due_at"]

    with patch.object(
        drafted_forms, "refresh_saved_form", AsyncMock(side_effect=RuntimeError("weasy"))
    ):
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 0

    assert len(db.rows) == 1                       # still queued
    assert row["due_at"] > was_due + timedelta(seconds=60)   # and pushed out
    assert db.rollbacks == 1


def test_one_bad_row_does_not_stop_the_rest_of_the_tick():
    db = _Queue()
    bad, good = _profile(), _profile()
    _due(db, bad, seconds_ago=30)
    _due(db, good, "pfs", seconds_ago=10)

    def _fail_on_bad(_db, profile, _kind, **_kw):
        if profile is bad:
            raise RuntimeError("weasy")
        return None

    with patch.object(drafted_forms, "refresh_saved_form", AsyncMock(side_effect=_fail_on_bad)):
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 1

    assert list(db.rows) == [(bad.id, "p_and_l")]


def test_a_form_with_nothing_to_file_is_dropped_rather_than_retried_forever():
    """`refresh_saved_form` returns None for a file with no checklist row and
    for a form nobody has typed into. Neither resolves by waiting, so keeping
    the row would redraw nothing every thirty seconds until the heat death."""
    db = _Queue()
    _due(db, _profile())
    with patch.object(drafted_forms, "refresh_saved_form", AsyncMock(return_value=None)):
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 1
    assert db.rows == {}


def test_a_deleted_profile_takes_its_queued_redraw_with_it():
    db = _Queue()
    row = _due(db, _profile())
    db.profiles.clear()
    with patch.object(drafted_forms, "refresh_saved_form", AsyncMock()) as drew:
        assert _run(drafted_forms.drain_form_refresh_queue(db)) == 1
    drew.assert_not_awaited()
    assert db.rows == {} and row["id"] is not None


def test_the_drain_reads_plain_columns_so_a_commit_cannot_expire_it_mid_loop():
    """`refresh_saved_form` commits. An ORM row held across that would expire
    and turn the next attribute read into a lazy load with no greenlet under
    it — a 500 on a cron tick, which nobody would see until the PDFs stopped."""
    src = inspect.getsource(drafted_forms.drain_form_refresh_queue)
    assert "FormPdfRefresh.id," in src and "FormPdfRefresh.profile_id," in src
    assert "select(FormPdfRefresh)" not in src


# ── a submit does not wait ──────────────────────────────────────────────────


def _storage(put=None):
    return (
        patch.object(
            buckets_router, "_bucket_storage_config", MagicMock(return_value=("b", "buckets", "k"))
        ),
        patch.object(intake_router, "_put_bucket_object", put or MagicMock()),
    )


class _SubmitDb:
    """The submit's own session: one lookup that finds nothing, plus whatever
    else the refresh runs."""

    def __init__(self):
        self.statements: list = []
        self.added: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(first=lambda: None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


def test_a_submit_files_at_once_and_clears_the_pending_row():
    """A submit is a deliberate act that marks a checklist requirement met.
    Making somebody wait two minutes for their own document would be absurd —
    and leaving the queued redraw standing would put an identical render over
    the top of what they just filed, a minute later."""
    db, slot = _SubmitDb(), SimpleNamespace(id=uuid.uuid4(), status="requested")
    bucket_id = uuid.uuid4()
    config, putter = _storage(put := MagicMock())
    with config, putter:
        _run(
            drafted_forms.refresh_form_pdf(
                db,
                bucket_id=bucket_id,
                requested_document=slot,
                pdf_bytes=b"%PDF",
                file_label="Profit and loss statement",
                classification="current_p_and_l",
                key_facts={"gross_revenue": 1.0},
                mark_uploaded=True,
            )
        )

    put.assert_called_once()                 # rendered and filed, now
    assert slot.status == "uploaded"         # and the requirement is met
    deletes = [
        str(s.compile(compile_kwargs={"literal_binds": True}))
        for s in db.statements
        if str(s).upper().lstrip().startswith("DELETE")
    ]
    assert len(deletes) == 1, "a submit must clear its own pending redraw"
    assert "form_pdf_refresh_queue" in deletes[0]
    assert "'p_and_l'" in deletes[0]                 # the kind, from the classification
    assert bucket_id.hex in deletes[0].replace('-', '')   # and only this file's


def test_a_draft_refresh_leaves_the_queue_alone():
    """Only `mark_uploaded=True` clears. A draft refresh — which is what the
    drain itself performs — must not delete a row somebody has since re-queued
    by typing again."""
    db, slot = _SubmitDb(), SimpleNamespace(id=uuid.uuid4(), status="requested")
    config, putter = _storage()
    with config, putter:
        _run(
            drafted_forms.refresh_form_pdf(
                db,
                bucket_id=uuid.uuid4(),
                requested_document=slot,
                pdf_bytes=b"%PDF",
                file_label="Profit and loss statement",
                classification="current_p_and_l",
                key_facts={},
            )
        )
    assert not [s for s in db.statements if str(s).upper().lstrip().startswith("DELETE")]


def test_the_clear_is_part_of_the_submits_own_transaction():
    """A submit that rolls back has filed nothing, and its form should still be
    redrawn on schedule."""
    src = inspect.getsource(drafted_forms._clear_pending_refresh)
    assert "commit" not in src and "rollback" not in src


def test_every_form_a_submit_can_file_maps_back_to_a_queue_kind():
    """The clear is keyed off the classification, because that is what the
    submit paths speak. A form whose classification is missing from the map
    would file immediately and then be redrawn a minute later anyway."""
    from app.services import pfs_schema  # noqa: F401  (kept beside its siblings)

    classifications = {schema.classification for schema in bss.SCHEMA_FOR.values()}
    classifications |= {"debt_schedule", "personal_financial_statement"}
    assert classifications <= set(drafted_forms._KIND_FOR_CLASSIFICATION)
    assert set(drafted_forms._KIND_FOR_CLASSIFICATION.values()) == set(drafted_forms._KINDS)


# ── where the save paths queue ──────────────────────────────────────────────


DRAFT_SITES = (
    router.save_business_statement,
    router.save_debt_schedule,
    router.update_financial_statement,
    router.save_public_financial_form_draft,
)

WORKSHEET_WRITES = (
    router.write_worksheet_cells,
    router.write_worksheet_row,
    worksheets_router.write_worksheet_cells,
    worksheets_router.write_worksheet_rows,
)


def test_every_stacked_form_draft_save_queues_instead_of_rendering():
    for handler in DRAFT_SITES:
        src = inspect.getsource(handler)
        assert "drafted_forms.enqueue_form_refresh(" in src, handler.__name__
        assert "drafted_forms.refresh_saved_form(" not in src, handler.__name__
        # Still after the save is durable — a queue write commits too.
        assert src.index("await db.commit()") < src.index(
            "drafted_forms.enqueue_form_refresh("
        ), handler.__name__


def test_the_four_worksheet_writes_queue_rather_than_render():
    """A figure typed in the grid still has to reach the PDF — just not on the
    keystroke. `refresh_touched_pdfs` keeps its name and signature so the four
    routes are untouched; what changed is what it does."""
    for handler in WORKSHEET_WRITES:
        src = inspect.getsource(handler)
        assert "refresh_touched_pdfs(" in src, handler.__qualname__
        assert src.index("await db.commit()") < src.index("refresh_touched_pdfs("), (
            f"{handler.__qualname__} refreshes before it commits"
        )
    body = inspect.getsource(sheets.refresh_touched_pdfs)
    assert "enqueue_form_refresh(" in body
    assert "refresh_saved_form(" not in body


def test_the_worksheet_routes_still_get_the_signature_they_call():
    params = inspect.signature(sheets.refresh_touched_pdfs).parameters
    assert list(params) == ["db", "profile", "kinds", "actor_name", "actor_email"]


@pytest.mark.asyncio
async def test_a_batch_of_cells_across_two_sheets_queues_one_row_each():
    db, profile = _Queue(), _profile()
    await sheets.refresh_touched_pdfs(
        db, profile, ["p_and_l", "p_and_l", "balance_sheet", "worksheet"], actor_name="Ana"
    )
    assert sorted(kind for _pid, kind in db.rows) == ["balance_sheet", "p_and_l"]


# ── the tick that draws them ────────────────────────────────────────────────


def test_the_scheduler_runs_the_drain_every_thirty_seconds():
    src = inspect.getsource(scheduler.start_scheduler)
    block = src.split("_wrap(job_form_pdf_refresh)")[1].split(")")[0]
    assert '"interval"' in block and "seconds=30" in block
    assert 'id="form_pdf_refresh"' in block
    assert "coalesce=True" in block and "max_instances=1" in block
    # The caveat every job in this file carries.
    assert "single-instance" in src.split("_wrap(job_form_pdf_refresh)")[0].split(
        "job_booking_reminders"
    )[-1].lower() or "second backend instance" in src


def test_the_job_opens_its_own_session_and_says_nothing_when_there_is_nothing():
    src = inspect.getsource(scheduler.job_form_pdf_refresh)
    assert "SessionLocal()" in src and "drain_form_refresh_queue" in src
    assert "if drawn:" in src


@pytest.mark.asyncio
async def test_the_job_drains_through_the_service():
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value="db")
    session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("app.db.SessionLocal", MagicMock(return_value=session)),
        patch.object(drafted_forms, "drain_form_refresh_queue", AsyncMock(return_value=3)) as drain,
    ):
        await scheduler.job_form_pdf_refresh()
    drain.assert_awaited_once_with("db")


# ── the table ───────────────────────────────────────────────────────────────


def test_the_table_is_one_row_per_form_and_indexed_on_the_only_question_asked():
    table = FormPdfRefresh.__table__
    assert table.name == "form_pdf_refresh_queue"
    uniques = {
        tuple(c.name for c in c_.columns)
        for c_ in table.constraints
        if c_.__class__.__name__ == "UniqueConstraint"
    }
    assert ("profile_id", "kind") in uniques
    assert table.c.due_at.index is True
    assert table.c.due_at.type.timezone is True
    assert table.c.profile_id.foreign_keys.pop().ondelete == "CASCADE"
    assert table.c.actor_name.nullable and table.c.actor_email.nullable


def test_the_migration_sits_on_0205_and_shouts_about_its_downgrade():
    from pathlib import Path

    src = Path("alembic/versions/0206_form_pdf_refresh_queue.py").read_text()
    assert 'revision = "0206_form_pdf_refresh_queue"' in src
    assert 'down_revision = "0205_worksheet_link_pins"' in src
    assert "uq_form_pdf_refresh_queue_profile_kind" in src
    assert 'ondelete="CASCADE"' in src
    # Loud about what a downgrade costs, in the docstring and again at the
    # statement that does it: pending redraws are lost, so the PDFs behind any
    # form saved in the last two minutes stay stale and nothing records which.
    docstring, downgrade = src.split("def downgrade()")
    assert "⚠️" in docstring and "⚠️" in downgrade
