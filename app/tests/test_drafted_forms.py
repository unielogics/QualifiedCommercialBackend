"""Keeping a drafted form's PDF and its figures current.

The rules pinned here, in the order they matter:

* one document per (bucket, checklist row, classification) — a second save
  overwrites the first rather than adding a near-identical PDF to the room;
* the S3 key never moves, so links and ids stay valid, while the bytes, the
  `content_hash` and the `key_facts` all change;
* **a draft save does not satisfy the checklist row.** "Filled in" and
  "uploaded" are different states the desk reads;
* a save that has already committed cannot be undone by a picture we failed to
  draw: every failure below is swallowed and logged.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.bucket import BucketFile, BucketFileAnalysis
from app.routers import application_profiles as router
from app.routers import buckets as buckets_router
from app.routers import dealer_ai_intake as intake_router
from app.services import business_statement_schema as bss
from app.services import dealer_forms_pdf, drafted_forms


def _run(coro):
    return asyncio.run(coro)


class _Db:
    """Enough session for the refresh: one configurable lookup result, a list
    of what was added, and a savepoint that is a no-op."""

    def __init__(self, existing=None):
        self.existing = existing
        self.added: list = []
        self.statements: list = []
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        row = self.existing
        return SimpleNamespace(first=lambda: row)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def begin_nested(self):
        @asynccontextmanager
        async def _cm():
            yield None

        return _cm()


def _slot(status="requested"):
    return SimpleNamespace(id=uuid.uuid4(), name="Profit and loss statement", status=status)


def _profile(bucket=True):
    return SimpleNamespace(id=uuid.uuid4(), primary_bucket_id=uuid.uuid4() if bucket else None)


def _pl_body(gross_revenue="100000"):
    body = bss.pl_empty_body()
    body["header"].update(period_start="2026-01-01", period_end="2026-06-30")
    body["sections"]["revenue"]["gross_revenue"] = gross_revenue
    return body


def _storage(put=None):
    """The two S3 helpers `refresh_form_pdf` imports at call time."""
    return (
        patch.object(
            buckets_router, "_bucket_storage_config", MagicMock(return_value=("b", "buckets", "k"))
        ),
        patch.object(intake_router, "_put_bucket_object", put or MagicMock()),
    )


def _refresh(db, slot, *, pdf=b"%PDF-1", key_facts=None, mark_uploaded=False, bucket_id=None):
    return drafted_forms.refresh_form_pdf(
        db,
        bucket_id=bucket_id or uuid.uuid4(),
        requested_document=slot,
        pdf_bytes=pdf,
        file_label="Profit and loss statement · Jan–Jun 2026",
        classification="current_p_and_l",
        key_facts=key_facts if key_facts is not None else {"gross_revenue": 100000.0},
        actor_name="Jane Desk",
        actor_email="jane@example.com",
        mark_uploaded=mark_uploaded,
    )


# ── one document, refreshed ─────────────────────────────────────────────────


def test_the_first_save_files_a_new_document_on_the_drafted_forms_key():
    db, slot, put = _Db(), _slot(), MagicMock()
    bucket_id = uuid.uuid4()
    config, putter = _storage(put)
    with config, putter:
        stored = _run(_refresh(db, slot, bucket_id=bucket_id))

    assert isinstance(stored, BucketFile)
    analyses = [row for row in db.added if isinstance(row, BucketFileAnalysis)]
    assert [type(row) for row in db.added] == [BucketFile, BucketFileAnalysis]
    assert stored.s3_key == f"buckets/drafted-forms/{bucket_id}/{stored.id}.pdf"
    assert put.call_args.args == (stored.s3_key, "application/pdf", b"%PDF-1")
    assert analyses[0].provider == "drafted_form"
    assert analyses[0].classification == "current_p_and_l"
    assert analyses[0].analysis == {"key_facts": {"gross_revenue": 100000.0}}
    assert analyses[0].bucket_file_id == stored.id


def test_a_second_save_overwrites_the_same_file_and_analysis_rather_than_adding_rows():
    slot = _slot()
    bucket_id = uuid.uuid4()
    put = MagicMock()
    config, putter = _storage(put)
    with config, putter:
        first = _run(_refresh(_first_db := _Db(), slot, bucket_id=bucket_id))
        analysis = next(r for r in _first_db.added if isinstance(r, BucketFileAnalysis))
        first_hash, first_key = analysis.content_hash, first.s3_key

        # The next save finds what the first one filed.
        db = _Db(existing=(first, analysis))
        again = _run(
            _refresh(
                db,
                slot,
                pdf=b"%PDF-2-longer",
                key_facts={"gross_revenue": 250000.0},
                bucket_id=bucket_id,
            )
        )

    assert again is first                      # same row, same id
    assert db.added == []                      # nothing accumulated
    assert again.s3_key == first_key           # and the object did not move
    assert put.call_args.args == (first_key, "application/pdf", b"%PDF-2-longer")
    assert again.size_bytes == len(b"%PDF-2-longer")
    assert analysis.content_hash != first_hash
    assert analysis.analysis == {"key_facts": {"gross_revenue": 250000.0}}
    assert analysis.provider == "drafted_form" and analysis.status == "completed"


def test_the_lookup_is_scoped_to_the_slot_the_classification_and_our_own_provider():
    db, slot = _Db(), _slot()
    config, putter = _storage()
    with config, putter:
        _run(_refresh(db, slot))
    where = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "bucket_files.requested_document_id" in where
    assert "bucket_files.deleted_at IS NULL" in where
    assert "'drafted_form'" in where
    assert "'current_p_and_l'" in where       # the shared Main Street row holds two


# ── the checklist rule ──────────────────────────────────────────────────────


def test_a_draft_save_leaves_the_checklist_row_alone_and_a_submit_files_it():
    config, putter = _storage()
    with config, putter:
        draft_slot = _slot(status="requested")
        _run(_refresh(_Db(), draft_slot))
        assert draft_slot.status == "requested"

        submitted_slot = _slot(status="requested")
        _run(_refresh(_Db(), submitted_slot, mark_uploaded=True))
        assert submitted_slot.status == "uploaded"


def test_a_refresh_after_a_submit_never_moves_the_row_back():
    config, putter = _storage()
    slot = _slot(status="uploaded")
    with config, putter:
        _run(_refresh(_Db(), slot))
    assert slot.status == "uploaded"


def test_the_save_path_can_never_mark_the_row_uploaded():
    # `refresh_saved_form` is the only thing a save calls, and it pins the flag
    # shut rather than passing the caller's word for it.
    assert "mark_uploaded=False" in inspect.getsource(drafted_forms.refresh_saved_form)
    assert "mark_uploaded" not in inspect.getsource(router.save_business_statement)


# ── the save path: never fails the save ─────────────────────────────────────


def test_a_business_statement_save_refreshes_with_the_forms_own_figures():
    db, profile, slot = _Db(), _profile(), _slot()
    config, putter = _storage()
    with (
        config,
        putter,
        patch.object(router, "_requested_slot", AsyncMock(return_value=slot)),
        patch.object(dealer_forms_pdf, "render_p_and_l_pdf", return_value=b"%PDF") as render,
    ):
        stored = _run(
            drafted_forms.refresh_saved_form(
                db, profile, "p_and_l", body=_pl_body(), actor_name="Jane Desk"
            )
        )
    assert isinstance(stored, BucketFile)
    assert stored.file_name == "Profit and loss statement · Jan–Jun 2026.pdf"
    render.assert_called_once()
    analysis = next(row for row in db.added if isinstance(row, BucketFileAnalysis))
    assert analysis.classification == "current_p_and_l"
    assert analysis.analysis["key_facts"]["gross_revenue"] == 100000.0
    assert analysis.analysis["key_facts"]["source_form"] == "qc_pl.v1"
    assert slot.status == "requested"          # a draft is not an upload
    assert db.commits == 1                     # its own write, after the caller's


def test_a_renderer_that_raises_does_not_fail_the_save():
    db, profile = _Db(), _profile()
    config, putter = _storage()
    with (
        config,
        putter,
        patch.object(router, "_requested_slot", AsyncMock(return_value=_slot())),
        patch.object(dealer_forms_pdf, "render_p_and_l_pdf", side_effect=RuntimeError("weasy")),
    ):
        assert (
            _run(drafted_forms.refresh_saved_form(db, profile, "p_and_l", body=_pl_body()))
            is None
        )
    # Nothing written, nothing rolled back, and the committed save stands.
    assert db.added == [] and db.commits == 0 and db.rollbacks == 0


def test_a_failed_write_rolls_back_its_own_savepoint_and_still_returns():
    db, profile = _Db(), _profile()
    config, putter = _storage()
    with (
        config,
        putter,
        patch.object(router, "_requested_slot", AsyncMock(return_value=_slot())),
        patch.object(dealer_forms_pdf, "render_p_and_l_pdf", return_value=b"%PDF"),
        patch.object(
            drafted_forms, "refresh_form_pdf", AsyncMock(side_effect=RuntimeError("db gone"))
        ),
    ):
        assert (
            _run(drafted_forms.refresh_saved_form(db, profile, "p_and_l", body=_pl_body()))
            is None
        )
    assert db.rollbacks == 1 and db.commits == 0


def test_no_document_room_is_a_silent_no_op_that_never_touches_the_session():
    db = _Db()
    assert _run(drafted_forms.refresh_saved_form(db, _profile(bucket=False), "pfs")) is None
    assert _run(drafted_forms.refresh_saved_form(db, None, "pfs")) is None
    assert db.statements == [] and db.added == [] and db.commits == 0


def test_no_checklist_row_and_an_unknown_form_are_silent_no_ops():
    db, profile = _Db(), _profile()
    with patch.object(router, "_requested_slot", AsyncMock(return_value=None)) as finder:
        assert (
            _run(drafted_forms.refresh_saved_form(db, profile, "p_and_l", body=_pl_body()))
            is None
        )
    finder.assert_awaited_once()
    with patch.object(router, "_requested_slot", AsyncMock()) as never:
        assert _run(drafted_forms.refresh_saved_form(db, profile, "worksheet")) is None
    never.assert_not_awaited()
    assert db.added == [] and db.commits == 0


def test_an_empty_form_is_not_filed():
    db, profile = _Db(), _profile()
    with (
        patch.object(router, "_requested_slot", AsyncMock(return_value=_slot())),
        patch.object(drafted_forms, "refresh_form_pdf", AsyncMock()) as wrote,
    ):
        assert (
            _run(drafted_forms.refresh_saved_form(db, profile, "p_and_l", body={}, statement=None))
            is None
        )
    wrote.assert_not_awaited()


# ── where the save paths call it ────────────────────────────────────────────

DRAFT_SITES = (
    router.save_business_statement,
    router.save_debt_schedule,
    router.update_financial_statement,
    router.save_public_financial_form_draft,
)


def test_every_draft_save_refreshes_the_pdf_after_its_commit():
    for handler in DRAFT_SITES:
        src = inspect.getsource(handler)
        assert "drafted_forms.refresh_saved_form(" in src, handler.__name__
        # After the save is durable: rendering is WeasyPrint and a put is a
        # network hop, and neither may sit inside the transaction the borrower
        # is waiting on.
        assert src.index("await db.commit()") < src.index(
            "drafted_forms.refresh_saved_form("
        ), handler.__name__


def test_the_public_draft_refreshes_all_three_kinds_it_serves():
    src = inspect.getsource(router.save_public_financial_form_draft)
    assert src.count("drafted_forms.refresh_saved_form(") == 3
    assert '"debt_schedule"' in src and "link.kind" in src and '"pfs"' in src


def test_a_submit_is_not_also_refreshed_by_the_draft_path():
    """A submit files the sheet itself; the draft refresh must not run too and
    re-put identical bytes a moment later."""
    for handler in (router.save_business_statement, router.save_debt_schedule):
        src = inspect.getsource(handler)
        assert "if not payload.submit:" in src, handler.__name__


@pytest.mark.asyncio
async def test_a_draft_save_then_a_submit_leaves_one_document_not_two():
    """The whole point of refreshing in place.

    A borrower types, autosave refreshes the PDF so the AI reads today's
    figures, and then they press Send. If submit created its own file the slot
    would carry two near-identical PDFs and `_slot_analyses`' "newest analysis
    per file" would start averaging a stale one against a fresh one.
    """
    import inspect

    from app.routers import application_profiles as ap
    from app.services import business_statements

    # Every one of the four forms files through the in-place path, and each
    # asks it to mark the requirement met — which a draft save never does.
    for source in (
        inspect.getsource(business_statements.file_pdf),
        inspect.getsource(ap._file_statement),
        inspect.getsource(ap.submit_public_financial_form),
        inspect.getsource(ap.save_debt_schedule),
    ):
        assert "store_form_pdf(" not in source, (
            "a submit that creates its own document leaves the draft's behind"
        )
        assert "refresh_form_pdf(" in source and "mark_uploaded=True" in source


def test_every_worksheet_write_refreshes_the_document_the_ai_reads():
    """A figure typed in the grid has to reach the PDF, exactly as one typed in
    the stacked forms does — otherwise the extractors, the intelligence cards
    and the lender packet read whatever was there at the last submit."""
    import inspect

    from app.routers import application_profiles as ap
    from app.routers import worksheets as ws

    for handler in (
        ap.write_worksheet_cells, ap.write_worksheet_row,
        ws.write_worksheet_cells, ws.write_worksheet_rows,
    ):
        source = inspect.getsource(handler)
        assert "refresh_touched_pdfs(" in source, handler.__qualname__
        # After the commit, never inside it: rendering is WeasyPrint and an S3
        # put, and neither belongs in the transaction holding the typed cell.
        assert source.index("await db.commit()") < source.index("refresh_touched_pdfs("), (
            f"{handler.__qualname__} refreshes before it commits"
        )
