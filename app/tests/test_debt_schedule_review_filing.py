"""Filing the reviewed schedule on the checklist.

Franco's desk filled the table in and the "Business debt schedule" row still
said a new file was needed: the evidence on it was the client's rejected PDF, and
rows alone do not satisfy a checklist. The desk's own submit already does what
is missing: it draws the PDF from the rows, puts it behind the slot as the one
drafted-form document that slot ever has, and marks the slot received. These
tests pin that the review's save reaches the same step (and the same words when
answers are missing), and that the desk's own submit still does what it did.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.enums import Role
from app.routers import application_profiles as routes
from app.schemas.application_profile import FinancialFormSave
from app.schemas.debt_schedule_review import DebtScheduleReviewWrite

COMPLETE_BODY = {
    "debts": [
        {
            "id": "row-1", "lender": "Credibly", "debt_type": "mca", "original_amount": "30000", "balance": "20000", "rate": "",
            "monthly_payment": "5000", "payment_status": "current", "mca_modified": "no",
        }
    ]
}
REVIEW = {"profile_id": "p", "sources": [], "lines": [], "removed": [], "counts": {}}


class _DB:
    def __init__(self):
        self.commits = 0

    async def execute(self, _statement):
        return None

    async def commit(self):
        self.commits += 1


@pytest.fixture
def desk(monkeypatch):
    state = SimpleNamespace(
        db=_DB(),
        profile=SimpleNamespace(id=uuid4(), dealer_id=None, primary_bucket_id=uuid4()),
        user=SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN, name="Franco", email="franco@example.test"),
        slot=SimpleNamespace(id=uuid4(), status="uploaded"),
        drafted=None,
        problems=[],
    )

    async def load(db, profile_id, user):
        return state.profile

    async def slot(db, profile, kind):
        return state.slot

    async def body(db, profile, origin=None):
        return COMPLETE_BODY

    async def problems(db, profile, rows, origin):
        return state.problems

    async def prefill(db, profile):
        return {"business_name": "Acme Inc."}

    async def existing(db, **kwargs):
        return state.drafted

    state.refresh = AsyncMock()
    state.enqueue = AsyncMock()
    state.emit = AsyncMock()
    state.log = AsyncMock()
    state.request = AsyncMock()
    state.apply = AsyncMock(return_value={"kept": 1, "added": 1, "updated": 0, "removed": 0, "restored": 0})
    monkeypatch.setattr(routes.profiles, "load_profile", load)
    monkeypatch.setattr(routes.profiles, "log_profile_action", state.log)
    monkeypatch.setattr(routes, "_requested_slot", slot)
    monkeypatch.setattr(routes, "request_financial_form", state.request)
    monkeypatch.setattr(routes.financial_statements, "debt_body_for_profile", body)
    monkeypatch.setattr(routes.financial_statements, "debt_submit_problems", problems)
    monkeypatch.setattr(routes.financial_statements, "form_prefill", prefill)
    monkeypatch.setattr(routes.drafted_forms, "refresh_form_pdf", state.refresh)
    monkeypatch.setattr(routes.drafted_forms, "enqueue_form_refresh", state.enqueue)
    monkeypatch.setattr(routes.drafted_forms, "_existing_drafted_form", existing)
    monkeypatch.setattr(routes.dealer_forms_pdf, "render_debt_schedule_pdf", lambda **kwargs: b"%PDF-1.7 " + str(kwargs["business_name"]).encode())
    monkeypatch.setattr(routes.file_events, "emit", state.emit)
    monkeypatch.setattr(routes.debt_schedule_review, "apply_review", state.apply)

    async def read(db, profile):
        return dict(REVIEW)

    monkeypatch.setattr(routes.debt_schedule_review, "read_review", read)
    return state


async def _save(desk, **over):
    payload = DebtScheduleReviewWrite(lines=[], **over)
    return await routes.apply_debt_schedule_review(desk.profile.id, payload, desk.user, desk.db)


@pytest.mark.asyncio
async def test_a_complete_schedule_is_filed_as_the_one_drafted_pdf_that_marks_the_checklist_row_received(desk):
    result = await _save(desk, file_as_schedule=True)
    assert result.filed is True and result.filing_problem is None
    desk.refresh.assert_awaited_once()
    kwargs = desk.refresh.await_args.kwargs
    assert kwargs["requested_document"] is desk.slot and kwargs["bucket_id"] == desk.profile.primary_bucket_id
    assert (kwargs["classification"], kwargs["file_label"], kwargs["mark_uploaded"]) == ("debt_schedule", "Business Debt Schedule", True)
    assert kwargs["pdf_bytes"].endswith(b"Acme Inc.")  # the business's name is on the PDF
    assert kwargs["key_facts"]["debts"][0]["lender"] == "Credibly"
    assert float(kwargs["key_facts"]["total_monthly_debt_service"]) == 5000.0
    desk.emit.assert_awaited_once()  # the timeline says it was submitted
    assert desk.emit.await_args.kwargs["kind"] == "document.received"
    desk.enqueue.assert_not_awaited()  # a submit has just filed it; nothing left to refresh
    assert any(call.args[3] == "financial_form.debt_schedule_filed" for call in desk.log.await_args_list)


@pytest.mark.asyncio
async def test_a_schedule_still_missing_answers_is_saved_but_not_filed_and_says_why_in_the_forms_words(desk):
    desk.problems = [{"index": 1, "lender": "Newtek", "missing": ["rate", "payment_status"]}]
    result = await _save(desk, file_as_schedule=True)
    assert result.filed is False
    assert result.filing_problem == "Obligation 1 (Newtek) still needs: interest rate, current or delinquent."
    desk.refresh.assert_not_awaited()
    desk.emit.assert_not_awaited()
    desk.apply.assert_awaited_once()  # the rows were saved regardless
    desk.enqueue.assert_awaited_once()  # and any PDF filed earlier is kept current with them
    assert desk.db.commits == 1


@pytest.mark.asyncio
async def test_saving_without_asking_to_file_files_nothing_and_keeps_an_earlier_pdf_current(desk):
    result = await _save(desk)
    assert result.filed is False and result.filing_problem is None
    desk.refresh.assert_not_awaited()
    desk.enqueue.assert_awaited_once()
    assert desk.enqueue.await_args.args[2] == "debt_schedule"


@pytest.mark.asyncio
async def test_a_file_with_no_checklist_row_gets_one_before_the_schedule_is_filed(desk, monkeypatch):
    calls = []

    async def slot(db, profile, kind):
        calls.append("slot")
        return None if desk.request.await_count == 0 else desk.slot

    monkeypatch.setattr(routes, "_requested_slot", slot)
    result = await _save(desk, file_as_schedule=True)
    desk.request.assert_awaited_once()
    assert desk.request.await_args.args[1] == "debt_schedule"
    assert result.filed is True
    desk.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_empty_schedule_is_not_filed(desk, monkeypatch):
    async def body(db, profile, origin=None):
        return {"debts": []}

    monkeypatch.setattr(routes.financial_statements, "debt_body_for_profile", body)
    result = await _save(desk, file_as_schedule=True)
    assert (result.filed, result.filing_problem) == (False, "There are no lines on the schedule to file.")
    desk.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_with_no_document_room_says_so_rather_than_failing(desk):
    desk.profile.primary_bucket_id = None
    result = await _save(desk, file_as_schedule=True)
    assert result.filed is False and "no document room" in result.filing_problem
    desk.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_review_says_whether_the_schedule_is_filed_only_when_the_slot_is_received_and_holds_our_pdf(desk):
    async def read(user):
        return await routes.get_debt_schedule_review(desk.profile.id, user, desk.db)

    assert (await read(desk.user)).filed is False  # nothing drafted yet
    desk.drafted = (object(), object())
    assert (await read(desk.user)).filed is True
    desk.slot.status = "requested"
    assert (await read(desk.user)).filed is False  # the row is still waiting
    desk.slot.status = "uploaded"
    desk.profile.primary_bucket_id = None
    assert (await read(desk.user)).filed is False


@pytest.mark.asyncio
async def test_the_desks_own_submit_still_files_the_same_pdf_the_same_way(desk, monkeypatch):
    save_rows = AsyncMock()
    monkeypatch.setattr(routes.financial_statements, "save_debt_rows", save_rows)
    payload = FinancialFormSave(body={**COMPLETE_BODY, "business_name": "Typed Co."}, submit=True)
    result = await routes.save_debt_schedule(desk.profile.id, payload, desk.user, desk.db)
    assert result == {"row_count": 1, "submitted": True}
    save_rows.assert_awaited_once()
    desk.refresh.assert_awaited_once()
    kwargs = desk.refresh.await_args.kwargs
    assert (kwargs["classification"], kwargs["mark_uploaded"], kwargs["file_label"]) == ("debt_schedule", True, "Business Debt Schedule")
    assert kwargs["pdf_bytes"].endswith(b"Typed Co.")
    desk.emit.assert_awaited_once()
    desk.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_desks_own_draft_save_files_nothing_and_queues_the_standing_pdf_refresh(desk, monkeypatch):
    monkeypatch.setattr(routes.financial_statements, "save_debt_rows", AsyncMock())
    result = await routes.save_debt_schedule(desk.profile.id, FinancialFormSave(body=COMPLETE_BODY, submit=False), desk.user, desk.db)
    assert result == {"row_count": 1, "submitted": False}
    desk.refresh.assert_not_awaited()
    desk.emit.assert_not_awaited()
    desk.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_desks_own_submit_with_missing_answers_is_still_refused_in_the_forms_words(desk, monkeypatch):
    from fastapi import HTTPException

    desk.problems = [{"index": 2, "lender": "Newtek", "missing": ["rate"]}]
    monkeypatch.setattr(routes.financial_statements, "save_debt_rows", AsyncMock())
    with pytest.raises(HTTPException) as refused:
        await routes.save_debt_schedule(desk.profile.id, FinancialFormSave(body=COMPLETE_BODY, submit=True), desk.user, desk.db)
    assert refused.value.status_code == 422
    assert refused.value.detail == "Obligation 2 (Newtek) still needs: interest rate."
    desk.refresh.assert_not_awaited()
